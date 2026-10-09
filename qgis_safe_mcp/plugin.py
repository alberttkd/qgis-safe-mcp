import os

from qgis.core import QgsApplication, QgsSettings
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAction,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .actions import DEFAULT_ALGORITHMS, MODE_AUTO, MODE_MANUAL, Dispatcher, Policy
from .bridge import BridgeServer

SETTINGS_PREFIX = "qgis_safe_mcp/"
PLUGIN_DIR = os.path.dirname(__file__)


def _default_workdir():
    return os.path.join(os.path.expanduser("~"), "Documents", "QGIS_Safe_MCP")


class AlgorithmsDialog(QDialog):
    def __init__(self, algorithms, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Algoritmos de Processing permitidos")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Un id por línea (por ejemplo native:buffer)."))
        self.editor = QPlainTextEdit("\n".join(algorithms))
        layout.addWidget(self.editor)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(
            lambda: self.editor.setPlainText("\n".join(DEFAULT_ALGORITHMS))
        )
        layout.addWidget(buttons)
        self.resize(420, 480)

    def algorithms(self):
        return [line.strip() for line in self.editor.toPlainText().splitlines() if line.strip()]


class SafeMcpDock(QDockWidget):
    def __init__(self, iface, policy, bridge):
        super().__init__("QGIS Safe MCP")
        self.iface = iface
        self.policy = policy
        self.bridge = bridge
        self.bridge.message.connect(self._append_log)
        self.bridge.stopped.connect(lambda _reason: self._refresh())

        body = QWidget()
        layout = QVBoxLayout(body)

        self.status = QLabel()
        layout.addWidget(self.status)

        row = QHBoxLayout()
        self.start_btn = QPushButton("Iniciar")
        self.stop_btn = QPushButton("Detener")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(lambda: self.bridge.stop())
        row.addWidget(self.start_btn)
        row.addWidget(self.stop_btn)
        layout.addLayout(row)

        layout.addWidget(QLabel("Modo:"))
        self.mode_box = QComboBox()
        self.mode_box.addItem("Manual: pide confirmación", MODE_MANUAL)
        self.mode_box.addItem("Automático: ejecuta sin preguntar", MODE_AUTO)
        self.mode_box.setCurrentIndex(1 if policy.automatic else 0)
        self.mode_box.currentIndexChanged.connect(self._on_mode_changed)
        layout.addWidget(self.mode_box)

        layout.addWidget(QLabel("Carpeta de trabajo (escrituras en modo automático):"))
        wd_row = QHBoxLayout()
        self.workdir_edit = QLineEdit(policy.workdir)
        self.workdir_edit.setReadOnly(True)
        wd_btn = QPushButton("Cambiar…")
        wd_btn.clicked.connect(self._choose_workdir)
        wd_row.addWidget(self.workdir_edit)
        wd_row.addWidget(wd_btn)
        layout.addLayout(wd_row)

        alg_btn = QPushButton("Algoritmos permitidos…")
        alg_btn.clicked.connect(self._edit_algorithms)
        layout.addWidget(alg_btn)

        layout.addWidget(QLabel("Registro:"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        layout.addWidget(self.log)

        self.setWidget(body)
        self._refresh()

    # ------------------------------------------------------------ estado
    def _refresh(self):
        running = self.bridge.running
        mode = "AUTOMÁTICO" if self.policy.automatic else "manual"
        if running:
            self.status.setText(f"<b style='color:#2e7d32'>● Activo</b> en 127.0.0.1:{self.bridge.port} — modo {mode}")
        else:
            self.status.setText(f"<b style='color:#888'>○ Apagado</b> — modo {mode}")
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    def _append_log(self, text):
        self.log.appendPlainText(text)

    def start(self):
        try:
            self.bridge.start()
        except RuntimeError as exc:
            QMessageBox.critical(self, "QGIS Safe MCP", str(exc))
        self._refresh()

    # ------------------------------------------------------------ ajustes
    def _on_mode_changed(self, index):
        mode = self.mode_box.itemData(index)
        if mode == MODE_AUTO:
            answer = QMessageBox.warning(
                self,
                "Activar modo automático",
                "En modo automático el asistente podrá quitar capas, ejecutar algoritmos, "
                "guardar y abrir proyectos SIN pedir confirmación.\n\n"
                f"Las escrituras en disco quedarán limitadas a:\n{self.policy.workdir}\n\n¿Activarlo?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.mode_box.blockSignals(True)
                self.mode_box.setCurrentIndex(0)
                self.mode_box.blockSignals(False)
                return
            os.makedirs(self.policy.workdir, exist_ok=True)
        self.policy.mode = mode
        QgsSettings().setValue(SETTINGS_PREFIX + "mode", mode)
        self._append_log(f"Modo cambiado a: {mode}")
        self._refresh()

    def _choose_workdir(self):
        folder = QFileDialog.getExistingDirectory(self, "Carpeta de trabajo", self.policy.workdir)
        if folder:
            self.policy.workdir = os.path.abspath(folder)
            self.workdir_edit.setText(self.policy.workdir)
            QgsSettings().setValue(SETTINGS_PREFIX + "workdir", self.policy.workdir)

    def _edit_algorithms(self):
        dialog = AlgorithmsDialog(self.policy.algorithms, self)
        if dialog.exec():
            self.policy.algorithms = dialog.algorithms()
            QgsSettings().setValue(SETTINGS_PREFIX + "algorithms", self.policy.algorithms)


class SafeMcpPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dock = None
        settings = QgsSettings()
        mode = settings.value(SETTINGS_PREFIX + "mode", MODE_MANUAL)
        algorithms = settings.value(SETTINGS_PREFIX + "algorithms", DEFAULT_ALGORITHMS)
        if isinstance(algorithms, str):
            algorithms = [algorithms]
        self.policy = Policy(
            mode=mode if mode in (MODE_MANUAL, MODE_AUTO) else MODE_MANUAL,
            workdir=settings.value(SETTINGS_PREFIX + "workdir", _default_workdir()),
            algorithms=algorithms,
        )
        self.dispatcher = Dispatcher(iface, self.policy, self._confirm)
        self.bridge = BridgeServer(self.dispatcher)

    def _confirm(self, title, text):
        parent = self.iface.mainWindow()
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setWindowFlags(box.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        box.exec()  # en PyQt6 devuelve int, no el enum: se compara el botón pulsado
        return box.clickedButton() == box.button(QMessageBox.StandardButton.Yes)

    def initGui(self):
        self.action = QAction(QIcon(os.path.join(PLUGIN_DIR, "icon.svg")), "QGIS Safe MCP", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.toggled.connect(self._toggle_dock)
        self.iface.addPluginToMenu("QGIS Safe MCP", self.action)
        self.iface.addToolBarIcon(self.action)
        QgsApplication.instance().aboutToQuit.connect(self._shutdown)

    def _toggle_dock(self, checked):
        if self.dock is None:
            self.dock = SafeMcpDock(self.iface, self.policy, self.bridge)
            self.dock.visibilityChanged.connect(self.action.setChecked)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.dock.setVisible(checked)

    def _shutdown(self):
        self.bridge.stop("QGIS se está cerrando")

    def unload(self):
        self.bridge.stop("plugin descargado")
        try:
            QgsApplication.instance().aboutToQuit.disconnect(self._shutdown)
        except TypeError:
            pass
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        self.iface.removePluginMenu("QGIS Safe MCP", self.action)
        self.iface.removeToolBarIcon(self.action)
