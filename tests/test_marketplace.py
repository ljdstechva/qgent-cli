import json
import os

import pytest

from qgent_cli import config, marketplace, mcp_registry, skills
from qgent_cli.ui.market import parse_selection


def test_gis_score_separates_gis_from_lookalikes():
    assert marketplace.gis_score("Geospatial analysis: vector and raster GIS operations") >= 9
    assert marketplace.gis_score("Elevation, slope, terrain maps for any site") >= 6
    assert marketplace.gis_score("GEO brand visibility and AI citation evidence") < 3
    assert marketplace.gis_score("Georgia auto insurance averages") < 3
    assert marketplace.gis_score("Repo analysis for coding agents with dependency maps") < 3
    assert marketplace.gis_score("Shipping labels for AI agents: logistics") < 3
    # "report" must not trip the repo filter.
    assert marketplace.gis_score("Flood hazard report with raster DEM") >= 6


def test_rank_prefers_qgis_and_caps_keyword_stuffing():
    stuffed = marketplace.rank(40, 10, "geopandas geospatial gis raster")
    qgis = marketplace.rank(6, 10, "QGIS processing")
    assert qgis > stuffed - 6
    assert marketplace.rank(6, 1000) > marketplace.rank(6, 10)


def test_parse_selection():
    assert parse_selection("1, 3-5 3 9", 5) == [1, 3, 4, 5]
    assert parse_selection("", 5) == []
    assert parse_selection("5-3", 5) == [3, 4, 5]
    with pytest.raises(ValueError):
        parse_selection("one", 5)


def _item(server):
    return marketplace._mcp_item(server)


PYPI = {"name": "io.github.x/terrain-mcp", "description": "DEM slope and terrain analysis",
        "version": "1.0", "packages": [{
            "registryType": "pypi", "identifier": "terrain-kit", "version": "2.0",
            "transport": {"type": "stdio"},
            "packageArguments": [{"type": "positional", "value": "serve"},
                                 {"type": "named", "name": "--region", "isRequired": True}],
            "environmentVariables": [{"name": "API_KEY", "isRequired": True, "isSecret": True},
                                     {"name": "OPTIONAL", "isRequired": False}]}]}
REMOTE = {"name": "com.example/geo", "description": "Geocoding and routing",
          "remotes": [{"type": "streamable-http", "url": "https://geo.example/mcp",
                       "headers": [{"name": "Authorization", "isRequired": True,
                                    "isSecret": True}]}]}
OCI_ONLY = {"name": "com.example/oci-geo", "description": "raster GIS tools",
            "packages": [{"registryType": "oci", "identifier": "ghcr.io/x/geo",
                          "transport": {"type": "stdio"}}]}


def test_pypi_spec_and_requirements(monkeypatch):
    monkeypatch.setattr(marketplace.shutil, "which",
                        lambda name: {"uvx": "C:/bin/uvx.exe"}.get(name))
    item = _item(PYPI)
    assert item["installable"] and item["kinds"] == ["pypi"]
    option, needed = marketplace.requirements(item)
    assert option["kind"] == "pypi"
    assert [n["key"] for n in needed] == ["API_KEY", "--region"]
    with pytest.raises(marketplace.MarketplaceError, match="missing"):
        marketplace.mcp_spec(item, {"API_KEY": "k"})
    spec = marketplace.mcp_spec(item, {"API_KEY": "k", "--region": "PH"})
    assert spec["command"] == "C:/bin/uvx.exe"
    assert spec["args"] == ["terrain-kit==2.0", "serve", "--region", "PH"]
    assert spec["env"] == {"API_KEY": "k", "PYTHONIOENCODING": "utf-8"}
    assert spec["source"].startswith("mcp-registry:io.github.x/terrain-mcp")


def test_remote_spec_and_unavailable(monkeypatch):
    monkeypatch.setattr(marketplace.shutil, "which", lambda name: None)
    remote = _item(REMOTE)
    spec = marketplace.mcp_spec(remote, {"Authorization": "Bearer t"})
    assert spec["url"] == "https://geo.example/mcp" and spec["transport"] == "http"
    assert spec["headers"] == {"Authorization": "Bearer t"}
    oci = _item(OCI_ONLY)
    assert not oci["installable"]
    with pytest.raises(marketplace.MarketplaceError, match="docker"):
        marketplace.mcp_spec(oci)


