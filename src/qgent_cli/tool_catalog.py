"""MCP tool definitions for QGent's ``qgis`` server (stdlib only).

Engine tools run inside the headless QGIS engine; hub tools (ask_user,
list_skills, read_skill) are answered by the qgent process itself. Names
match the QGent plugin's where the behaviour matches, so the shared skills
and subagents work unchanged.
"""

_OBJ = {"type": "object"}


def _schema(properties=None, required=None):
    schema = {"type": "object", "properties": properties or {}}
    if required:
        schema["required"] = required
    return schema


ENGINE_TOOLS = [
    {
        "name": "execute_pyqgis",
        "description": (
            "Run PyQGIS in the headless QGIS engine and return captured "
            "stdout/stderr. The workhorse: prefer ONE script doing a whole "
            "workflow over many small calls. Variables persist between "
            "calls for the session. Pre-injected: QgsProject, processing, "
            "all qgis.core Qgs* classes, QGENT_ASSETS, and iface — a "
            "headless stand-in (activeLayer, setActiveLayer, "
            "zoomToActiveLayer, mapCanvas extent/CRS drive the view "
            "render_map_snapshot draws; no windows or dialogs). print() the "
            "facts you need as evidence. Destructive code (deleting files "
            "or features, overwriting files or the project) asks the user "
            "first. Memory layers live only in this engine: write anything "
            "worth keeping to a GeoPackage."),
        "inputSchema": _schema(
            {"code": {"type": "string", "description": "PyQGIS source to exec()."}},
            ["code"]),
    },
    {
        "name": "get_project_context",
        "description": (
            "Project state as JSON: project file, CRS, unsaved changes, every "
            "layer (name, id, type, CRS, path, feature count, geometry, "
            "visibility), active layer, print layouts and the current view "
            "extent. Cheap — use it to ground layer and field names."),
        "inputSchema": _schema(),
    },
    {
        "name": "open_project",
        "description": "Open a .qgz/.qgs project in the engine (replaces the current one).",
        "inputSchema": _schema(
            {"path": {"type": "string", "description": "Project file path."}},
            ["path"]),
    },
    {
        "name": "new_project",
        "description": ("Start an empty project, optionally with a CRS "
                        "(e.g. 'EPSG:32651') and title."),
        "inputSchema": _schema({"crs": {"type": "string"},
                                "title": {"type": "string"}}),
    },
    {
        "name": "save_project",
        "description": (
            "Save the project to path (.qgz/.qgs), or to its current file "
            "when path is omitted. Overwriting an existing file asks the "
            "user. Memory layers are saved without their features — export "
            "them to a GeoPackage first."),
        "inputSchema": _schema({"path": {"type": "string"}}),
    },
    {
        "name": "load_layer",
        "description": (
            "Add a vector or raster layer to the project from a file path "
            "(Shapefile, GeoPackage, GeoJSON, KML, CSV, GeoTIFF, ...; use "
            "'file.gpkg|layername=roads' for one GeoPackage table) or a "
            "provider URI with provider set (e.g. provider 'wms' for an XYZ "
            "URI). Returns the layer summary; the first layer sets the "
            "project CRS when it has none."),
        "inputSchema": _schema(
            {"path": {"type": "string", "description": "File path or provider URI."},
             "name": {"type": "string", "description": "Layer name (default: file name)."},
             "kind": {"type": "string", "enum": ["auto", "vector", "raster"]},
             "provider": {"type": "string",
                          "description": "QGIS provider key (ogr, gdal, wms, "
                                         "delimitedtext, ...)."}},
            ["path"]),
    },
    {
        "name": "inspect_dataset",
        "description": (
            "Describe GIS data without loading it: for a file — driver, "
            "vector layers (geometry, feature count, CRS, fields, extent) "
            "and raster bands (size, type, nodata, CRS, pixel size, extent); "
            "for a folder — the GIS files inside it (3 levels deep). Use it "
            "first on data the user points at."),
        "inputSchema": _schema(
            {"path": {"type": "string", "description": "File or folder path."}},
            ["path"]),
    },
    {
        "name": "run_processing",
        "description": (
            "Run a QGIS Processing algorithm: processing.run(alg_id, params). "
            "OUTPUT defaults to TEMPORARY_OUTPUT; memory outputs are added "
            "to the project automatically (name them with output_names "
            "{OUTPUT: 'name'}); file outputs are reported by path and added "
            "when load_outputs is true. Layer parameters accept layer names, "
            "ids or file paths. Check parameters with algorithm_help first."),
        "inputSchema": _schema(
            {"alg_id": {"type": "string", "description": "e.g. 'native:buffer'."},
             "params": {"type": "object", "description": "Algorithm parameters."},
             "load_outputs": {"type": "boolean"},
             "output_names": {"type": "object",
                              "description": "Output key -> layer name."}},
            ["alg_id", "params"]),
    },
    {
        "name": "list_algorithms",
        "description": ("Search Processing algorithms by words in id/name/tags/"
                        "group (e.g. 'buffer', 'raster fill sinks')."),
        "inputSchema": _schema(
            {"query": {"type": "string"},
             "provider": {"type": "string",
                          "description": "native, gdal, qgis, grass, ..."},
             "limit": {"type": "integer"}}),
    },
    {
        "name": "algorithm_help",
        "description": ("Parameters (name, type, optional, default, enum "
                        "options) and outputs of one Processing algorithm."),
        "inputSchema": _schema({"alg_id": {"type": "string"}}, ["alg_id"]),
    },
    {
        "name": "get_layer_features",
        "description": (
            "Sample attribute rows from a vector layer (capped at 200), "
            "optionally filtered by a QGIS expression and with WKT geometry. "
            "Never dump a whole table into context."),
        "inputSchema": _schema(
            {"layer": {"type": "string", "description": "Layer name or id (blank = active)."},
             "limit": {"type": "integer"},
             "filter_expr": {"type": "string"},
             "with_geometry": {"type": "boolean"}}),
    },
    {
        "name": "render_map_snapshot",
        "description": (
            "Render the visible project layers to a PNG and return its path "
            "— read the image to check your work. extent: [xmin, ymin, xmax, "
            "ymax] in the view CRS or 'layer:Name'; layers: names to draw "
            "instead of the visible ones; set_view keeps that extent as the "
            "view."),
        "inputSchema": _schema(
            {"width": {"type": "integer"}, "height": {"type": "integer"},
             "extent": {"description": "[xmin,ymin,xmax,ymax] or 'layer:Name'."},
             "layers": {"type": "array", "items": {"type": "string"}},
             "path": {"type": "string", "description": "Output .png (optional)."},
             "set_view": {"type": "boolean"}}),
    },
    {
        "name": "stat_path",
        "description": (
            "Read-only metadata for one path: exists, file/dir, size, ISO "
            "mtime. Use it to verify every exported file."),
        "inputSchema": _schema({"path": {"type": "string"}}, ["path"]),
    },
    {
        "name": "layout_info",
        "description": (
            "Read-only print-layout inspector. action='list': project "
            "layouts, presets and every .qpt template QGIS can see. "
            "action='describe' with layout=<name> or template=<name|.qpt>: "
            "pages, items (id, type, rect_mm [x, y, w, h], text, map "
            "scale/CRS), [placeholders] and design issues (clipped text, "
            "overflowing legend/table, off-page or overlapping items). "
            "action='render' with layout (page, dpi optional): PNG preview "
            "path — read it before calling a layout done."),
        "inputSchema": _schema(
            {"action": {"type": "string", "enum": ["list", "describe", "render"]},
             "layout": {"type": "string"}, "template": {"type": "string"},
             "page": {"type": "integer"}, "dpi": {"type": "number"}},
            ["action"]),
    },
    {
        "name": "manage_layouts",
        "description": (
            "Build and manage print layouts and .qpt templates without "
            "writing PyQGIS — read the layout-templates skill for the spec. "
            "build: spec={name, mode:'create'|'update', preset?:'side_panel'|"
            "'title_strip'|'report_figure', page?:{size:'A3', orientation:"
            "'landscape'}, style?, items:[{id, type, rect:[x,y,w,h] mm, ...}]"
            "} with types map{extent:'canvas'|'layer:Name'|[xmin,ymin,xmax,"
            "ymax], scale?, crs?, layers?, grid?, overview?}, label{text "
            "with [Placeholder] or [% expression %]}, legend, scalebar, "
            "north_arrow, picture, rectangle/ellipse, line, table, "
            "attribute_table. save_template: layout, name? -> user template "
            "folder (write fill-ins as [Field Name] placeholders). "
            "create_from_template: template or source_layout, layout_name, "
            "fill:{placeholder: text}, map?. export: layout, absolute path "
            "(.pdf/.png/.jpg/.tif/.svg), dpi?. delete: layout. 'open' is "
            "unavailable headless. replace/overwrite/delete ask the user."),
        "inputSchema": _schema(
            {"action": {"type": "string",
                        "enum": ["build", "save_template", "create_from_template",
                                 "export", "open", "delete"]},
             "spec": _OBJ, "layout": {"type": "string"},
             "name": {"type": "string"}, "folder": {"type": "string"},
             "template": {"type": "string"}, "source_layout": {"type": "string"},
             "layout_name": {"type": "string"}, "fill": _OBJ,
             "variables": _OBJ, "images": _OBJ, "map": _OBJ,
             "path": {"type": "string"}, "dpi": {"type": "number"},
             "overwrite": {"type": "boolean"}, "replace": {"type": "boolean"}},
            ["action"]),
    },
]

