#!/usr/bin/env python3
"""QGent headless QGIS engine.

Runs under QGIS's own Python (stdlib + PyQGIS only — never import qgent_cli
here) as a long-lived child of the ``qgent`` process, so a loaded project,
layers and print layouts survive from one agent turn to the next.

Protocol: one JSON object per line.
    stdin   {"id": 1, "tool": "execute_pyqgis", "args": {...}}
    stdout  {"event": "ready", ...}             once, after QGIS initialised
            {"id": 1, "ok": true, "result": "..."}
Native libraries also print to fd 1, so the protocol keeps a private dup of
it and everything else (Python prints included) is routed to stderr.
EOF on stdin means the parent is gone: the engine exits.

Requests run one at a time on the main thread, which owns QgsApplication.
"""
import contextlib
import io
import json
import os
import queue
import stat
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)

MAX_RESULT = int(os.environ.get("QGENT_MAX_RESULT", "20000"))
RENDER_DIR = os.environ.get("QGENT_RENDER_DIR") or os.path.join(
    os.path.expanduser("~"), ".qgent", "renders")


def _claim_protocol_stream():
    """Reserve fd 1 for protocol JSON; send every other write to stderr."""
    sys.stdout.flush()
    proto_fd = os.dup(1)
    os.dup2(2, 1)
    proto = os.fdopen(proto_fd, "w", encoding="utf-8", newline="\n")
    sys.stdout = sys.stderr
    return proto


def _truncate(text):
    if len(text) > MAX_RESULT:
        return text[:MAX_RESULT] + (
            f"\n…[truncated, {len(text) - MAX_RESULT} more chars]")
    return text


def _jsonable(value):
    if value is None:
        return None
    try:
        if repr(value) == "NULL":
            return None
    except Exception:
        pass
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class _NoOp:
    """Absorbs GUI-only calls (message bar, layer tree view refreshes)."""

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _View:
    """Stand-in for QgsMapCanvas: a virtual view that renders on demand."""

    def __init__(self):
        self._extent = None
        self._crs = None

    def _project(self):
        from qgis.core import QgsProject
        return QgsProject.instance()

    def destinationCrs(self):
        from qgis.core import QgsCoordinateReferenceSystem
        if self._crs is not None and self._crs.isValid():
            return self._crs
        crs = self._project().crs()
        if crs.isValid():
            return crs
        for layer in self.layers():
            if layer.crs().isValid():
                return layer.crs()
        return QgsCoordinateReferenceSystem("EPSG:4326")

    def setDestinationCrs(self, crs):
        self._crs = crs

    def layers(self):
        project = self._project()
        root = project.layerTreeRoot()
        out = []
        for layer in root.layerOrder():
            node = root.findLayer(layer.id())
            if node is not None and node.isVisible() and layer.isValid():
                out.append(layer)
        return out

    def fullExtent(self):
        from qgis.core import QgsCoordinateTransform, QgsRectangle
        target = self.destinationCrs()
        combined = None
        for layer in self.layers():
            rect = layer.extent()
            if rect.isNull():
                continue
            try:
                rect = QgsCoordinateTransform(
                    layer.crs(), target, self._project()).transformBoundingBox(rect)
            except Exception:
                continue
            if combined is None:
                combined = QgsRectangle(rect)
            else:
                combined.combineExtentWith(rect)
        if combined is None:
            return QgsRectangle()
        if combined.width() == 0 or combined.height() == 0:
            combined.grow(max(combined.width(), combined.height(), 1.0) * 0.05
                          or 1.0)
        else:
            combined.scale(1.05)
        return combined

    def extent(self):
        from qgis.core import QgsRectangle
        if self._extent is not None and not self._extent.isEmpty():
            return QgsRectangle(self._extent)
        return self.fullExtent()

    def setExtent(self, rect, magnified=False):
        from qgis.core import QgsRectangle
        self._extent = QgsRectangle(rect)
        return True

    def zoomToFullExtent(self):
        self._extent = None

    def zoomScale(self, scale):
        settings = self.mapSettings()
        current = settings.scale()
        if current > 0:
            rect = self.extent()
            rect.scale(float(scale) / current)
            self._extent = rect

    def scale(self):
        return self.mapSettings().scale()

    def mapSettings(self, width=1200, height=800):
        from qgis.core import QgsMapSettings
        from qgis.PyQt.QtCore import QSize
        from qgis.PyQt.QtGui import QColor
        settings = QgsMapSettings()
        settings.setDestinationCrs(self.destinationCrs())
        settings.setLayers(self.layers())
        settings.setOutputSize(QSize(int(width), int(height)))
        settings.setBackgroundColor(QColor(255, 255, 255))
        settings.setTransformContext(self._project().transformContext())
        extent = self.extent()
        if not extent.isEmpty():
            settings.setExtent(extent)
        return settings

    def refresh(self, *_args):
        pass

    refreshAllLayers = refresh
    freeze = refresh