def test_install_and_remove_mcp(qgent_home, monkeypatch):
    monkeypatch.setattr(marketplace.shutil, "which", lambda name: None)
    slug, _spec = marketplace.install_mcp(_item(REMOTE), {"Authorization": "x"})
    assert slug == "geo"
    servers = mcp_registry.servers(config.load(), {}, live=False)
    assert servers["geo"]["url"] == "https://geo.example/mcp"
    assert marketplace.remove_mcp("geo") and not marketplace.remove_mcp("geo")
    assert marketplace.server_slug({"id": "x/qgis"}) == "qgis_mcp"  # no clash


def test_search_mcp_filters_and_caches(qgent_home, monkeypatch):
    calls = []

    def fake_query(query):
        calls.append(query)
        return [PYPI, REMOTE, {"name": "com.x/seo", "description": "GEO brand citations"}]
    monkeypatch.setattr(marketplace, "_registry_query", fake_query)
    monkeypatch.setattr(marketplace.shutil, "which", lambda name: "x")
    items = marketplace.search_mcp()
    assert [i["id"] for i in items] == ["io.github.x/terrain-mcp", "com.example/geo"]
    count = len(calls)
    marketplace.search_mcp()
    assert len(calls) == count  # cached


def test_remote_wiring_claude_and_codex():
    server_map = {"geo": {"url": "https://geo.example/mcp", "transport": "http",
                          "headers": {"Authorization": "Bearer s3cret"}, "env": {},
                          "timeout": 60, "startup": 30}}
    claude = mcp_registry.claude_config(server_map)["mcpServers"]["geo"]
    assert claude == {"type": "http", "url": "https://geo.example/mcp",
                      "headers": {"Authorization": "Bearer s3cret"}}
    args, env = mcp_registry.codex_args(server_map)
    assert "s3cret" not in " ".join(args)
    assert 'mcp_servers.geo.url="https://geo.example/mcp"' in args
    assert any(a.startswith("mcp_servers.geo.env_http_headers=") for a in args)
    assert list(env.values()) == ["Bearer s3cret"]


def test_install_skill(qgent_home, monkeypatch):
    files = {"skills/gdal/SKILL.md": b"---\nname: gdal\ndescription: GDAL raster tips\n---\nBody",
             "skills/gdal/scripts/x.py": b"print(1)", "skills/other/SKILL.md": b"x"}
    monkeypatch.setattr(marketplace, "_repo_tree", lambda source, headers: list(files))
    monkeypatch.setattr(marketplace, "_github_headers", lambda: {})
    monkeypatch.setattr(marketplace, "_get", lambda url, timeout=0, headers=None:
                        files[url.split("/HEAD/", 1)[1]])
    item = {"id": "o/r/gdal", "name": "gdal", "source": "o/r", "path": "skills/gdal/SKILL.md"}
    name, target, count = marketplace.install_skill(item)
    assert (name, count) == ("gdal", 2)
    assert os.path.isfile(os.path.join(target, "scripts", "x.py"))
    with open(os.path.join(target, ".qgent-source.json"), encoding="utf-8") as handle:
        assert json.load(handle)["source"] == "o/r"
    installed = {s["name"]: s for s in skills.list_skills()}
    assert installed["gdal"]["source"] == "user"
    assert config.user_plugin_args() == ["--plugin-dir", marketplace.user_plugin_dir()]
    assert marketplace.remove_skill("gdal") and not marketplace.remove_skill("gdal")


def test_remove_skill_cannot_escape_the_skills_folder(qgent_home):
    marketplace.ensure_user_plugin()
    sentinel = qgent_home / "plugin" / ".claude-plugin" / "plugin.json"
    outside = qgent_home / "keep.txt"
    outside.write_text("x", encoding="utf-8")
    for name in ("..", "../..", "../../..", ".", "", "a/../..", "C:\\Windows", "/etc"):
        assert marketplace.remove_skill(name) is False, name
    assert sentinel.exists() and outside.exists()
    with pytest.raises(marketplace.MarketplaceError):
        marketplace.skill_dir("..")
    # A folder without SKILL.md is not a skill either.
    (qgent_home / "plugin" / "skills" / "notaskill").mkdir()
    assert marketplace.remove_skill("notaskill") is False


