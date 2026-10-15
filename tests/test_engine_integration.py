"""Real headless-QGIS tests. Skipped when no QGIS installation is found.

Run just these with:  pytest tests/test_engine_integration.py -v
"""
import json
import os

import pytest

from qgent_cli import config, qgis_locator
from qgent_cli.engine_client import EngineClient
from qgent_cli.hub import Hub

SITES = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"name": "Plant A", "kind": "industrial"},
     "geometry": {"type": "Point", "coordinates": [125.17, 6.11]}},
    {"type": "Feature", "properties": {"name": "Plant B", "kind": "commercial"},
     "geometry": {"type": "Point", "coordinates": [125.19, 6.13]}}]}


@pytest.fixture(scope="module")
def qgis_info():
    try:
        return qgis_locator.resolve(config.load())
    except qgis_locator.QgisNotFound as exc:
        pytest.skip(f"QGIS not installed: {exc}")


@pytest.fixture
def engine(qgis_info, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Reuse the real cache so the module doesn't re-probe per test.
    client = EngineClient(config.load())
    client.start()
    yield client
    client.stop()


def _json(result):
    ok, text = result
    assert ok, text
    return json.loads(text)


def test_full_workflow(engine, tmp_path):
    data = tmp_path / "sites.geojson"
    data.write_text(json.dumps(SITES), encoding="utf-8")
    described = _json(engine.call("inspect_dataset", {"path": str(data)}))
    assert described["vector_layers"][0]["features"] == 2

    layer = _json(engine.call("load_layer", {"path": "sites.geojson"}))
    assert layer["feature_count"] == 2 and layer["crs"] == "EPSG:4326"

    out = tmp_path / "buffers.gpkg"
    run = _json(engine.call("run_processing", {
        "alg_id": "native:buffer",
        "params": {"INPUT": "sites", "DISTANCE": 0.001, "OUTPUT": str(out)},
        "load_outputs": True, "output_names": {"OUTPUT": "buffers"}}))
    assert out.exists() and run["loaded_layers"][0]["name"] == "buffers"

    memory = _json(engine.call("run_processing", {
        "alg_id": "native:centroids", "params": {"INPUT": "buffers"},
        "output_names": {"OUTPUT": "centres"}}))
    assert memory["loaded_layers"][0]["provider"] == "memory"

    ok, text = engine.call("execute_pyqgis", {"code": "n = 7"})
    assert ok
    ok, text = engine.call("execute_pyqgis", {
        "code": "print(n * 6, iface.activeLayer().name(), len(QgsProject.instance().mapLayers()))"})
    assert ok and text.strip() == "42 centres 3", text

    ok, text = engine.call("execute_pyqgis", {"code": "1/0"})
    assert not ok and "ZeroDivisionError" in text

    snap = _json(engine.call("render_map_snapshot", {"width": 300, "height": 200}))
    assert os.path.getsize(snap["snapshot_path"]) > 500
    assert set(snap["layers"]) == {"sites", "buffers", "centres"}

    ctx = _json(engine.call("get_project_context", {}))
    assert ctx["layer_count"] == 3 and ctx["project_crs"] == "EPSG:4326"

    help_ = _json(engine.call("algorithm_help", {"alg_id": "native:buffer"}))
    assert "DISTANCE" in {p["name"] for p in help_["parameters"]}
    ok, listing = engine.call("list_algorithms", {"query": "buffer", "limit": 3})
    assert ok and "native:buffer" in listing

    layout = _json(engine.call("manage_layouts", {"action": "build", "spec": {
        "name": "Fig", "preset": "report_figure",
        "items": [{"id": "map_main", "type": "map", "extent": "canvas"}]}}))
    assert layout["layout"] == "Fig"
    pdf = tmp_path / "fig.pdf"
    exported = _json(engine.call("manage_layouts", {"action": "export",
                                                    "layout": "Fig", "path": str(pdf)}))
    assert pdf.exists() and exported["files"][0]["size_bytes"] > 1000
    ok, text = engine.call("manage_layouts", {"action": "open", "layout": "Fig"})
    assert not ok and "headless" in text

    saved = _json(engine.call("save_project", {"path": str(tmp_path / "p.qgz")}))
    assert saved["layers"] == 3 and engine.last_project == str(tmp_path / "p.qgz")


def test_crash_recovery_reopens_project(engine, tmp_path):
    data = tmp_path / "sites.geojson"
    data.write_text(json.dumps(SITES), encoding="utf-8")
    _json(engine.call("load_layer", {"path": str(data)}))
    _json(engine.call("save_project", {"path": str(tmp_path / "keep.qgz")}))
    # Simulate a native crash between calls: the next call restarts the
    # engine transparently and reopens the last saved project.
    engine._proc.kill()
    engine._proc.wait(10)
    ok, text = engine.call("get_project_context", {})
    assert ok, text
    assert "restarted and reopened" in text
    ctx = json.loads(text[text.index("{"):])
    assert ctx["layer_count"] == 1 and ctx["project_file"].endswith("keep.qgz")


def test_crash_during_call_is_reported(engine):
    ok, text = engine.call("execute_pyqgis", {"code": "import os\nos._exit(3)"})
    assert not ok and "crashed (exit code 3)" in text
    ok, text = engine.call("execute_pyqgis", {"code": "print('back')"})
    assert ok and text.strip().endswith("back")


def test_hub_gate_with_real_engine(engine, tmp_path):
    target = tmp_path / "victim.txt"
    target.write_text("keep me", encoding="utf-8")
    hub = Hub(engine, mode="deny")
    ok, text = hub.handle("execute_pyqgis",
                          {"code": f"import os\nos.remove({str(target)!r})"})
    assert not ok and "approval" in text and target.exists()
    ok, _ = Hub(engine, mode="auto").handle(
        "execute_pyqgis", {"code": f"import os\nos.remove({str(target)!r})"})
    assert ok and not target.exists()