class HeadlessIface:
    """The bits of QgisInterface PyQGIS snippets commonly touch."""

    def __init__(self):
        self._canvas = _View()
        self._active_id = None

    def mapCanvas(self):
        return self._canvas

    def activeLayer(self):
        from qgis.core import QgsProject
        if self._active_id:
            return QgsProject.instance().mapLayer(self._active_id)
        return None

    def setActiveLayer(self, layer):
        self._active_id = layer.id() if layer is not None else None
        return True

    def zoomToActiveLayer(self):
        from qgis.core import QgsCoordinateTransform, QgsProject
        layer = self.activeLayer()
        if layer is None:
            return
        rect = QgsCoordinateTransform(
            layer.crs(), self._canvas.destinationCrs(),
            QgsProject.instance()).transformBoundingBox(layer.extent())
        rect.scale(1.05)
        self._canvas.setExtent(rect)

    def addVectorLayer(self, path, name="", provider="ogr"):
        from qgis.core import QgsProject, QgsVectorLayer
        layer = QgsVectorLayer(path, name or os.path.splitext(
            os.path.basename(path))[0], provider)
        if not layer.isValid():
            return None
        QgsProject.instance().addMapLayer(layer)
        return layer

    def addRasterLayer(self, path, name="", provider="gdal"):
        from qgis.core import QgsProject, QgsRasterLayer
        layer = QgsRasterLayer(path, name or os.path.splitext(
            os.path.basename(path))[0], provider)
        if not layer.isValid():
            return None
        QgsProject.instance().addMapLayer(layer)
        return layer

    def layerTreeView(self):
        return _NoOp()

    def messageBar(self):
        return _NoOp()

    def mainWindow(self):
        return None

    def openLayoutDesigner(self, layout):
        from layout_tools import LayoutToolError
        name = layout.name() if hasattr(layout, "name") else str(layout)
        raise LayoutToolError(
            "QGent CLI runs QGIS headless, so there is no Layout Designer. "
            f"Layout {name!r} is in the project: export it, or save the "
            "project and open it in QGIS.")

    def __getattr__(self, name):
        raise AttributeError(
            f"iface.{name} needs the QGIS window; QGent CLI runs QGIS headless")


class _Feedback:
    """Collects processing log lines (built lazily: needs qgis imported)."""

    @staticmethod
    def make():
        from qgis.core import QgsProcessingFeedback

        class Feedback(QgsProcessingFeedback):
            def __init__(self):
                super().__init__()
                self.lines = []

            def pushInfo(self, info):
                self.lines.append(str(info))

            def pushWarning(self, warning):
                self.lines.append("WARNING: " + str(warning))

            def reportError(self, error, fatalError=False):
                self.lines.append("ERROR: " + str(error))

        return Feedback()


