import json

import pytest

from qgent_cli import router, subagents

CODEX_CACHE = {"models": [
    {"slug": "gpt-6.1-sol", "description": "Latest workhorse model for coding and everyday work.",
     "visibility": "list", "priority": 1,
     "supported_reasoning_levels": [{"effort": e} for e in
                                    ("low", "medium", "high", "xhigh", "max", "ultra")]},
    {"slug": "gpt-6-astra", "description": "Frontier intelligence for the most demanding work.",
     "visibility": "list", "priority": 2,
     "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh")]},
    {"slug": "gpt-6-luna", "description": "Fast and affordable model for easier tasks.",
     "visibility": "list", "priority": 4,
     "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high")]},
    {"slug": "gpt-5.6-sol", "description": "Older generation workhorse model.",
     "visibility": "list", "priority": 5, "supported_reasoning_levels": []},
    {"slug": "secret", "description": "hidden", "visibility": "hide", "priority": 0},
]}


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    (tmp_path / "models_cache.json").write_text(json.dumps(CODEX_CACHE), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    return tmp_path


def test_codex_catalog_tiers(codex_home):
    cat = router.codex_catalog()
    assert [e["id"] for e in cat] == ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna"]
    assert {e["id"]: e["tier"] for e in cat} == {
        "gpt-6.1-sol": "strong", "gpt-6-astra": "frontier", "gpt-6-luna": "fast"}


def test_codex_catalog_missing_cache_uses_user_model(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert [e["id"] for e in router.codex_catalog("my-model")] == ["my-model"]


@pytest.mark.parametrize("prompt,task,low,high", [
    ("what CRS is sites.geojson?", "inspection", 1, 1),
    ("hello there", "conversation", 1, 1),
    ("buffer the wells by 250 m", "analysis", 2, 2),
    ("Build an A3 flood hazard vicinity map with legend and scale bar, then export "
     "it to PDF and also save a template", "cartography", 4, 5),
    ("reproject every shapefile in this folder to EPSG:32651", "batch", 3, 5),
])
def test_classify(prompt, task, low, high):
    got_task, complexity = router.classify(prompt)
    assert got_task == task
    assert low <= complexity <= high


def test_destructive_requests_get_a_capable_model(codex_home, monkeypatch):
    assert router.classify("Delete the file sites_utm.gpkg")[1] >= 2
    assert router.classify("Can you delete sites.gpkg?")[1] >= 2
    assert router.classify("Delete sites.gpkg?")[1] >= 2
    assert router.classify("please overwrite roads.shp")[1] >= 2
    assert router.classify("what does delete do?")[1] == 1

    def cheap(*_a, **_k):  # the AI router under-sizes it
        return {"task": "question", "complexity": 1, "supervisor_model": "gpt-6-luna",
                "supervisor_effort": "low", "scout_model": "gpt-6-luna",
                "executor_model": "gpt-6-luna", "verifier_model": "gpt-6-luna",
                "delegate": False, "reason": "trivial"}
    monkeypatch.setattr(router, "_run_codex_router", cheap)
    decision = _router("ai").decide("Delete sites_utm.gpkg with os.remove", "codex", "codex")
    assert decision["complexity"] == 2
    assert decision["roles"]["supervisor"]["model"] == "gpt-6.1-sol"
    assert "changes data" in decision["reason"]


@pytest.mark.parametrize("prompt", [
    "list the empty layers and delete them",
    "show the duplicates then remove them",
    "Is sites.gpkg needed? If not, delete it",
    "Can you delete sites.gpkg?",
    "Delete sites.gpkg?",
    # second fresh-verifier counterexamples
    "Can you go ahead with dropping the empty fields?",
    "Moving rivers.shp into the archive folder please",
    "wiping the attribute table is fine",
    "What layers are empty? Delete those.",
    "How about you delete the temp layers now",
    "Explain the plan, then delete sites.gpkg",
    "Tell me about the layers and remove the empty ones",
    "purge the scratch layers",
    "modify the POP field values to be integers",
    "the empty fields need dropping",
    "moved files: put rivers.shp in archive",
    "What's in sites.gpkg? Remove anything older than 2020.",
    "clear all features from the roads layer",
    "destroy the backup folder",
    "do a clean-up of the scratch layers",
    "over-write sites.gpkg with the clipped version",
    # third fresh-verifier counterexamples (synonyms no keyword list names)
    "nuke the roads layer", "get rid of the null geometries", "set NAME to null for all rows",
    "show me the nulls then fill them with 0", "strip the Z values from the lines",
    "trim the attribute table to 3 fields", "prune the duplicate vertices",
    "dedupe the wells layer", "archive the old layers", "throw away the scratch layers",
    "blank out the OWNER column", "write the changes back to the database",
    "fill the nulls in POP with 0", "change the values in CLASS from A to B",
    "fix the invalid geometries in place", "toss the duplicates", "ditch the old columns",
    "scrap layer x", "trash the temp files", "kill the empty fields",
    "set all ELEV values below 0 to 0", "assign CLASS = 3 to the selected features",
    "swap the X and Y columns", "null out the bad readings", "zero out the negative values",
    "remvoe the duplicates", "pls nuke it", "rm parcels.shp", "del the layer",
    "apply the changes to the layer", "flatten the hierarchy and dump the old groups",
    "decimate the point cloud in place", "collapse duplicate rows",
    "squash the attribute values to lowercase", "lowercase all names in NAME",
    "geocode the addresses and write lat/lon into the table",
    "what if you deleted the roads layer", "what about removing the temp layers",
    "How bout dropping the old field",
])
def test_floor_holds_for_every_phrasing(codex_home, prompt):
    decision = _router("heuristic").decide(prompt, "codex", "codex")
    assert decision["complexity"] >= 2, prompt
    assert decision["roles"]["supervisor"]["model"] != "gpt-6-luna", prompt
    claude = _router("heuristic").decide(prompt, "claude", "claude")
    assert claude["roles"]["supervisor"]["model"] != "haiku", prompt


def test_follow_up_that_deletes_is_raised(codex_home):
    sticky = _router("heuristic")
    first = sticky.decide("what layers are loaded?", "codex", "codex")
    assert first["complexity"] == 1  # cheap, read-only first turn
    again = sticky.decide("yes, delete it", "codex", "codex")
    assert again["source"] == "follow-up"
    assert again["roles"]["supervisor"]["model"] != "gpt-6-luna"
    for reply in ("yes, dropping them is fine", "yep, moving it now", "yes purge them",
                  "yes, get rid of them", "yes", "do it", "proceed", "option 2", "1",
                  "ok, set them to null", "yes commit", "fine, archive them",
                  "ok do it on roads"):
        fresh = _router("heuristic")
        fresh.decide("what layers are loaded?", "codex", "codex")
        assert fresh.decide(reply, "codex", "codex")["roles"]["supervisor"]["model"] \
            != "gpt-6-luna", reply
    # Plain how-to / definition questions stay cheap.
    for question in ("how do I delete a field?", "what does delete do?",
                     "what happens if I remove a layer?"):
        assert router.classify(question)[1] == 1, question


@pytest.mark.parametrize("prompt", [
    "what layers are loaded?", "list the layers", "what CRS is sites.geojson?",
    "how many features are in roads?", "show the attribute table of parcels",
    "which fields does parcels have?", "hello", "thanks!", "describe the project",
    "how do I delete a field?", "what does the erase tool do?",
])
def test_read_only_questions_stay_cheap(codex_home, prompt):
    decision = _router("heuristic").decide(prompt, "codex", "codex")
    assert decision["complexity"] == 1, prompt
    assert decision["roles"]["supervisor"] == {"model": "gpt-6.1-sol", "effort": "low"}, prompt


@pytest.mark.parametrize("prompt", [
    # fourth fresh-verifier round: question-shaped instructions
    "what are the empty layers delete them", "which layers are empty nuke them",
    "is the crs wrong if so set it to 32651", "how to delete roads.shp do it now",
    "give me a new column named code", "who cares just do it",
    "what are the empty layers\n\nDelete them.", "what are the empty layers — delete them",
    "how 'bout you nuke the temp layer", "display the roads layer in red",
    "what is the difference lol just delete roads", "why not just get rid of the empty layers?",
])
@pytest.mark.parametrize("backend,fast", [("codex", "gpt-6-luna"), ("claude", "haiku")])
def test_supervisor_is_never_fast_tier(codex_home, monkeypatch, prompt, backend, fast):
    def picks_fast(*_a, **_k):  # even if the AI router chooses the fast tier
        return {"task": "question", "complexity": 2, "supervisor_model": fast,
                "supervisor_effort": "low", "scout_model": fast, "executor_model": fast,
                "verifier_model": fast, "delegate": False, "reason": "cheap"}
    monkeypatch.setattr(router, "_run_codex_router", picks_fast)
    monkeypatch.setattr(router, "_run_claude_router", picks_fast)
    for mode in ("auto", "ai", "heuristic"):
        decision = _router(mode).decide(prompt, backend, backend)
        assert decision["roles"]["supervisor"]["model"] != fast, (mode, prompt)


@pytest.mark.parametrize("raw", [[], "str", None, 5, {"complexity": "3"}])
def test_bad_router_output_falls_back(codex_home, monkeypatch, raw):
    monkeypatch.setattr(router, "_run_codex_router", lambda *_a, **_k: raw)
    decision = _router("ai").decide("buffer the rivers", "codex", "codex")
    assert decision["source"] == "heuristic"
    monkeypatch.setattr(router, "_run_codex_router",
                        lambda *_a, **_k: (_ for _ in ()).throw(AttributeError("x")))
    assert _router("ai").decide("buffer the rivers", "codex", "codex")["source"] == "heuristic"


def test_fast_only_current_generation_keeps_an_older_strong_model(tmp_path, monkeypatch):
    cache = {"models": [
        {"slug": "tiny", "description": "Fast model", "visibility": "list", "priority": 1},
        {"slug": "old-strong", "description": "Older generation workhorse model.",
         "visibility": "list", "priority": 5}]}
    (tmp_path / "models_cache.json").write_text(json.dumps(cache), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    decision = _router("heuristic").decide("what layers are loaded?", "codex", "codex")
    assert decision["roles"]["supervisor"]["model"] == "old-strong"


def test_auto_mode_sends_any_destructive_prompt_to_the_ai(codex_home, monkeypatch):
    calls = []

    def fake(*_a, **_k):
        calls.append(1)
        raise ValueError("offline")
    monkeypatch.setattr(router, "_run_codex_router", fake)
    _router("auto").decide("nuke the roads layer", "codex", "codex")
    assert calls  # unrecognised instruction: the AI sizes it
    calls.clear()
    _router("auto").decide("how do I delete a field?", "codex", "codex")
    assert not calls  # a plain how-to question is read-only


def test_clamp_effort():
    entry = {"efforts": ["low", "medium", "high"]}
    assert router.clamp_effort(entry, "xhigh") == "high"
    assert router.clamp_effort(entry, "low") == "low"
    assert router.clamp_effort({"efforts": ["low", "xhigh", "max"]}, "max", cap="high") == "low"
    assert router.clamp_effort(entry, "bogus") == "medium"


def test_heuristic_ladders(codex_home):
    cat = router.codex_catalog()
    easy = router.heuristic_decision("what layers are loaded?", "codex", cat)
    assert easy["roles"]["supervisor"] == {"model": "gpt-6.1-sol", "effort": "low"}
    hard = router.heuristic_decision(
        "Delineate the watershed from the DEM, compute flow accumulation, then clip "
        "the land cover and finally build an A3 map layout with a legend", "codex", cat)
    assert hard["complexity"] == 5
    assert hard["roles"]["supervisor"] == {"model": "gpt-6-astra", "effort": "high"}
    assert hard["delegate"] is False  # codex is single-agent


def test_claude_roles():
    cat = router.claude_catalog()
    decision = router.heuristic_decision(
        "Build an A3 flood hazard map layout with legend, then export to PDF and "
        "then save it as a template", "claude", cat)
    roles = decision["roles"]
    assert roles["scout"]["model"] == "haiku"
    assert roles["supervisor"]["model"] in ("opus", "sonnet")
    assert decision["delegate"] is (decision["complexity"] >= 4)


def _router(mode="auto", **extra):
    return router.Router(dict({"router": mode, "models": {}, "effort": ""}, **extra))


def test_trivial_prompt_skips_ai(codex_home, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("AI router must not run for trivial prompts")
    monkeypatch.setattr(router, "_run_codex_router", boom)
    decision = _router().decide("list the layers", "codex", "codex")
    assert decision["source"] == "heuristic" and decision["complexity"] == 1


def test_ai_route_is_validated(codex_home, monkeypatch):
    calls = []

    def fake(exe, model, text, schema, timeout):
        calls.append((model, schema))
        return {"task": "analysis", "complexity": 4, "supervisor_model": "gpt-6-astra",
                "supervisor_effort": "max", "scout_model": "nope",
                "executor_model": "gpt-6.1-sol", "verifier_model": "gpt-6-luna",
                "delegate": True, "reason": "multi-step"}
    monkeypatch.setattr(router, "_run_codex_router", fake)
    decision = _router().decide("buffer wells, clip with the barangay layer and "
                                "export a GeoPackage", "codex", "codex")
    assert calls[0][0] == "gpt-6-luna"           # cheapest model routes
    assert "gpt-6-astra" in calls[0][1]["properties"]["supervisor_model"]["enum"]
    assert decision["source"] == "ai"
    assert decision["roles"]["supervisor"] == {"model": "gpt-6-astra", "effort": "xhigh"}
    assert decision["roles"]["scout"]["model"]   # invalid id replaced by heuristic
    assert decision["delegate"] is False          # never on codex


def test_ai_failure_falls_back(codex_home, monkeypatch):
    def fail(*_a, **_k):
        raise ValueError("no json")
    monkeypatch.setattr(router, "_run_codex_router", fail)
    decision = _router("ai").decide("hello", "codex", "codex")
    assert decision["source"] == "heuristic" and "unavailable" in decision["reason"]


def test_pins_and_off_and_follow_up(codex_home):
    pinned = _router("heuristic", models={"codex": "gpt-6-luna"}, effort="high")
    decision = pinned.decide("buffer the wells by 250 m", "codex", "codex")
    assert decision["roles"]["supervisor"] == {"model": "gpt-6-luna", "effort": "high"}
    assert decision["pinned"] == ["model", "effort"]
    off = _router("off").decide("anything", "codex", "codex")
    assert off["roles"]["supervisor"] == {"model": "", "effort": ""}
    sticky = _router("heuristic")
    first = sticky.decide("Build an A3 flood map layout with legend then export PDF",
                          "codex", "codex")
    again = sticky.decide("yes, do it", "codex", "codex")
    assert again["source"] == "follow-up"
    assert again["roles"]["supervisor"] == first["roles"]["supervisor"]
    other = sticky.decide("yes", "claude", "claude")
    assert other["source"] != "follow-up"


def test_router_prompt_and_schema(codex_home):
    cat = router.codex_catalog()
    text = router.build_router_prompt("map it", "codex", cat, "xhigh", "layers: 2")
    assert "gpt-6-astra — frontier" in text and "single-agent" in text
    assert "Session context: layers: 2" in text
    schema = router._schema([e["id"] for e in cat], ["low", "medium"])
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_guidance():
    decision = {"task": "analysis", "complexity": 4, "delegate": True, "source": "ai",
                "roles": {"supervisor": {"model": "opus", "effort": "medium"}}}
    assert "qa-verifier PASS" in router.guidance(decision, "claude")
    assert "self-verification" in router.guidance(decision, "codex")
    assert router.guidance({"source": "off"}, "codex") == ""


def test_subagent_definitions_follow_roles():
    decision = {"roles": {"scout": {"model": "haiku"}, "executor": {"model": "opus"},
                          "verifier": {"model": "sonnet"}}}
    defs = subagents.definitions(decision)
    assert set(defs) == {"data-scout", "geoprocessor", "cartographer", "qa-verifier"}
    assert defs["data-scout"]["model"] == "haiku"
    assert defs["geoprocessor"]["model"] == "opus"
    assert defs["qa-verifier"]["model"] == "sonnet"
    assert "mcp__qgis__inspect_dataset" in defs["data-scout"]["tools"]
    assert defs["geoprocessor"]["prompt"].startswith("You are **geoprocessor**")
    default = subagents.definitions()
    assert default["data-scout"]["model"] == "haiku"  # frontmatter fallback
