"""Archivo de sesión compartido entre el plugin y el servidor MCP.

Contiene el puerto y el token de la sesión activa. Solo existe mientras el
puente está encendido y se guarda en una carpeta privada del usuario.
"""

import json
import os
import sys
from pathlib import Path

ENV_OVERRIDE = "QGIS_SAFE_MCP_SESSION"


def session_file() -> Path:
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "qgis_safe_mcp" / "session.json"


def write_session(port: int, token: str) -> Path:
    path = session_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"host": "127.0.0.1", "port": port, "token": token, "pid": os.getpid()}, fh)
    os.replace(tmp, path)
    return path


def remove_session() -> None:
    try:
        session_file().unlink()
    except FileNotFoundError:
        pass