HUB_TOOLS = [
    {
        "name": "ask_user",
        "description": (
            "Ask the user one structured question in the terminal and wait "
            "for the answer. Only when unresolved ambiguity would materially "
            "change the GIS outcome or safety; offer 2-5 concrete options. "
            "Never end a turn with a plain-text question you need answered."),
        "inputSchema": _schema(
            {"question": {"type": "string", "minLength": 1, "maxLength": 300},
             "options": {"type": "array", "minItems": 2, "maxItems": 5,
                         "items": {"type": "string", "minLength": 1, "maxLength": 80}},
             "allow_other": {"type": "boolean", "default": True}},
            ["question", "options"]),
    },
    {
        "name": "list_skills",
        "description": "List QGent's bundled GIS skills (playbooks) with one-line descriptions.",
        "inputSchema": _schema(),
    },
    {
        "name": "read_skill",
        "description": (
            "Return a bundled skill's full playbook (SKILL.md). Read the "
            "matching skill before PyQGIS, Processing, cartography, layout "
            "or Philippine-compliance map work."),
        "inputSchema": _schema({"name": {"type": "string"}}, ["name"]),
    },
]

ALL_TOOLS = ENGINE_TOOLS + HUB_TOOLS
ENGINE_TOOL_NAMES = frozenset(tool["name"] for tool in ENGINE_TOOLS)
HUB_TOOL_NAMES = frozenset(tool["name"] for tool in HUB_TOOLS)
TOOL_NAMES = ENGINE_TOOL_NAMES | HUB_TOOL_NAMES