def test_install_skill_sanitises_hostile_names_and_paths(qgent_home, monkeypatch):
    files = {"s/../../evil/SKILL.md": b"---\nname: x\n---\n",
             "s/SKILL.md": b"---\nname: x\ndescription: ok\n---\n",
             "s/sub/../../escape.txt": b"no"}
    monkeypatch.setattr(marketplace, "_repo_tree", lambda source, headers: list(files))
    monkeypatch.setattr(marketplace, "_github_headers", lambda: {})
    monkeypatch.setattr(marketplace, "_get", lambda url, timeout=0, headers=None:
                        files[url.split("/HEAD/", 1)[1].replace("%2F", "/")])
    name, target, _ = marketplace.install_skill(
        {"id": "o/r/..", "name": "..", "source": "o/r", "path": "s/SKILL.md"})
    assert name == "skill" and os.path.dirname(target) == os.path.realpath(
        marketplace.user_skills_dir())
    assert not (qgent_home / "plugin" / "escape.txt").exists()
    assert not (qgent_home / "plugin" / "skills" / "evil").exists()


def test_hostile_sources_never_reach_github(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("no request may be made")
    monkeypatch.setattr(marketplace, "_get", boom)
    monkeypatch.setattr(marketplace, "_get_json", boom)
    for source in ("o/r/../../user", "../x", "o", "o/r?x=1", "o/r#", "https://evil/x"):
        with pytest.raises(marketplace.MarketplaceError):
            marketplace.locate_skill(source, "gdal", headers={})
    with pytest.raises(marketplace.MarketplaceError):
        marketplace.locate_skill("o/r", "../gdal", headers={})


def test_same_name_from_another_repo_does_not_replace(qgent_home, monkeypatch):
    files = {"s/SKILL.md": b"---\nname: gdal\ndescription: a\n---\n"}
    monkeypatch.setattr(marketplace, "_repo_tree", lambda source, headers: list(files))
    monkeypatch.setattr(marketplace, "_github_headers", lambda: {})
    monkeypatch.setattr(marketplace, "_get", lambda url, timeout=0, headers=None:
                        files[url.split("/HEAD/", 1)[1]])
    first = marketplace.install_skill({"id": "a/r/gdal", "name": "gdal", "source": "a/r",
                                       "path": "s/SKILL.md"})
    second = marketplace.install_skill({"id": "b/r/gdal", "name": "gdal", "source": "b/r",
                                        "path": "s/SKILL.md"})
    again = marketplace.install_skill({"id": "a/r/gdal", "name": "gdal", "source": "a/r",
                                       "path": "s/SKILL.md"})
    assert (first[0], second[0], again[0]) == ("gdal", "gdal-b", "gdal")


def test_market_tables_survive_markup_in_summaries(monkeypatch):
    from rich.console import Console

    from qgent_cli.ui import market
    hostile = dict(REMOTE, description="Geocoding [/x] and [bold]routing[/bold] for GIS")
    monkeypatch.setattr(marketplace, "search_mcp", lambda q, refresh=False: [_item(hostile)])
    console = Console(record=True, width=140)
    monkeypatch.setattr(console, "input", lambda *_a, **_k: "")
    market.browse_mcp(console)
    assert "[/x]" in console.export_text()


def test_frontmatter_unquotes_yaml():
    meta = skills._frontmatter('---\nname: "q"\ndescription: "Use when \\u2014 it\'s"\n'
                               "other: 'it''s'\n---\n")
    assert meta == {"name": "q", "description": "Use when \u2014 it's", "other": "it's"}


def test_locate_skill_by_frontmatter(monkeypatch):
    tree = ["pkg/source/qgis-impl/vector/SKILL.md", "README.md"]

    def fake_get(url, timeout=0, headers=None):
        if url.endswith("pkg/source/qgis-impl/vector/SKILL.md"):
            return b"---\nname: qgis-impl-vector\ndescription: vector analysis\n---\n"
        raise OSError("404")
    monkeypatch.setattr(marketplace, "_get", fake_get)
    monkeypatch.setattr(marketplace, "_repo_tree", lambda source, headers: tree)
    path, text, _ = marketplace.locate_skill("o/r", "qgis-impl-vector", headers={})
    assert path == tree[0] and "vector analysis" in text
