"""Servidor MCP (stdio) que reenvía herramientas al plugin QGIS Safe MCP.

Lee el puerto y el token del archivo de sesión que crea el plugin al pulsar
"Iniciar" y habla con él por 127.0.0.1. No ejecuta nada por su cuenta.
"""

import base64
import json
import os
import socket
import sys
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

# Las confirmaciones en modo manual esperan a que el usuario responda en QGIS.
RESPONSE_TIMEOUT = float(os.environ.get("QGIS_SAFE_MCP_TIMEOUT", "330"))
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def session_file() -> Path:
    # Debe coincidir con qgis_safe_mcp/session.py
    override = os.environ.get("QGIS_SAFE_MCP_SESSION")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "qgis_safe_mcp" / "session.json"


def call(action: str, **params: Any) -> Any:
    try:
        sess = json.loads(session_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError(
            "El puente no está activo. En QGIS abre Complementos > QGIS Safe MCP y pulsa 'Iniciar'."
        ) from None

    request = {
        "id": uuid.uuid4().hex,
        "token": sess["token"],
        "action": action,
        "params": {k: v for k, v in params.items() if v is not None},
    }
    try:
        with socket.create_connection(("127.0.0.1", int(sess["port"])), timeout=10) as sock:
            sock.settimeout(RESPONSE_TIMEOUT)
            sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
            data = bytearray()
            while b"\n" not in data:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise RuntimeError("Respuesta demasiado grande")
    except ConnectionRefusedError:
        raise RuntimeError("QGIS no responde: ¿se cerró QGIS o se detuvo el puente?") from None
    except socket.timeout:
        raise RuntimeError("Tiempo de espera agotado (¿confirmación pendiente en QGIS?)") from None

    if not data:
        raise RuntimeError("QGIS cerró la conexión sin responder")
    response = json.loads(bytes(data).split(b"\n", 1)[0])
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "Error desconocido"))
    return response["result"]


mcp = FastMCP(
    "qgis-safe",
    instructions=(
        "Controla la sesión de QGIS abierta por el usuario mediante una lista cerrada de acciones. "
        "No existe ejecución de código arbitrario. Llama primero a get_bridge_info para conocer el modo "
        "(manual: el usuario confirma cada cambio en QGIS; automatico: sin preguntas y escrituras "
        "limitadas a la carpeta de trabajo). Las capas se indican por id o por nombre exacto."
    ),
)


@mcp.tool()
def get_bridge_info() -> dict:
    """Versión de QGIS, modo actual (manual/automatico), carpeta de trabajo y acciones disponibles."""
    return call("get_bridge_info")


@mcp.tool()
def get_project_info() -> dict:
    """Archivo, SRC, número de capas, cambios sin guardar y extensión actual del mapa."""
    return call("get_project_info")


@mcp.tool()
def list_layers() -> list:
    """Lista las capas del proyecto en el orden del panel de capas."""
    return call("list_layers")


@mcp.tool()
def describe_layer(layer: str) -> dict:
    """Detalles de una capa: campos, SRC, extensión, origen y número de entidades."""
    return call("describe_layer", layer=layer)


@mcp.tool()
def get_layer_features(layer: str, limit: int = 20, expression: str | None = None, include_geometry: bool = False) -> dict:
    """Lee entidades de una capa vectorial (máximo 200), con filtro opcional en expresión QGIS."""
    return call("get_layer_features", layer=layer, limit=limit, expression=expression, include_geometry=include_geometry)


@mcp.tool()
def get_map_image(width: int = 1024, height: int = 768) -> Image:
    """Devuelve una imagen PNG de la vista actual del mapa para poder verla."""
    result = call("get_map_image", width=width, height=height)
    return Image(data=base64.b64decode(result["base64"]), format="png")


@mcp.tool()
def list_allowed_algorithms() -> list:
    """Algoritmos de Processing que el usuario ha permitido ejecutar."""
    return call("list_allowed_algorithms")


@mcp.tool()
def zoom_to_layer(layer: str) -> dict:
    """Ajusta el mapa a la extensión de una capa."""
    return call("zoom_to_layer", layer=layer)


@mcp.tool()
def set_layer_visibility(layer: str, visible: bool) -> dict:
    """Muestra u oculta una capa."""
    return call("set_layer_visibility", layer=layer, visible=visible)


@mcp.tool()
def select_by_expression(layer: str, expression: str) -> dict:
    """Selecciona entidades de una capa vectorial con una expresión QGIS."""
    return call("select_by_expression", layer=layer, expression=expression)


@mcp.tool()
def add_vector_layer(path: str, name: str | None = None) -> dict:
    """Añade una capa vectorial desde archivo (shp, gpkg, geojson...; admite 'ruta.gpkg|layername=x')."""
    return call("add_vector_layer", path=path, name=name)


@mcp.tool()
def add_raster_layer(path: str, name: str | None = None) -> dict:
    """Añade una capa raster desde archivo (GeoTIFF, etc.)."""
    return call("add_raster_layer", path=path, name=name)


@mcp.tool()
def add_xyz_layer(url: str, name: str = "Mapa base") -> dict:
    """Añade un mapa base de teselas XYZ (solo https), p. ej. https://tile.openstreetmap.org/{z}/{x}/{y}.png"""
    return call("add_xyz_layer", url=url, name=name)


@mcp.tool()
def remove_layer(layer: str) -> dict:
    """Quita una capa del proyecto. En modo manual requiere confirmación del usuario."""
    return call("remove_layer", layer=layer)


@mcp.tool()
def run_processing(algorithm: str, parameters: dict, add_to_map: bool = True) -> dict:
    """Ejecuta un algoritmo permitido de Processing. Usa 'TEMPORARY_OUTPUT' para salidas en memoria.
    En modo manual requiere confirmación; en automático las salidas en disco deben ir a la carpeta de trabajo."""
    return call("run_processing", algorithm=algorithm, parameters=parameters, add_to_map=add_to_map)


@mcp.tool()
def export_map_image(path: str, width: int = 1600, height: int = 1200) -> dict:
    """Guarda la vista actual del mapa como imagen (png, jpg, tif)."""
    return call("export_map_image", path=path, width=width, height=height)


@mcp.tool()
def save_project(path: str | None = None) -> dict:
    """Guarda el proyecto (.qgz/.qgs) en su ruta actual o en 'path'."""
    return call("save_project", path=path)


@mcp.tool()
def load_project(path: str) -> dict:
    """Abre un proyecto .qgz/.qgs existente."""
    return call("load_project", path=path)


@mcp.tool()
def new_project() -> dict:
    """Cierra el proyecto actual y empieza uno vacío."""
    return call("new_project")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
