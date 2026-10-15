import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))


@pytest.fixture(autouse=True)
def qgent_home(tmp_path, monkeypatch):
    """Every test gets its own ~/.qgent."""
    home = tmp_path / "qgent_home"
    monkeypatch.setenv("QGENT_HOME", str(home))
    return home


class FakeEngine:
    """Records calls; answers like the real engine's JSON tools."""

    def __init__(self, project_file=None):
        self.calls = []
        self.state = "ready"
        self.info = {"qgis_version": "3.44.8-Solothurn"}
        self.error = ""
        self.last_project = None
        self.project_file = project_file
        self.layers = []

    def call(self, tool, args=None, timeout=None):
        self.calls.append((tool, args))
        if tool == "get_project_context":
            import json
            return True, json.dumps({
                "project_file": self.project_file, "project_crs": "EPSG:32651",
                "unsaved_changes": True, "layers": self.layers,
                "active_layer": None, "layouts": []})
        return True, f"ran {tool}"


@pytest.fixture
def fake_engine():
    return FakeEngine()