class Engine:
    def __init__(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        if os.name == "nt":
            os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(
                os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
        from qgis.core import QgsApplication
        from qgis.PyQt.QtCore import QCoreApplication
        # QGIS resolves its profiles folder from Qt's app-data location, which
        # follows these names; unset, a standalone app looks in
        # %APPDATA%/python/profiles instead of the user's real QGIS profiles.
        QCoreApplication.setOrganizationName("QGIS")
        QCoreApplication.setOrganizationDomain("qgis.org")
        QCoreApplication.setApplicationName("QGIS3")
        profile = os.environ.get("QGENT_QGIS_PROFILE_DIR", "")
        if profile:
            self.app = QgsApplication([], True, profile)
        else:
            self.app = QgsApplication([], True)
        self.app.initQgis()
        self._init_processing()
        self.iface = HeadlessIface()
        from layout_tools import LayoutTools
        self._layouts = LayoutTools(self.iface)
        self.ns = None

    def _init_processing(self):
        from qgis.core import QgsApplication
        plugins = os.path.join(QgsApplication.prefixPath(), "python", "plugins")
        if plugins not in sys.path:
            sys.path.append(plugins)
        from processing.core.Processing import Processing
        Processing.initialize()
        registry = QgsApplication.processingRegistry()
        if registry.algorithmById("native:buffer") is None:
            from qgis.analysis import QgsNativeAlgorithms
            registry.addProvider(QgsNativeAlgorithms())

    def info(self):
        from qgis.core import Qgis, QgsApplication
        return {
            "qgis_version": Qgis.version(),
            "python": sys.version.split()[0],
            "prefix": QgsApplication.prefixPath(),
            "profile": QgsApplication.qgisSettingsDirPath(),
            "algorithms": len(QgsApplication.processingRegistry().algorithms()),
            "pid": os.getpid(),
        }

    # -- dispatch -----------------------------------------------------------
    def call(self, tool, args):
        handler = getattr(self, "t_" + str(tool), None)
        if handler is None:
            return False, f"unknown engine tool: {tool}"
        try:
            return True, handler(args if isinstance(args, dict) else {})
        except Exception as exc:
            from layout_tools import LayoutToolError
            if isinstance(exc, (LayoutToolError, ToolError)):
                return False, "ERROR: " + str(exc)
            return False, "ERROR:\n" + traceback.format_exc()[-2500:]

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _project():
        from qgis.core import QgsProject
        return QgsProject.instance()

    def _layer(self, ref, required=True):
        project = self._project()
        if not ref:
            layer = self.iface.activeLayer()
        else:
            layer = project.mapLayer(ref)
            if layer is None:
                matches = project.mapLayersByName(ref)
                layer = matches[0] if matches else None
        if layer is None and required:
            names = [lyr.name() for lyr in project.mapLayers().values()]
            raise ToolError(f"layer not found: {ref!r}. Layers: {names}")
        return layer

    def _layer_summary(self, layer):
        from qgis.core import QgsProviderRegistry, QgsWkbTypes
        entry = {"name": layer.name(), "id": layer.id(),
                 "type": type(layer).__name__,
                 "provider": layer.providerType(),
                 "crs": layer.crs().authid() if layer.crs().isValid() else "",
                 "valid": layer.isValid()}
        try:
            parts = QgsProviderRegistry.instance().decodeUri(
                layer.providerType(), layer.source())
            if parts.get("path"):
                entry["path"] = parts["path"]
            if parts.get("layerName"):
                entry["sublayer"] = parts["layerName"]
        except Exception:
            pass
        if hasattr(layer, "featureCount"):
            entry["feature_count"] = layer.featureCount()
            entry["geometry"] = QgsWkbTypes.displayString(layer.wkbType())
            entry["fields"] = len(layer.fields())
        if hasattr(layer, "bandCount"):
            entry["bands"] = layer.bandCount()
            entry["size"] = [layer.width(), layer.height()]
        return entry

    @staticmethod
    def _rect(rect):
        return [round(rect.xMinimum(), 6), round(rect.yMinimum(), 6),
                round(rect.xMaximum(), 6), round(rect.yMaximum(), 6)]

    # -- tools --------------------------------------------------------------
    def t_ping(self, _args):
        return json.dumps(self.info())

    def _namespace(self):
        if self.ns is None:
            import processing
            from qgis import core as qgis_core
            ns = {"__name__": "__qgent__", "iface": self.iface,
                  "processing": processing, "qgis": __import__("qgis"),
                  "QGENT_ASSETS": os.environ.get("QGENT_ASSETS", "")}
            for name in dir(qgis_core):
                if name.startswith("Qgs") or name == "Qgis":
                    ns[name] = getattr(qgis_core, name)
            self.ns = ns
        return self.ns

    def t_execute_pyqgis(self, args):
        code = args.get("code") or ""
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                exec(compile(code, "<execute_pyqgis>", "exec"),
                     self._namespace())
        except Exception:
            captured = out.getvalue()
            raise ToolError((captured + "\n" if captured else "")
                            + traceback.format_exc()[-2500:]) from None
        text = out.getvalue()
        return _truncate(text) if text.strip() else "OK (no stdout)"

    def t_get_project_context(self, _args):
        project = self._project()
        root = project.layerTreeRoot()
        layers = []
        for layer in root.layerOrder() or list(project.mapLayers().values()):
            entry = self._layer_summary(layer)
            node = root.findLayer(layer.id())
            entry["visible"] = bool(node and node.isVisible())
            layers.append(entry)
        active = self.iface.activeLayer()
        view = self.iface.mapCanvas()
        ctx = {
            "project_file": project.fileName() or None,
            "project_title": project.title() or None,
            "unsaved_changes": project.isDirty(),
            "project_crs": project.crs().authid() if project.crs().isValid() else "",
            "layer_count": len(layers),
            "layers": layers,
            "active_layer": active.name() if active else None,
            "layouts": [layout.name() for layout in
                        project.layoutManager().printLayouts()],
            "view": {"crs": view.destinationCrs().authid(),
                     "extent": self._rect(view.extent())},
        }
        return _truncate(json.dumps(ctx, indent=1))

    def t_open_project(self, args):
        path = os.path.abspath(os.path.expanduser(str(args.get("path") or "")))
        if not os.path.isfile(path):
            raise ToolError(f"project file not found: {path}")
        project = self._project()
        if not project.read(path):
            raise ToolError(f"QGIS could not read {path}: {project.error()}")
        self.iface.mapCanvas().zoomToFullExtent()
        self.iface.mapCanvas().setDestinationCrs(None)
        try:
            default = project.viewSettings().defaultViewExtent()
            if not default.isEmpty():
                from qgis.core import QgsCoordinateTransform
                rect = QgsCoordinateTransform(
                    default.crs(), project.crs(), project).transformBoundingBox(
                        default)
                self.iface.mapCanvas().setExtent(rect)
        except Exception:
            pass
        layers = list(project.mapLayers().values())
        invalid = [layer.name() for layer in layers if not layer.isValid()]
        self.iface.setActiveLayer(layers[0] if layers else None)
        return json.dumps({"opened": path, "layers": len(layers),
                           "invalid_layers": invalid,
                           "layouts": len(project.layoutManager().printLayouts()),
                           "project_crs": project.crs().authid()}, indent=1)

    def t_new_project(self, args):
        from qgis.core import QgsCoordinateReferenceSystem
        project = self._project()
        project.clear()
        self.iface.setActiveLayer(None)
        self.iface.mapCanvas().zoomToFullExtent()
        self.iface.mapCanvas().setDestinationCrs(None)
        crs = args.get("crs")
        if crs:
            ref = QgsCoordinateReferenceSystem(str(crs))
            if not ref.isValid():
                raise ToolError(f"invalid CRS: {crs!r}")
            project.setCrs(ref)
        if args.get("title"):
            project.setTitle(str(args["title"]))
        return json.dumps({"new_project": True,
                           "project_crs": project.crs().authid()})

    def t_save_project(self, args):
        project = self._project()
        path = str(args.get("path") or "") or project.fileName()
        if not path:
            raise ToolError("the project has never been saved: pass a .qgz/.qgs path")
        path = os.path.abspath(os.path.expanduser(path))
        if not path.lower().endswith((".qgz", ".qgs")):
            raise ToolError("project path must end in .qgz or .qgs")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not project.write(path):
            raise ToolError(f"QGIS could not write {path}: {project.error()}")
        return json.dumps({"saved": path, "size_bytes": os.path.getsize(path),
                           "layers": len(project.mapLayers())})

    def t_load_layer(self, args):
        from qgis.core import QgsRasterLayer, QgsVectorLayer
        source = str(args.get("path") or args.get("source") or "")
        if not source:
            raise ToolError("missing 'path'")
        kind = str(args.get("kind") or "auto")
        provider = args.get("provider")
        local = os.path.abspath(os.path.expanduser(source.split("|")[0]))
        if os.path.exists(local):
            source = local + (source[len(source.split("|")[0]):]
                              if "|" in source else "")
        name = str(args.get("name") or "") or os.path.splitext(
            os.path.basename(local))[0] or "layer"
        layer = None
        if kind in ("auto", "vector"):
            candidate = QgsVectorLayer(source, name, provider or "ogr")
            if candidate.isValid():
                layer = candidate
        if layer is None and kind in ("auto", "raster"):
            candidate = QgsRasterLayer(source, name, provider or "gdal")
            if candidate.isValid():
                layer = candidate
        if layer is None:
            raise ToolError(f"could not load {source!r} as a {kind} layer "
                            f"(provider {provider or 'ogr/gdal'})")
        project = self._project()
        first = not project.mapLayers()
        project.addMapLayer(layer)
        if first and not project.crs().isValid() and layer.crs().isValid():
            project.setCrs(layer.crs())
        self.iface.setActiveLayer(layer)
        return json.dumps(self._layer_summary(layer), indent=1)

    def t_inspect_dataset(self, args):
        path = os.path.abspath(os.path.expanduser(str(args.get("path") or "")))
        if os.path.isdir(path) and not path.lower().endswith(".gdb"):
            return _truncate(json.dumps(_list_gis_files(path), indent=1))
        if not os.path.exists(path):
            raise ToolError(f"path not found: {path}")
        return _truncate(json.dumps(_describe_dataset(path), indent=1))

    def t_run_processing(self, args):
        import processing
        from qgis.core import QgsMapLayer, QgsProcessingContext
        alg_id = args.get("alg_id")
        if not alg_id:
            raise ToolError("missing 'alg_id'")
        params = dict(args.get("params") or {})
        from qgis.core import QgsApplication
        alg = QgsApplication.processingRegistry().algorithmById(alg_id)
        if alg is None:
            raise ToolError(f"unknown algorithm {alg_id!r}; use list_algorithms")
        if "OUTPUT" not in params and any(
                out.name() == "OUTPUT" for out in alg.outputDefinitions()):
            params["OUTPUT"] = "TEMPORARY_OUTPUT"
        feedback = _Feedback.make()
        context = QgsProcessingContext()
        context.setProject(self._project())
        started = time.time()
        result = processing.run(alg_id, params, feedback=feedback,
                                context=context)
        load = bool(args.get("load_outputs"))
        names = args.get("output_names") or {}
        outputs, loaded = {}, []
        for key, value in result.items():
            # processing.run hands back layer objects for layer outputs. A
            # memory layer exists nowhere else, so it always joins the project;
            # a file-backed one is reported by path and loaded only on request.
            if isinstance(value, QgsMapLayer):
                if value.providerType() == "memory" or load:
                    value.setName(str(names.get(key) or value.name() or key))
                    self._project().addMapLayer(value)
                    loaded.append(self._layer_summary(value))
                    outputs[key] = f"layer {value.name()!r} (added to project)"
                else:
                    outputs[key] = value.source()
                continue
            outputs[key] = _jsonable(value)
            if load and isinstance(value, str) and os.path.isfile(value):
                summary = json.loads(self.t_load_layer(
                    {"path": value, "name": names.get(key) or key}))
                loaded.append(summary)
        if loaded:
            # Like the QGIS GUI: the newest result becomes the active layer.
            self.iface.setActiveLayer(self._project().mapLayer(loaded[-1]["id"]))
        payload = {"alg_id": alg_id, "seconds": round(time.time() - started, 2),
                   "outputs": outputs, "loaded_layers": loaded}
        if feedback.lines:
            payload["log"] = feedback.lines[-30:]
        return _truncate(json.dumps(payload, indent=1, default=str))

    def t_list_algorithms(self, args):
        from qgis.core import QgsApplication
        query = str(args.get("query") or "").casefold()
        provider = str(args.get("provider") or "").casefold()
        limit = max(1, min(int(args.get("limit") or 60), 300))
        rows = []
        for alg in QgsApplication.processingRegistry().algorithms():
            if provider and alg.provider().id().casefold() != provider:
                continue
            hay = " ".join([alg.id(), alg.displayName(), " ".join(alg.tags()),
                            alg.group()]).casefold()
            if query and not all(word in hay for word in query.split()):
                continue
            rows.append(f"{alg.id()} — {alg.displayName()} [{alg.group()}]")
        rows.sort()
        more = len(rows) - limit
        text = "\n".join(rows[:limit]) or "no algorithms match"
        if more > 0:
            text += f"\n… {more} more; narrow the query"
        return text

    def t_algorithm_help(self, args):
        from qgis.core import QgsApplication
        alg_id = str(args.get("alg_id") or "")
        alg = QgsApplication.processingRegistry().algorithmById(alg_id)
        if alg is None:
            raise ToolError(f"unknown algorithm {alg_id!r}; use list_algorithms")
        params = []
        for param in alg.parameterDefinitions():
            entry = {"name": param.name(), "type": param.type(),
                     "description": param.description(),
                     "optional": bool(param.flags() & 8)}
            default = param.defaultValue()
            if default is not None:
                entry["default"] = _jsonable(default)
            if hasattr(param, "options") and callable(param.options):
                try:
                    entry["options"] = {i: o for i, o in enumerate(param.options())}
                except Exception:
                    pass
            params.append(entry)
        outputs = [{"name": out.name(), "type": out.type(),
                    "description": out.description()}
                   for out in alg.outputDefinitions()]
        payload = {"id": alg.id(), "name": alg.displayName(),
                   "parameters": params, "outputs": outputs,
                   "help": (alg.shortHelpString() or "")[:1500]}
        return _truncate(json.dumps(payload, indent=1, default=str))

    def t_get_layer_features(self, args):
        from qgis.core import QgsExpression, QgsFeatureRequest
        layer = self._layer(args.get("layer"))
        if not hasattr(layer, "getFeatures"):
            raise ToolError(f"{layer.name()!r} is not a vector layer")
        limit = max(1, min(int(args.get("limit") or 20), 200))
        request = QgsFeatureRequest()
        expression = args.get("filter_expr")
        if expression:
            parsed = QgsExpression(expression)
            if parsed.hasParserError():
                raise ToolError(f"bad filter_expr: {parsed.parserErrorString()}")
            request.setFilterExpression(expression)
        request.setLimit(limit)
        with_geometry = bool(args.get("with_geometry"))
        fields = [field.name() for field in layer.fields()]
        rows = []
        for feature in layer.getFeatures(request):
            row = {name: _jsonable(feature[name]) for name in fields}
            if with_geometry and feature.hasGeometry():
                row["_wkt"] = feature.geometry().asWkt(6)[:300]
            rows.append(row)
        payload = {"layer": layer.name(), "fields": fields,
                   "total_features": layer.featureCount(),
                   "returned": len(rows), "features": rows}
        return _truncate(json.dumps(payload, indent=1, default=str))

    def t_render_map_snapshot(self, args):
        from qgis.core import QgsMapRendererParallelJob, QgsRectangle
        width = max(64, min(int(args.get("width") or 1200), 4000))
        height = max(64, min(int(args.get("height") or 800), 4000))
        view = self.iface.mapCanvas()
        settings = view.mapSettings(width, height)
        names = args.get("layers")
        if names:
            settings.setLayers([self._layer(name) for name in names])
        extent = args.get("extent")
        if isinstance(extent, str) and extent.startswith("layer:"):
            layer = self._layer(extent[len("layer:"):].strip())
            from qgis.core import QgsCoordinateTransform
            rect = QgsCoordinateTransform(
                layer.crs(), settings.destinationCrs(),
                self._project()).transformBoundingBox(layer.extent())
            rect.scale(1.08)
            settings.setExtent(rect)
        elif isinstance(extent, (list, tuple)) and len(extent) == 4:
            settings.setExtent(QgsRectangle(*[float(v) for v in extent]))
        if not settings.layers():
            raise ToolError("nothing to render: no visible valid layers")
        if args.get("set_view"):
            view.setExtent(settings.extent())
        job = QgsMapRendererParallelJob(settings)
        job.start()
        job.waitForFinished()
        image = job.renderedImage()
        os.makedirs(RENDER_DIR, exist_ok=True)
        path = str(args.get("path") or "") or os.path.join(
            RENDER_DIR, time.strftime("map_%Y%m%d_%H%M%S") + f"_{os.getpid()}.png")
        if not image.save(path, "PNG"):
            raise ToolError(f"could not save {path}")
        errors = [f"{err.layerID}: {err.message}" for err in job.errors()]
        return json.dumps({"snapshot_path": path, "size": [width, height],
                           "extent": self._rect(settings.extent()),
                           "crs": settings.destinationCrs().authid(),
                           "layers": [layer.name() for layer in settings.layers()],
                           "render_errors": errors[:10]}, indent=1)

    def t_stat_path(self, args):
        path = args.get("path")
        if not isinstance(path, str) or not path:
            raise ToolError("missing 'path'")
        try:
            meta = os.stat(path)
        except FileNotFoundError:
            return json.dumps({"exists": False, "is_file": False,
                               "is_dir": False, "size_bytes": None,
                               "mtime": None})
        return json.dumps({
            "exists": True, "is_file": stat.S_ISREG(meta.st_mode),
            "is_dir": stat.S_ISDIR(meta.st_mode), "size_bytes": meta.st_size,
            "mtime": datetime.fromtimestamp(
                meta.st_mtime, tz=timezone.utc).isoformat()})

    def t_layout_info(self, args):
        return _truncate(json.dumps(self._layouts.info(args), indent=1,
                                    default=str))

    def t_manage_layouts(self, args):
        return _truncate(json.dumps(self._layouts.manage(args), indent=1,
                                    default=str))


class ToolError(Exception):
    """A tool failure whose message is the whole story (no traceback)."""


_GIS_EXTENSIONS = {
    ".shp", ".gpkg", ".geojson", ".json", ".kml", ".kmz", ".gml", ".csv",
    ".tab", ".mif", ".fgb", ".parquet", ".tif", ".tiff", ".vrt", ".asc",
    ".img", ".jp2", ".ecw", ".sid", ".nc", ".hgt", ".dem", ".qgz", ".qgs",
    ".qpt", ".qml", ".sld", ".dxf", ".gpx", ".sqlite", ".mbtiles", ".pmtiles",
}


def _list_gis_files(folder, limit=300):
    found = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")
                   and d not in ("__pycache__", "node_modules")]
        for name in dirs:
            if name.lower().endswith(".gdb"):
                found.append({"path": os.path.join(root, name),
                              "kind": "file geodatabase"})
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext in _GIS_EXTENSIONS:
                full = os.path.join(root, name)
                found.append({"path": full, "size_bytes": os.path.getsize(full)})
            if len(found) >= limit:
                return {"folder": folder, "files": found, "truncated": True}
        if root.count(os.sep) - folder.count(os.sep) >= 3:
            dirs[:] = []
    return {"folder": folder, "files": found, "truncated": False}


def _srs_label(srs):
    if srs is None:
        return ""
    try:
        srs.AutoIdentifyEPSG()
    except Exception:
        pass
    auth, code = srs.GetAuthorityName(None), srs.GetAuthorityCode(None)
    name = srs.GetName() if hasattr(srs, "GetName") else ""
    return f"{auth}:{code} ({name})" if auth and code else (name or "unknown")


def _describe_dataset(path):
    from osgeo import gdal, ogr
    gdal.UseExceptions()
    ogr.UseExceptions()
    info = {"path": path}
    dataset = gdal.OpenEx(path, gdal.OF_READONLY | gdal.OF_VECTOR | gdal.OF_RASTER)
    info["driver"] = dataset.GetDriver().ShortName
    if dataset.RasterCount:
        transform = dataset.GetGeoTransform(can_return_null=True)
        info["raster"] = {
            "size": [dataset.RasterXSize, dataset.RasterYSize],
            "bands": [],
            "crs": _srs_label(dataset.GetSpatialRef()),
        }
        if transform:
            x0, dx, _, y0, _, dy = transform
            info["raster"]["pixel_size"] = [dx, dy]
            info["raster"]["extent"] = [x0, y0 + dy * dataset.RasterYSize,
                                        x0 + dx * dataset.RasterXSize, y0]
        for index in range(1, min(dataset.RasterCount, 12) + 1):
            band = dataset.GetRasterBand(index)
            entry = {"band": index,
                     "type": gdal.GetDataTypeName(band.DataType),
                     "nodata": band.GetNoDataValue()}
            try:
                lo, hi = band.ComputeRasterMinMax(True)
                entry["approx_min_max"] = [lo, hi]
            except Exception:
                pass
            info["raster"]["bands"].append(entry)
    layers = []
    for index in range(dataset.GetLayerCount()):
        layer = dataset.GetLayerByIndex(index)
        defn = layer.GetLayerDefn()
        entry = {"name": layer.GetName(),
                 "geometry": ogr.GeometryTypeToName(layer.GetGeomType()),
                 "features": layer.GetFeatureCount(),
                 "crs": _srs_label(layer.GetSpatialRef()),
                 "fields": [f"{defn.GetFieldDefn(i).GetName()}:"
                            f"{defn.GetFieldDefn(i).GetTypeName()}"
                            for i in range(min(defn.GetFieldCount(), 60))]}
        try:
            xmin, xmax, ymin, ymax = layer.GetExtent()
            entry["extent"] = [xmin, ymin, xmax, ymax]
        except Exception:
            pass
        layers.append(entry)
        if len(layers) >= 50:
            break
    if layers:
        info["vector_layers"] = layers
    return info


def main():
    proto = _claim_protocol_stream()
    sys.stdin.reconfigure(encoding="utf-8")
    write_lock = threading.Lock()

    def send(obj):
        with write_lock:
            # ASCII on the wire: no pipe can mis-decode an escaped character.
            proto.write(json.dumps(obj, ensure_ascii=True, default=str) + "\n")
            proto.flush()

    try:
        engine = Engine()
    except Exception:
        send({"event": "fatal", "error": traceback.format_exc()[-3000:]})
        proto.close()
        os._exit(2)
    send({"event": "ready", **engine.info()})

    inbox = queue.Queue()

    def reader():
        for raw in sys.stdin:
            inbox.put(raw)
        inbox.put(None)

    threading.Thread(target=reader, name="qgent-engine-stdin",
                     daemon=True).start()
    while True:
        try:
            raw = inbox.get(timeout=0.05)
        except queue.Empty:
            engine.app.processEvents()
            continue
        if raw is None:
            break
        raw = raw.strip()
        if not raw:
            continue
        try:
            request = json.loads(raw)
        except json.JSONDecodeError as exc:
            send({"id": None, "ok": False, "result": f"bad request: {exc}"})
            continue
        if request.get("tool") == "shutdown":
            send({"id": request.get("id"), "ok": True, "result": "bye"})
            break
        ok, result = engine.call(request.get("tool"), request.get("args") or {})
        send({"id": request.get("id"), "ok": ok, "result": result})
    proto.flush()
    # exitQgis() can hang or crash on Windows at teardown; nothing is left to
    # save here, so leave immediately.
    os._exit(0)


if __name__ == "__main__":
    main()
