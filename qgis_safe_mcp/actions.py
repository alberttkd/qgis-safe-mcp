"""Lista cerrada de acciones que el puente acepta y las reglas de seguridad.

Nada de lo que llega por la red se evalúa como código: cada acción es una
función concreta con parámetros validados.

Modos:
  - manual:     toda acción que modifica el proyecto o el disco pide
                confirmación en QGIS antes de ejecutarse.
  - automatico: no pregunta; las escrituras en disco solo se permiten dentro de
                la carpeta de trabajo y no se descartan cambios sin guardar.
"""

import base64
import os
from urllib.parse import quote, urlparse

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateTransform,
    QgsExpression,
    QgsFeatureRequest,
    QgsMapRendererParallelJob,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, QSize

MODE_MANUAL = "manual"
MODE_AUTO = "automatico"

MAX_FEATURES = 200
MAX_IMAGE_SIDE = 2048
MAX_WKT_CHARS = 2000
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tif", ".tiff")
PROJECT_EXTENSIONS = (".qgz", ".qgs")
TEMP_OUTPUTS = ("TEMPORARY_OUTPUT", "memory:")

DEFAULT_ALGORITHMS = [
    "native:buffer",
    "native:centroids",
    "native:clip",
    "native:countpointsinpolygon",
    "native:difference",
    "native:dissolve",
    "native:extractbyattribute",
    "native:extractbyexpression",
    "native:extractbylocation",
    "native:fieldcalculator",
    "native:fixgeometries",
    "native:intersection",
    "native:joinattributestable",
    "native:joinattributesbylocation",
    "native:mergevectorlayers",
    "native:reprojectlayer",
    "native:simplifygeometries",
    "native:union",
    "native:zonalstatisticsfb",
    "native:slope",
    "native:hillshade",
    "native:aspect",
    "gdal:cliprasterbymasklayer",
    "gdal:warpreproject",
    "gdal:contour",
]


class ActionError(Exception):
    """Error que se devuelve al cliente como mensaje legible."""


class Policy:
    """Configuración vigente; la lee el dispatcher en cada petición."""

    def __init__(self, mode=MODE_MANUAL, workdir="", algorithms=None):
        self.mode = mode
        self.workdir = workdir
        self.algorithms = list(algorithms or DEFAULT_ALGORITHMS)

    @property
    def automatic(self):
        return self.mode == MODE_AUTO


def _is_inside(path, folder):
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(folder)]) == os.path.normcase(folder)
    except ValueError:  # unidades distintas en Windows
        return False


def _to_json(value):
    if value is None:
        return None
    if hasattr(value, "isNull") and value.isNull():
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


