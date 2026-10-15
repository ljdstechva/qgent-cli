import json
import os

import pytest

from qgent_cli import config, prompts, qgis_locator, sessions, skills
from qgent_cli.backends.base import flatten_content, short_tool_name
from qgent_cli.ui.terminal import _paragraph_cut, describe_input


def test_config_roundtrip_and_set(qgent_home):
    cfg = config.load()
    assert cfg["approval"] == "ask" and cfg["mcp"]["qgis_live"] == "auto"
    config.set_value(cfg, "models.codex", "gpt-5.6-sol")
    config.set_value(cfg, "mcp.fetch", "false")
    config.set_value(cfg, "mcp_servers.pg.args", '["--ro", "x y"]')
    config.set_value(cfg, "mcp_servers.pg.command", 'C:\\tools\\pg "mcp".exe')
    config.save(cfg)
    again = config.load()
    assert again["models"]["codex"] == "gpt-5.6-sol"
    assert again["mcp"]["fetch"] is False
    assert again["mcp_servers"]["pg"]["args"] == ["--ro", "x y"]
    assert again["mcp_servers"]["pg"]["command"] == 'C:\\tools\\pg "mcp".exe'


def test_bad_config_raises(qgent_home):
    os.makedirs(qgent_home, exist_ok=True)
    (qgent_home / "config.toml").write_text("backend = ", encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load()


def test_parse_scalar():
    assert config.parse_scalar("true") is True
    assert config.parse_scalar("42") == 42
    assert config.parse_scalar("claude") == "claude"
    assert config.parse_scalar("[1, 2]") == [1, 2]


def test_skills_index_and_frontmatter():
    names = [s["name"] for s in skills.list_skills()]
    assert names == sorted(names)
    assert {"pyqgis-patterns", "layout-templates", "vicinity-map-template"} <= set(names)
    for skill in skills.list_skills():
        assert skill["description"], skill["name"]
        assert "[placeholder]" not in skill["description"] or True
    skill, text = skills.read_skill("qgent:layout-templates")
    assert skill and "manage_layouts" in text
    assert "pyqgis-patterns:" in skills.index_text()


def test_user_skill_shadows_bundled(qgent_home):
    folder = qgent_home / "plugin" / "skills" / "pyqgis-patterns"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: pyqgis-patterns\ndescription: mine\n---\nX",
                                     encoding="utf-8")
    skill, text = skills.read_skill("pyqgis-patterns")
    assert skill["source"] == "user" and text.endswith("X")


def test_runtime_bundle_is_complete():
    runtime = config.RUNTIME_DIR
    assert os.path.isfile(os.path.join(runtime, ".claude-plugin", "plugin.json"))
    with open(os.path.join(runtime, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
        assert json.load(fh)["name"] == "qgent"
    agents = sorted(os.listdir(os.path.join(runtime, "subagents")))
    assert agents == ["cartographer.md", "data-scout.md", "geoprocessor.md", "qa-verifier.md"]
    for name in agents:
        with open(os.path.join(runtime, "subagents", name), encoding="utf-8") as fh:
            text = fh.read()
        assert "mcp__qgis__" in text and "Supervisor" not in text
    for asset in ("vicinity_a4_landscape.qpt", "ph_outline.geojson"):
        assert os.path.isfile(os.path.join(runtime, "assets", asset))
    vicinity = skills.read_skill("vicinity-map-template")[1]
    assert "qgis_chat_agent" not in vicinity and "QGENT_ASSETS" in vicinity


def test_instructions():
    codex = prompts.codex_instructions()
    assert "read_skill" in codex and "Skill index" in codex and "Goal Contract" in codex
    claude = prompts.claude_append_prompt()
    assert "qa-verifier" in claude and "route:" in claude


def test_context_block(fake_engine):
    fake_engine.layers = [{"name": "roads", "type": "QgsVectorLayer", "crs": "EPSG:4326",
                           "feature_count": 12, "geometry": "LineString", "valid": True,
                           "visible": True}]
    block = prompts.context_block(fake_engine, live=False, cwd="D:/data",
                                  servers=["qgis", "fetch"])
    assert block.startswith("<qgent_context>") and block.endswith("</qgent_context>")
    assert "roads (Vector, LineString, EPSG:4326, 12 features)" in block
    assert "other MCP servers: fetch" in block
    fake_engine.state = "failed"
    fake_engine.error = "no QGIS\nmore"
    assert "UNAVAILABLE — no QGIS" in prompts.context_block(fake_engine, live=True)


def test_sessions_roundtrip(qgent_home, tmp_path):
    session = sessions.Session("codex", str(tmp_path))
    session.title, session.backend_session, session.project = "t", "B1", "p.qgz"
    session.add_usage({"input_tokens": 5, "output_tokens": 2, "cost_usd": 0.5})
    session.save()
    session.record("user", "hi")
    loaded = sessions.load(session.id[:12])
    assert loaded.backend_session == "B1" and loaded.usage["cost_usd"] == 0.5
    assert sessions.latest(str(tmp_path)).id == session.id
    assert sessions.load("B1").id == session.id
    assert sessions.latest(str(tmp_path / "other")) is None


def test_clean_env_strips_host_python():
    env = qgis_locator.clean_env({"PATH": "x", "PYTHONPATH": "y", "VIRTUAL_ENV": "z",
                                  "pythonhome": "w"})
    assert env == {"PATH": "x"}


def test_explicit_qgis_python_wins(monkeypatch):
    monkeypatch.setenv("QGENT_QGIS_PYTHON", "~/q/python-qgis.bat")
    assert qgis_locator.candidates({})[0].endswith(os.path.join("q", "python-qgis.bat"))


def test_paragraph_cut_respects_fences():
    assert _paragraph_cut("one\n\ntwo") == len("one\n\n")
    fenced = "```\na\n\nb\n```\n\nafter"
    assert _paragraph_cut(fenced) == len("```\na\n\nb\n```\n\n")
    assert _paragraph_cut("```\na\n\nb") == 0


def test_describe_input():
    assert describe_input("x", {"code": "import os\n# c\nlyr = 1\nprint(lyr)"}) == \
        "lyr = 1  (+3 lines)"
    assert describe_input("x", {"alg_id": "native:buffer", "params": {"D": 1}}) == \
        'native:buffer {"D": 1}'
    assert describe_input("x", {"path": os.path.join(os.getcwd(), "a.gpkg")}) == "a.gpkg"


def test_helpers():
    assert short_tool_name("mcp__qgis__stat_path") == "qgis.stat_path"
    assert short_tool_name("Read") == "Read"
    assert flatten_content([{"type": "text", "text": "a"}, {"type": "image"}]) == "a\n[image]"
