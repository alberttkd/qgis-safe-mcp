"""Servidor TCP local del puente.

Protocolo: una petición JSON por línea:
    {"id": "...", "token": "...", "action": "...", "params": {...}}
Respuesta, también una línea JSON:
    {"id": "...", "ok": true, "result": ...}  |  {"id": "...", "ok": false, "error": "..."}

Seguridad:
  - Escucha solo en 127.0.0.1, en un puerto aleatorio por sesión.
  - Cada petición debe llevar el token de la sesión (comparación en tiempo constante).
  - Tras MAX_AUTH_FAILURES tokens erróneos el servidor se apaga solo.
  - Las peticiones se procesan de una en una, en el hilo principal de QGIS.
"""

import hmac
import json
import secrets
import traceback

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.PyQt.QtNetwork import QAbstractSocket, QHostAddress, QTcpServer

from . import session
from .actions import ActionError

LOG_TAG = "QGIS Safe MCP"
MAX_REQUEST_BYTES = 1024 * 1024
MAX_AUTH_FAILURES = 5


class BridgeServer(QObject):
    message = pyqtSignal(str)
    stopped = pyqtSignal(str)

    def __init__(self, dispatcher, parent=None):
        super().__init__(parent)
        self.dispatcher = dispatcher
        self.server = None
        self.token = None
        self.port = None
        self._clients = {}  # id(socket) -> [socket, bytearray]
        self._queue = []
        self._busy = False
        self._auth_failures = 0

    @property
    def running(self):
        return self.server is not None and self.server.isListening()

    def _log(self, text, level=Qgis.MessageLevel.Info):
        QgsMessageLog.logMessage(text, LOG_TAG, level)
        self.message.emit(text)

    def start(self):
        if self.running:
            return
        self.server = QTcpServer(self)
        if not self.server.listen(QHostAddress(QHostAddress.SpecialAddress.LocalHost), 0):
            error = self.server.errorString()
            self.server = None
            raise RuntimeError(f"No se pudo abrir el puerto local: {error}")
        self.server.newConnection.connect(self._on_new_connection)
        self.token = secrets.token_urlsafe(32)
        self.port = self.server.serverPort()
        self._auth_failures = 0
        path = session.write_session(self.port, self.token)
        self._log(f"Puente activo en 127.0.0.1:{self.port} (sesión: {path})")

    def stop(self, reason="detenido por el usuario"):
        if self.server is None:
            return
        for sock, _ in list(self._clients.values()):
            sock.abort()
        self._clients.clear()
        self._queue.clear()
        self.server.close()
        self.server = None
        self.token = None
        session.remove_session()
        self._log(f"Puente apagado: {reason}")
        self.stopped.emit(reason)

    # ------------------------------------------------------------ conexiones
    def _on_new_connection(self):
        while self.server and self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            if not sock.peerAddress().isLoopback():
                sock.abort()
                continue
            self._clients[id(sock)] = [sock, bytearray()]
            sock.readyRead.connect(lambda s=sock: self._on_ready_read(s))
            sock.disconnected.connect(lambda s=sock: self._on_disconnected(s))

    def _on_disconnected(self, sock):
        self._clients.pop(id(sock), None)
        sock.deleteLater()

    def _on_ready_read(self, sock):
        entry = self._clients.get(id(sock))
        if entry is None:
            return
        buf = entry[1]
        buf.extend(bytes(sock.readAll()))
        if len(buf) > MAX_REQUEST_BYTES and b"\n" not in buf:
            self._send(sock, {"id": None, "ok": False, "error": "Petición demasiado grande"})
            sock.disconnectFromHost()
            return
        while b"\n" in buf:
            line, _, rest = bytes(buf).partition(b"\n")
            buf[:] = rest
            if line.strip():
                self._queue.append((sock, line))
        self._drain()

    def _drain(self):
        # Una confirmación en modo manual abre un diálogo modal (bucle de eventos
        # anidado); mientras tanto las peticiones nuevas solo se encolan.
        if self._busy:
            return
        self._busy = True
        try:
            while self._queue and self.running:
                sock, line = self._queue.pop(0)
                response, close = self._process(line)
                self._send(sock, response)
                if close:
                    sock.disconnectFromHost()
        finally:
            self._busy = False

    def _send(self, sock, payload):
        if sock.state() == QAbstractSocket.SocketState.ConnectedState:
            sock.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            sock.flush()

    # ------------------------------------------------------------ peticiones
    def _process(self, line):
        try:
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError
        except ValueError:
            return {"id": None, "ok": False, "error": "JSON inválido"}, True

        req_id = request.get("id")
        token = request.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token.encode(), self.token.encode()):
            self._auth_failures += 1
            self._log(f"Token rechazado ({self._auth_failures}/{MAX_AUTH_FAILURES})", Qgis.MessageLevel.Warning)
            if self._auth_failures >= MAX_AUTH_FAILURES:
                self.stop("demasiados intentos con token inválido")
            return {"id": req_id, "ok": False, "error": "No autorizado"}, True

        action = request.get("action")
        params = request.get("params") or {}
        try:
            result = self.dispatcher.handle(action, params)
            self._log(f"{action} -> ok")
            return {"id": req_id, "ok": True, "result": result}, False
        except ActionError as exc:
            self._log(f"{action} -> {exc}", Qgis.MessageLevel.Warning)
            return {"id": req_id, "ok": False, "error": str(exc)}, False
        except Exception as exc:  # noqa: BLE001 - se informa al cliente sin tumbar QGIS
            QgsMessageLog.logMessage(traceback.format_exc(), LOG_TAG, Qgis.MessageLevel.Critical)
            self._log(f"{action} -> error interno: {exc}", Qgis.MessageLevel.Critical)
            return {"id": req_id, "ok": False, "error": f"Error interno: {exc}"}, False