class Dispatcher:
    def __init__(self, iface, policy, confirm):
        """confirm(titulo, texto) -> bool; solo se usa en modo manual."""
        self.iface = iface
        self.policy = policy
        self.confirm = confirm
        # nombre -> (función, modifica, descripción para la confirmación)
        self.actions = {
            "ping": (self.ping, False, None),
            "get_bridge_info": (self.get_bridge_info, False, None),
            "get_project_info": (self.get_project_info, False, None),
            "list_layers": (self.list_layers, False, None),
            "describe_layer": (self.describe_layer, False, None),
            "get_layer_features": (self.get_layer_features, False, None),
            "get_map_image": (self.get_map_image, False, None),
            "list_allowed_algorithms": (self.list_allowed_algorithms, False, None),
            "zoom_to_layer": (self.zoom_to_layer, False, None),
            "set_layer_visibility": (self.set_layer_visibility, False, None),
            "select_by_expression": (self.select_by_expression, False, None),
            "add_vector_layer": (self.add_vector_layer, False, None),
            "add_raster_layer": (self.add_raster_layer, False, None),
            "add_xyz_layer": (self.add_xyz_layer, False, None),
            "remove_layer": (self.remove_layer, True, self._describe_remove),
            "run_processing": (self.run_processing, True, self._describe_processing),
            "export_map_image": (self.export_map_image, True, self._describe_export),
            "save_project": (self.save_project, True, self._describe_save),
            "load_project": (self.load_project, True, self._describe_load),
            "new_project": (self.new_project, True, self._describe_new),
        }

    # ------------------------------------------------------------ despacho
    def handle(self, action, params):
        if action not in self.actions:
            raise ActionError(f"Acción no permitida: {action!r}")
        if not isinstance(params, dict):
            raise ActionError("'params' debe ser un objeto JSON")
        func, modifies, describe = self.actions[action]
        try:
            if modifies and not self.policy.automatic:
                text = describe(**params) if describe else action
                if not self.confirm(f"Claude quiere: {action}", text):
                    raise ActionError("El usuario rechazó la acción en QGIS")
            return func(**params)
        except TypeError as exc:
            raise ActionError(f"Parámetros inválidos para {action}: {exc}") from None

    # ------------------------------------------------------------ utilidades
    def _project(self):
        return QgsProject.instance()

    def _layer(self, layer):
        project = self._project()
        found = project.mapLayer(layer)
        if found:
            return found
        by_name = project.mapLayersByName(layer)
        if len(by_name) == 1:
            return by_name[0]
        if len(by_name) > 1:
            raise ActionError(f"Hay varias capas llamadas {layer!r}; usa su id")
        raise ActionError(f"Capa no encontrada: {layer!r}")

    def _vector(self, layer):
        lyr = self._layer(layer)
        if lyr.type() != Qgis.LayerType.Vector:
            raise ActionError(f"{lyr.name()!r} no es una capa vectorial")
        return lyr

    def _check_write(self, path, extensions=None):
        if not isinstance(path, str) or not path.strip():
            raise ActionError("Ruta de salida vacía")
        full = os.path.abspath(os.path.expanduser(path))
        if extensions and not full.lower().endswith(extensions):
            raise ActionError(f"Extensión no permitida; usa una de: {', '.join(extensions)}")
        if self.policy.automatic:
            workdir = self.policy.workdir
            if not workdir:
                raise ActionError("Modo automático sin carpeta de trabajo configurada")
            if not _is_inside(full, os.path.abspath(workdir)):
                raise ActionError(f"En modo automático solo se puede escribir dentro de {workdir}")
        parent = os.path.dirname(full)
        if not os.path.isdir(parent):
            raise ActionError(f"La carpeta no existe: {parent}")
        return full

    def _layer_summary(self, lyr):
        root = self._project().layerTreeRoot()
        node = root.findLayer(lyr.id())
        info = {
            "id": lyr.id(),
            "name": lyr.name(),
            "crs": lyr.crs().authid(),
            "visible": bool(node and node.isVisible()),
        }
        if lyr.type() == Qgis.LayerType.Vector:
            info.update(type="vector", geometry=QgsWkbTypes.displayString(lyr.wkbType()), feature_count=lyr.featureCount())
        elif lyr.type() == Qgis.LayerType.Raster:
            info.update(type="raster", width=lyr.width(), height=lyr.height(), bands=lyr.bandCount())
        else:
            info["type"] = str(lyr.type())
        return info

    def _render(self, width, height):
        width, height = int(width), int(height)
        if not (16 <= width <= MAX_IMAGE_SIDE and 16 <= height <= MAX_IMAGE_SIDE):
            raise ActionError(f"Tamaño fuera de rango (16-{MAX_IMAGE_SIDE} px)")
        settings = self.iface.mapCanvas().mapSettings()
        settings.setOutputSize(QSize(width, height))
        job = QgsMapRendererParallelJob(settings)
        job.start()
        job.waitForFinished()
        return job.renderedImage()

    # ------------------------------------------------------------ lectura
    def ping(self):
        return {"pong": True}

    def get_bridge_info(self):
        return {
            "qgis_version": Qgis.version(),
            "mode": self.policy.mode,
            "workdir": self.policy.workdir,
            "actions": sorted(self.actions),
            "actions_needing_confirmation_in_manual_mode": sorted(k for k, v in self.actions.items() if v[1]),
        }

    def get_project_info(self):
        project = self._project()
        return {
            "file": project.fileName(),
            "title": project.title(),
            "crs": project.crs().authid(),
            "unsaved_changes": project.isDirty(),
            "layer_count": len(project.mapLayers()),
            "canvas_extent": self.iface.mapCanvas().extent().toString(4),
        }

    def list_layers(self):
        root = self._project().layerTreeRoot()
        return [self._layer_summary(node.layer()) for node in root.findLayers() if node.layer()]

    def describe_layer(self, layer):
        lyr = self._layer(layer)
        info = self._layer_summary(lyr)
        info["extent"] = lyr.extent().toString(4)
        info["provider"] = lyr.providerType()
        if lyr.providerType() in ("ogr", "gdal"):
            info["source"] = lyr.source()
        if lyr.type() == Qgis.LayerType.Vector:
            info["fields"] = [{"name": f.name(), "type": f.typeName()} for f in lyr.fields()]
            info["selected_count"] = lyr.selectedFeatureCount()
        return info

    def get_layer_features(self, layer, limit=20, expression=None, include_geometry=False):
        lyr = self._vector(layer)
        limit = max(1, min(int(limit), MAX_FEATURES))
        request = QgsFeatureRequest()
        if expression:
            expr = QgsExpression(expression)
            if expr.hasParserError():
                raise ActionError(f"Expresión inválida: {expr.parserErrorString()}")
            request.setFilterExpression(expression)
        request.setLimit(limit)
        if not include_geometry:
            request.setFlags(Qgis.FeatureRequestFlag.NoGeometry)
        names = [f.name() for f in lyr.fields()]
        features = []
        for feat in lyr.getFeatures(request):
            item = {"id": feat.id(), "attributes": {n: _to_json(feat[n]) for n in names}}
            if include_geometry and feat.hasGeometry():
                wkt = feat.geometry().asWkt(6)
                item["wkt"] = wkt if len(wkt) <= MAX_WKT_CHARS else wkt[:MAX_WKT_CHARS] + "...(truncado)"
            features.append(item)
        return {"layer": lyr.name(), "fields": names, "returned": len(features), "features": features}

    def get_map_image(self, width=1024, height=768):
        image = self._render(width, height)
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buf, "PNG")
        buf.close()
        return {"format": "png", "base64": base64.b64encode(bytes(data)).decode("ascii")}

    def list_allowed_algorithms(self):
        registry = QgsApplication.processingRegistry()
        result = []
        for alg_id in self.policy.algorithms:
            alg = registry.algorithmById(alg_id)
            result.append({"id": alg_id, "name": alg.displayName() if alg else None, "available": bool(alg)})
        return result

    # ------------------------------------------------------------ cambios no destructivos
    def zoom_to_layer(self, layer):
        lyr = self._layer(layer)
        canvas = self.iface.mapCanvas()
        transform = QgsCoordinateTransform(lyr.crs(), self._project().crs(), self._project())
        extent = transform.transformBoundingBox(lyr.extent())
        extent.scale(1.05)
        canvas.setExtent(extent)
        canvas.refresh()
        return {"zoomed_to": lyr.name()}

    def set_layer_visibility(self, layer, visible):
        lyr = self._layer(layer)
        node = self._project().layerTreeRoot().findLayer(lyr.id())
        node.setItemVisibilityChecked(bool(visible))
        return {"layer": lyr.name(), "visible": bool(visible)}

    def select_by_expression(self, layer, expression):
        lyr = self._vector(layer)
        expr = QgsExpression(expression)
        if expr.hasParserError():
            raise ActionError(f"Expresión inválida: {expr.parserErrorString()}")
        lyr.selectByExpression(expression)
        return {"layer": lyr.name(), "selected": lyr.selectedFeatureCount()}

    def add_vector_layer(self, path, name=None):
        lyr = QgsVectorLayer(path, name or os.path.basename(path.split("|")[0]), "ogr")
        if not lyr.isValid():
            raise ActionError(f"No se pudo abrir la capa vectorial: {path}")
        self._project().addMapLayer(lyr)
        return self._layer_summary(lyr)

    def add_raster_layer(self, path, name=None):
        lyr = QgsRasterLayer(path, name or os.path.basename(path), "gdal")
        if not lyr.isValid():
            raise ActionError(f"No se pudo abrir el raster: {path}")
        self._project().addMapLayer(lyr)
        return self._layer_summary(lyr)

    def add_xyz_layer(self, url, name="Mapa base"):
        if urlparse(url).scheme != "https":
            raise ActionError("Solo se aceptan URLs https para teselas XYZ")
        uri = f"type=xyz&url={quote(url, safe=':/')}&zmin=0&zmax=19"
        lyr = QgsRasterLayer(uri, name, "wms")
        if not lyr.isValid():
            raise ActionError("No se pudo crear la capa XYZ")
        self._project().addMapLayer(lyr)
        return self._layer_summary(lyr)

    # ------------------------------------------------------------ cambios que requieren confirmación
    def _describe_remove(self, layer, **_):
        return f"Quitar la capa {self._layer(layer).name()!r} del proyecto."

    def remove_layer(self, layer):
        lyr = self._layer(layer)
        name = lyr.name()
        self._project().removeMapLayer(lyr.id())
        return {"removed": name}

    def _processing_outputs(self, algorithm, parameters):
        """Valida el algoritmo y las rutas de salida; devuelve las rutas a escribir."""
        if algorithm not in self.policy.algorithms:
            raise ActionError(f"Algoritmo no permitido: {algorithm!r}. Consulta list_allowed_algorithms.")
        alg = QgsApplication.processingRegistry().algorithmById(algorithm)
        if alg is None:
            raise ActionError(f"Algoritmo no disponible en esta instalación: {algorithm!r}")
        if not isinstance(parameters, dict):
            raise ActionError("'parameters' debe ser un objeto JSON")
        outputs = []
        for dest in alg.destinationParameterDefinitions():
            value = parameters.get(dest.name())
            if value is None or (isinstance(value, str) and value.startswith(TEMP_OUTPUTS)):
                continue
            if not isinstance(value, str):
                raise ActionError(f"Salida {dest.name()!r}: solo se aceptan rutas o TEMPORARY_OUTPUT")
            outputs.append(self._check_write(value))
        return alg, outputs

    def _describe_processing(self, algorithm, parameters=None, add_to_map=True, **_):
        alg, outputs = self._processing_outputs(algorithm, parameters or {})
        lines = [f"Ejecutar {alg.displayName()} ({algorithm})", "", "Parámetros:"]
        lines += [f"  {k} = {v}" for k, v in (parameters or {}).items()]
        if outputs:
            lines += ["", "Escribirá en disco:"] + [f"  {p}" for p in outputs]
        return "\n".join(lines)

    def run_processing(self, algorithm, parameters, add_to_map=True):
        import processing

        self._processing_outputs(algorithm, parameters)
        runner = processing.runAndLoadResults if add_to_map else processing.run
        result = runner(algorithm, parameters)
        return {"algorithm": algorithm, "result": {k: _to_json(v) for k, v in result.items()}}

    def _describe_export(self, path, width=1600, height=1200, **_):
        full = self._check_write(path, IMAGE_EXTENSIONS)
        extra = " (SOBRESCRIBE un archivo existente)" if os.path.exists(full) else ""
        return f"Exportar el mapa ({width}x{height}) a:\n{full}{extra}"

    def export_map_image(self, path, width=1600, height=1200):
        full = self._check_write(path, IMAGE_EXTENSIONS)
        if not self._render(width, height).save(full):
            raise ActionError(f"No se pudo guardar la imagen en {full}")
        return {"saved": full}

    def _describe_save(self, path=None, **_):
        target = path or self._project().fileName() or "(sin ruta)"
        return f"Guardar el proyecto en:\n{target}"

    def save_project(self, path=None):
        project = self._project()
        if path:
            target = self._check_write(path, PROJECT_EXTENSIONS)
        elif project.fileName():
            target = self._check_write(project.fileName(), PROJECT_EXTENSIONS)
        else:
            raise ActionError("El proyecto no tiene ruta; indica 'path'")
        if not project.write(target):
            raise ActionError(f"No se pudo guardar en {target}")
        return {"saved": target}

    def _guard_unsaved(self):
        if self.policy.automatic and self._project().isDirty():
            raise ActionError("Hay cambios sin guardar; guarda el proyecto antes (modo automático no los descarta)")

    def _describe_load(self, path, **_):
        dirty = "\n\nATENCIÓN: el proyecto actual tiene cambios sin guardar." if self._project().isDirty() else ""
        return f"Abrir el proyecto:\n{path}{dirty}"

    def load_project(self, path):
        if not path.lower().endswith(PROJECT_EXTENSIONS) or not os.path.isfile(path):
            raise ActionError(f"No es un proyecto QGIS existente: {path}")
        self._guard_unsaved()
        if not self._project().read(path):
            raise ActionError(f"No se pudo abrir {path}")
        return self.get_project_info()

    def _describe_new(self, **_):
        dirty = "\n\nATENCIÓN: el proyecto actual tiene cambios sin guardar." if self._project().isDirty() else ""
        return f"Cerrar el proyecto actual y empezar uno vacío.{dirty}"

    def new_project(self):
        self._guard_unsaved()
        self._project().clear()
        return {"new_project": True}
