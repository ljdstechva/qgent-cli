import json
import socket

from qgent_cli.bridge import hub_caller
from qgent_cli.hub import Hub


def test_safe_code_runs_without_prompt(fake_engine):
    asked = []
    hub = Hub(fake_engine, mode="ask", approver=lambda r: asked.append(r) or "yes")
    ok, text = hub.handle("execute_pyqgis", {"code": "print(QgsProject.instance().crs())"})
    assert ok and text == "ran execute_pyqgis"
    assert not asked


def test_destructive_code_asks_and_decline_blocks(fake_engine):
    requests = []
    hub = Hub(fake_engine, mode="ask",
              approver=lambda r: requests.append(r) or "no")
    ok, text = hub.handle("execute_pyqgis", {"code": "import os\nos.remove('a.shp')"})
    assert not ok
    assert "declined" in text
    assert requests and requests[0]["reasons"]
    assert ("execute_pyqgis", {"code": "import os\nos.remove('a.shp')"}) not in fake_engine.calls


def test_project_overwrite_alias_is_gated(fake_engine):
    hub = Hub(fake_engine, mode="deny")
    ok, text = hub.handle("execute_pyqgis",
                          {"code": "p = QgsProject.instance()\np.write()"})
    assert not ok and "approval" in text


def test_always_switches_session_to_auto(fake_engine):
    answers = iter(["always"])
    hub = Hub(fake_engine, mode="ask", approver=lambda r: next(answers))
    assert hub.handle("execute_pyqgis", {"code": "lyr.deleteFeatures([1])"})[0]
    assert hub.mode == "auto"
    # No second prompt: the iterator would raise StopIteration.
    assert hub.handle("execute_pyqgis", {"code": "lyr.deleteFeatures([2])"})[0]


def test_deny_mode_points_to_safe_route(fake_engine):
    hub = Hub(fake_engine, mode="deny")
    ok, text = hub.handle("execute_pyqgis",
                          {"code": "QgsVectorFileWriter.writeAsVectorFormatV3(l, p, c, o)"})
    assert not ok
    assert "run_processing" in text and "--yes" in text


def test_fatal_call_is_refused_even_in_auto(fake_engine):
    hub = Hub(fake_engine, mode="auto")
    ok, text = hub.handle("execute_pyqgis", {"code": "img = layer.previewAsImage(size)"})
    assert not ok and "crash" in text
    assert not fake_engine.calls


def test_save_project_over_existing_file_is_gated(fake_engine, tmp_path):
    existing = tmp_path / "a.qgz"
    existing.write_bytes(b"x")
    hub = Hub(fake_engine, mode="deny")
    assert not hub.handle("save_project", {"path": str(existing)})[0]
    assert hub.handle("save_project", {"path": str(tmp_path / "new.qgz")})[0]


def test_save_project_without_path_uses_current_file(tmp_path):
    from conftest import FakeEngine
    existing = tmp_path / "current.qgz"
    existing.write_bytes(b"x")
    engine = FakeEngine(project_file=str(existing))
    hub = Hub(engine, mode="deny")
    ok, text = hub.handle("save_project", {})
    assert not ok and "current.qgz" in text


def test_processing_output_overwrite_is_gated(fake_engine, tmp_path):
    existing = tmp_path / "out.gpkg"
    existing.write_bytes(b"x")
    hub = Hub(fake_engine, mode="deny")
    assert not hub.handle("run_processing", {"alg_id": "native:buffer", "params": {
        "INPUT": str(existing), "OUTPUT": str(existing)}})[0]
    assert hub.handle("run_processing", {"alg_id": "native:buffer", "params": {
        "INPUT": str(existing), "OUTPUT": str(tmp_path / "new.gpkg")}})[0]


def test_layout_delete_is_gated(fake_engine):
    hub = Hub(fake_engine, mode="deny")
    assert not hub.handle("manage_layouts", {"action": "delete", "layout": "A"})[0]
    assert hub.handle("manage_layouts", {"action": "export", "layout": "A",
                                         "path": "x.pdf"})[0]


def test_ask_user_without_terminal_returns_guidance(fake_engine):
    hub = Hub(fake_engine, mode="deny")
    ok, text = hub.handle("ask_user", {"question": "Which?", "options": ["a", "b"]})
    assert ok and "not available" in text


def test_ask_user_with_terminal(fake_engine):
    hub = Hub(fake_engine, asker=lambda q, o, a: o[1])
    ok, text = hub.handle("ask_user", {"question": "Which?", "options": ["a", "b"]})
    assert ok and text == "The user answered: b"


def test_skills_tools(fake_engine):
    hub = Hub(fake_engine)
    ok, listing = hub.handle("list_skills", {})
    assert ok and "pyqgis-patterns" in listing
    ok, text = hub.handle("read_skill", {"name": "qgent:pyqgis-patterns"})
    assert ok and text.startswith("---")
    ok, text = hub.handle("read_skill", {"name": "nope"})
    assert not ok and "Skills:" in text


def test_unknown_tool(fake_engine):
    ok, text = Hub(fake_engine).handle("rm_rf", {})
    assert not ok and "unknown tool" in text


def test_socket_roundtrip_and_token(fake_engine, monkeypatch):
    hub = Hub(fake_engine, mode="auto").start()
    try:
        for key, value in hub.bridge_env().items():
            monkeypatch.setenv(key, value)
        ok, text = hub_caller()("get_project_context", {})
        assert ok and json.loads(text)["project_crs"] == "EPSG:32651"
        # Wrong token is rejected.
        with socket.create_connection(("127.0.0.1", hub.port)) as sock:
            sock.sendall(b'{"token": "bad", "tool": "stat_path", "args": {}}\n')
            reply = json.loads(sock.makefile().readline())
        assert reply == {"ok": False, "error": "bad token"}
    finally:
        hub.stop()


def test_audit_log(fake_engine, tmp_path):
    log = tmp_path / "tools.jsonl"
    hub = Hub(fake_engine, mode="auto", log_path=str(log))
    hub.handle("stat_path", {"path": "x"})
    entry = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert entry["tool"] == "stat_path" and entry["ok"] is True
