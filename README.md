# QGIS Safe MCP

Conecta un asistente de IA compatible con [MCP](https://modelcontextprotocol.io) (Claude Code, Claude Desktop u otros) con **tu sesión abierta de QGIS**, sin dejar puertas abiertas.

El asistente puede consultar el proyecto, leer atributos, ver el mapa, añadir capas y ejecutar algoritmos de Processing. Solo puede usar una **lista cerrada de acciones**: no existe ninguna forma de ejecutar código arbitrario en QGIS.

Compatible con **QGIS 3.36+ y QGIS 4.x** (Qt5 y Qt6).

## Cómo funciona

```
Asistente IA ──stdio──▶ servidor MCP (server/) ──TCP 127.0.0.1 + token──▶ plugin en QGIS (qgis_safe_mcp/)
```

1. En QGIS pulsas **Iniciar** en el panel del plugin.
2. El plugin abre un puerto aleatorio en `127.0.0.1` y genera un token secreto, que guarda en un archivo de sesión privado de tu usuario.
3. El servidor MCP lee ese archivo y envía las peticiones del asistente con el token.
4. Al pulsar **Detener**, cerrar QGIS o desactivar el plugin, el puerto se cierra y el archivo de sesión se borra.

## Seguridad

| Medida | Detalle |
|---|---|
| Solo local | Escucha únicamente en `127.0.0.1` y rechaza conexiones que no sean de loopback. |
| Token por sesión | Token aleatorio de 256 bits que se compara en tiempo constante. Tras 5 intentos fallidos el puente se apaga solo. |
| Sin ejecución de código | No hay `exec`/`eval`. Cada acción es una función concreta con parámetros validados. |
| Processing restringido | Solo los algoritmos de una lista blanca editable desde el panel. Las salidas solo pueden ser rutas o `TEMPORARY_OUTPUT`. |
| Apagado por defecto | Nunca arranca solo. Se apaga al cerrar QGIS. |
| Registro | Cada acción queda registrada en el panel y en el registro de mensajes de QGIS (pestaña *QGIS Safe MCP*). |
| Límites | Peticiones de 1 MB como máximo, 200 entidades por lectura e imágenes de 2048 px como máximo. |

**Límite conocido:** un programa malicioso que ya se esté ejecutando con tu usuario podría leer el archivo de sesión. Ese mismo programa también podría modificar directamente tus plugins de QGIS, así que el puente no añade un riesgo nuevo.

### Modos de confirmación

| | Manual (por defecto) | Automático |
|---|---|---|
| Lecturas, zoom, visibilidad, selección, añadir capas | Sin preguntar | Sin preguntar |
| Quitar capas, ejecutar Processing, exportar imagen, guardar/abrir/nuevo proyecto | **Aparece un diálogo en QGIS** y tú aceptas o rechazas | Se ejecuta sin preguntar |
| Escrituras en disco | Donde tú confirmes | **Solo dentro de la carpeta de trabajo** |
| Cambios sin guardar | Te avisa en el diálogo | Se niega a abrir o crear un proyecto hasta que guardes |

El modo y la carpeta de trabajo se cambian en el panel y se pueden cambiar con el puente encendido. Para activar el modo automático hay que confirmarlo explícitamente.

## Instalación (Windows)

Requisitos: QGIS 3.36+ o 4.x, [uv](https://docs.astral.sh/uv/) y, opcionalmente, [Claude Code](https://claude.com/claude-code).

```powershell
git clone https://github.com/alberttkd/qgis-safe-mcp.git
cd qgis-safe-mcp
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

El script copia el plugin a tu perfil de QGIS (usa `QGIS4` si existe, o `QGIS3`), instala las dependencias del servidor y lo registra en Claude Code como `qgis-safe`.

Después:

1. Reinicia QGIS.
2. En **Complementos → Administrar e instalar complementos → Instalados**, activa **QGIS Safe MCP**. Como es un plugin experimental, puede que tengas que marcar "Mostrar también complementos experimentales" en Configuración.
3. Abre el panel con el icono del escudo y pulsa **Iniciar**.

### Instalación manual u otros sistemas

1. Copia la carpeta `qgis_safe_mcp/` en `<perfil de QGIS>/python/plugins/`. Encuentras la ruta en QGIS en *Configuración → Perfiles de usuario → Abrir carpeta del perfil activo*.
2. Configura tu cliente MCP para lanzar:

```json
{
  "mcpServers": {
    "qgis-safe": {
      "command": "uv",
      "args": ["--directory", "/RUTA/A/qgis-safe-mcp/server", "run", "qgis_safe_mcp_server.py"]
    }
  }
}
```

## Herramientas disponibles

| Herramienta | Qué hace | ¿Confirma en modo manual? |
|---|---|---|
| `get_bridge_info` | Versión de QGIS, modo, carpeta de trabajo | — |
| `get_project_info` | Archivo, SRC, capas, cambios sin guardar | — |
| `list_layers`, `describe_layer` | Capas, campos, SRC, extensión | — |
| `get_layer_features` | Entidades con filtro por expresión (máx. 200) | — |
| `get_map_image` | PNG de la vista actual, para que el asistente "vea" el mapa | — |
| `list_allowed_algorithms` | Lista blanca de Processing | — |
| `zoom_to_layer`, `set_layer_visibility`, `select_by_expression` | Navegación y selección | — |
| `add_vector_layer`, `add_raster_layer`, `add_xyz_layer` | Añadir capas (XYZ solo https) | — |
| `remove_layer` | Quitar una capa | Sí |
| `run_processing` | Ejecutar un algoritmo permitido | Sí |
| `export_map_image` | Guardar el mapa como imagen | Sí |
| `save_project`, `load_project`, `new_project` | Gestión del proyecto | Sí |

## Ejemplos de uso

> "¿Qué capas tengo cargadas y cuántas entidades tiene cada una?"
>
> "Haz un buffer de 500 m a la capa *ríos* y muéstrame el resultado."
>
> "Selecciona los municipios con población > 50000 y enséñame el mapa."

## Estructura

```
qgis_safe_mcp/   Plugin de QGIS (servidor local, acciones, panel)
server/          Servidor MCP (stdio) en Python
install.ps1      Instalador para Windows
```

## Licencia

[GPL-2.0-or-later](LICENSE), igual que QGIS.
