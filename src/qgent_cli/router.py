"""QGent's router: picks the model and effort for every role of a turn.

Before each turn the router classifies the request (task type, complexity
1–5) and assigns a model + reasoning effort to each role:

* ``supervisor`` — the main agent that plans, talks to the user and reports
  (both backends);
* ``scout``      — read-only research subagent (Claude: data-scout);
* ``executor``   — analysis and cartography subagents (Claude: geoprocessor,
  cartographer);
* ``verifier``   — independent checker (Claude: qa-verifier).

Codex runs single-agent, so only the supervisor assignment applies there.

Modes (config ``router``): ``auto`` asks a small, fast model (Claude haiku /
the fastest Codex model) to decide, except for trivial prompts the heuristic
already settles; ``ai`` always asks; ``heuristic`` never asks; ``off`` uses the
CLI defaults. A model or effort the user pins (``/model``, ``/effort``, -m,
--effort) always wins for the supervisor.
"""
import json
import os
import re
import subprocess
import tempfile

ROLES = ("supervisor", "scout", "executor", "verifier")
EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
TASK_TYPES = ("question", "inspection", "analysis", "cartography", "data",
              "batch", "research", "conversation")
MODES = ("auto", "ai", "heuristic", "off")

CLAUDE_MODELS = [
    {"id": "haiku", "tier": "fast",
     "label": "fastest and cheapest — lookups, read-only scouting, checklists"},
    {"id": "sonnet", "tier": "balanced",
     "label": "strong workhorse — multi-step PyQGIS/Processing execution"},
    {"id": "opus", "tier": "strong",
     "label": "most capable everyday model — planning, tricky analysis, cartographic judgement"},
    {"id": "fable", "tier": "frontier",
     "label": "frontier — only the hardest, longest jobs (falls back to opus)"},
]
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
TIERS = ("fast", "balanced", "strong", "frontier")


# -- model catalogues ------------------------------------------------------------
def _codex_cache_path():
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(home, "models_cache.json")


def _codex_tier(description):
    text = description.lower()
    if re.search(r"frontier|most demanding|most capable", text):
        return "frontier"
    if re.search(r"fast|affordable|easier|efficient|small|cheap", text):
        return "fast"
    if re.search(r"workhorse|coding and everyday|latest", text):
        return "strong"
    return "balanced"


def codex_catalog(user_model=""):
    """Visible Codex models from the CLI's own cache, current generation first."""
    try:
        with open(_codex_cache_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    entries = []
    for model in data.get("models") or []:
        if model.get("visibility") != "list":
            continue
        description = str(model.get("description") or "")
        efforts = [level.get("effort") for level in model.get("supported_reasoning_levels") or []
                   if level.get("effort")]
        entries.append({
            "id": model.get("slug"), "label": description,
            "tier": _codex_tier(description),
            "older": bool(re.search(r"\b(older|previous)\b", description.lower())),
            "efforts": efforts or ["low", "medium", "high"],
            "priority": model.get("priority", 99)})
    current = [e for e in entries if not e["older"]] or entries
    if all(e["tier"] == "fast" for e in current):
        # Keep a stronger older model so the supervisor floor has somewhere to go.
        current += [e for e in entries if e["older"] and e["tier"] != "fast"][:1]
    current.sort(key=lambda e: e["priority"])
    if not current and user_model:
        current = [{"id": user_model, "label": "configured Codex model", "tier": "strong",
                    "older": False, "efforts": ["low", "medium", "high", "xhigh"],
                    "priority": 0}]
    return current


def claude_catalog():
    return [dict(m, efforts=list(CLAUDE_EFFORTS), older=False) for m in CLAUDE_MODELS]


def catalog(backend, user_model=""):
    return claude_catalog() if backend == "claude" else codex_catalog(user_model)


def pick_tier(cat, tier):
    """Best model of a tier, stepping to the nearest tier when it is missing."""
    order = list(TIERS)
    start = order.index(tier) if tier in order else 1
    # Prefer the same tier, then one step up, then down.
    for offset in (0, 1, -1, 2, -2, 3, -3):
        index = start + offset
        if 0 <= index < len(order):
            for entry in cat:
                if entry["tier"] == order[index]:
                    return entry
    return cat[0] if cat else None


def clamp_effort(entry, effort, cap="xhigh"):
    allowed = list((entry or {}).get("efforts") or CLAUDE_EFFORTS)
    if effort not in EFFORT_ORDER:
        effort = "medium"
    limit = EFFORT_ORDER.index(cap) if cap in EFFORT_ORDER else EFFORT_ORDER.index("xhigh")
    candidates = [e for e in allowed if e in EFFORT_ORDER
                  and EFFORT_ORDER.index(e) <= limit]
    if not candidates:
        return effort
    wanted = EFFORT_ORDER.index(effort)
    return min(candidates, key=lambda e: (abs(EFFORT_ORDER.index(e) - wanted),
                                          EFFORT_ORDER.index(e)))


# -- heuristic -------------------------------------------------------------------
_KEYWORDS = {
    "analysis": r"buffer|clip|intersect|dissolve|reproject|transform|join|overlay|"
                r"calculat|statistic|zonal|slope|aspect|watershed|catchment|flow "
                r"accum|hydrolog|flood|inundat|interpolat|kriging|idw|raster calc|"
                r"classif|reclass|distance|area|density|hotspot|network|route|"
                r"isochrone|viewshed|contour|centroid|voronoi|merge|union|difference",
    "cartography": r"\bmap\b|maps\b|layout|legend|symbol|style|label|pdf|print|"
                   r"vicinity|atlas|template|cartograph|scale bar|north arrow|"
                   r"render|figure|poster|qpt",
    "data": r"load|download|import|convert|export|fetch|open|save|geopackage|gpkg|"
            r"shapefile|geotiff|csv|kml|geojson|dem|srtm|osm|openstreetmap",
    "batch": r"\ball (the )?(files|layers|shapefiles|rasters)|every |each (file|layer)|"
             r"batch|folder of|for each|in bulk",
    "research": r"where can i (get|find)|find (data|a dataset|sources)|recommend|"
                r"compare|best (way|method|source)|which dataset|research",
}
_HEAVY = (r"watershed|catchment|hydrolog|flood (model|hazard|map)|atlas|batch|"
          r"terrain analysis|network analysis|interpolat|classification|report|"
          r"time series|multi-?criteria|suitability|land ?cover|change detection|"
          r"vicinity map|eia|ecc|compliance")
# Requests that change or destroy data deserve a capable model even when
# they are short: the cheapest tier tends to refuse or cut corners.
# Stems, so every verb form counts ("drop", "dropping", "dropped"). Matching
# too much only costs a stronger model; matching too little risks the cheapest
# model touching user data, so the list errs wide.
_DESTRUCTIVE = re.compile(
    r"\b(delet|remov|over-?writ|eras|drop|renam|mov(e|es|ed|ing)\b|replac|truncat|"
    r"wip(e|es|ed|ing)\b|purg|clear|modif|destroy|clean[- ]?up|discard|edit|updat|"
    r"reset|overrid|unlink|rmtree|empty (the|all|every))", re.I)
# The fast tier is earned, not defaulted: only requests recognised as
# read-only may use it. Everything else — any instruction, any multi-clause
# request, a bare "yes" / "do it" / "1" — might change data, and keyword lists
# can never name every way to say so ("nuke it", "get rid of", "set X to
# null"). Unknown phrasings therefore cost a stronger model, never user data.
_READ_ONLY_START = re.compile(
    r"^\s*(what|which|how(?!\s+(about|bout)\b)|why|where|when|who|whose|is|are|was|"
    r"were|does|did|list|show|describe|explain|tell me|count|summari[sz]e|inspect|"
    r"preview|display|print|give me|find|search|look up|check)\b", re.I)
# How-to / definition questions may mention an operation without asking for
# it ("how do I delete a field?", "what does the erase tool do?").
_HOW_TO = re.compile(r"^\s*(how (do|does|can|could|should|would) (i|we|you|one)\b|how to\b|"
                     r"what (does|do|is|are)\b|what's\b|explain (what|how)\b|"
                     r"what happens (if|when)\b|what is the difference)", re.I)
# A second clause turns a question into a request: "What layers are empty?
# Delete those.", "show the nulls then fill them", "... and remove them".
# (A sentence break needs a space after it, so "sites.geojson" is one word.)
_SECOND_CLAUSE = re.compile(r"[.?!;:]\s+\S|,\s*\S|\b(then|and|also|after that|next)\b", re.I)
_SMALLTALK = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|good (morning|afternoon|"
                        r"evening)|bye|goodbye)\b[\s!.,]*(there|everyone|qgent|so much|"
                        r"a lot)?[\s!.]*$", re.I)
_SPECULATIVE = re.compile(r"^\s*what (if|about)\b", re.I)


def is_read_only(prompt):
    """True only for requests that plainly just ask for information."""
    text = prompt.strip()
    if _SMALLTALK.match(text):
        return True
    if not _READ_ONLY_START.match(text) or _SPECULATIVE.match(text):
        return False
    if _SECOND_CLAUSE.search(text.rstrip("?.! ")):
        return False
    return not _DESTRUCTIVE.search(text) or bool(_HOW_TO.match(text))


def _changes_data(prompt):
    """Might the request change data? (Anything not plainly read-only.)"""
    return not is_read_only(prompt)


_QUESTION = re.compile(r"^\s*(what|which|how many|how much|is|are|does|do|can|"
                       r"show|list|describe|tell|explain|why|where|when)\b", re.I)
_CONTINUE = re.compile(r"^\s*(yes|yep|ok(ay)?|sure|go( ahead)?|do it|continue|proceed|"
                       r"please do|option \w+|\d|confirm(ed)?|approved?|fine|"
                       r"sounds good)\b", re.I)


def is_follow_up(prompt):
    """A short "yes / go ahead / option 2" reply that names no new GIS work."""
    if len(prompt.split()) > 8 or not _CONTINUE.match(prompt):
        return False
    return not any(re.search(p, prompt.lower()) for p in _KEYWORDS.values())


def classify(prompt):
    """(task_type, complexity 1-5) from keywords and shape."""
    text = prompt.lower()
    words = len(text.split())
    hits = {kind: len(re.findall(pattern, text)) for kind, pattern in _KEYWORDS.items()}
    kinds = [kind for kind, count in hits.items() if count]
    question = bool(_QUESTION.match(prompt)) or text.rstrip().endswith("?")
    # Unrecognised instructions are at least 2; _destructive_floor guarantees
    # the same for every route, so the keyword score is not inflated here.
    if not kinds and _changes_data(prompt):
        return "data", 2
    if not kinds:
        task = "question" if question else "conversation"
        return task, 2 if words > 40 or text.startswith(("why", "explain")) else 1
    if hits["batch"]:
        task = "batch"
    elif hits["cartography"] and hits["cartography"] >= hits["analysis"]:
        task = "cartography"
    elif hits["analysis"]:
        task = "analysis"
    elif hits["research"]:
        task = "research"
    else:
        task = "data"
    if (question and not hits["analysis"] and not hits["cartography"] and words <= 30
            and not _changes_data(prompt)):
        return "inspection", 1
    score = 1 + len(kinds)
    if hits["analysis"] > 2 or hits["cartography"] > 2:
        score += 1
    if re.search(_HEAVY, text):
        score += 1
    if words > 80:
        score += 1
    if len(re.findall(r"\b(then|and then|after that|finally|also)\b", text)) >= 2:
        score += 1
    return task, max(1, min(5, score))


def _assignments(backend, cat, complexity, cap):
    """Role -> (model entry, effort) ladders for a complexity level."""
    # The supervisor never runs on the fast tier (see SUPERVISOR_FLOOR).
    if backend == "claude":
        sup_tier = {1: "balanced", 2: "balanced", 3: "balanced", 4: "strong",
                    5: "strong"}[complexity]
        sup_effort = {1: "low", 2: "low", 3: "medium", 4: "medium", 5: "high"}[complexity]
        roles = {
            "supervisor": (sup_tier, sup_effort),
            "scout": ("fast", "low"),
            "executor": ("strong" if complexity >= 5 else "balanced",
                         "high" if complexity >= 4 else "medium"),
            "verifier": ("balanced" if complexity >= 4 else "fast", "medium"),
        }
    else:
        sup_tier = {1: "strong", 2: "strong", 3: "strong", 4: "strong",
                    5: "frontier"}[complexity]
        sup_effort = {1: "low", 2: "low", 3: "medium", 4: "high", 5: "high"}[complexity]
        roles = {role: (sup_tier, sup_effort) for role in ROLES}
    out = {}
    for role, (tier, effort) in roles.items():
        entry = pick_tier(cat, tier)
        out[role] = {"model": entry["id"] if entry else "",
                     "effort": clamp_effort(entry, effort, cap)}
    return out


def _supervisor_floor(backend, cat, cap, decision):
    """SUPERVISOR_FLOOR: the main agent never runs on the fast tier.

    Whether a request only reads or changes data cannot be decided reliably
    from free text ("nuke it", "yes", "what are the empty layers delete
    them"), so the cheapest model is kept to routing and, on Claude, the
    scout/verifier subagents. The supervisor always gets at least the strong
    (Codex) / balanced (Claude) tier; the router still chooses effort and when
    to go higher.
    """
    tiers = {e["id"]: e["tier"] for e in cat}
    supervisor = decision["roles"]["supervisor"]
    if tiers.get(supervisor["model"], "strong") != "fast":
        return decision
    floor = _assignments(backend, cat, max(1, decision["complexity"]), cap)["supervisor"]
    floor = dict(floor, effort=supervisor.get("effort") or floor["effort"])
    entry = next((e for e in cat if e["id"] == floor["model"]), None)
    floor["effort"] = clamp_effort(entry, floor["effort"], cap)
    decision = dict(decision, roles=dict(decision["roles"], supervisor=floor))
    if backend == "codex":  # single agent: every role is the supervisor
        decision["roles"] = {role: dict(floor) for role in ROLES}
    return decision


def _destructive_floor(prompt, backend, cat, cap, decision):
    """Data-changing requests get at least complexity 2 and no fast-tier supervisor."""
    decision = _supervisor_floor(backend, cat, cap, decision)
    if not _changes_data(prompt):
        return decision
    tiers = {e["id"]: e["tier"] for e in cat}
    supervisor = decision["roles"]["supervisor"]
    if decision["complexity"] >= 2 and tiers.get(supervisor["model"]) != "fast":
        return decision
    floor = _assignments(backend, cat, max(2, decision["complexity"]), cap)["supervisor"]
    decision = dict(decision, complexity=max(2, decision["complexity"]))
    decision["roles"] = dict(decision["roles"], supervisor=floor)
    decision["reason"] = (decision.get("reason") or "") + " (raised: changes data)"
    return decision


def heuristic_decision(prompt, backend, cat, cap="xhigh"):
    task, complexity = classify(prompt)
    roles = _assignments(backend, cat, complexity, cap)
    return {"task": task, "complexity": complexity, "roles": roles,
            "delegate": backend == "claude" and complexity >= 4,
            "reason": f"keyword heuristic: {task}, complexity {complexity}",
            "source": "heuristic"}


# -- AI router -------------------------------------------------------------------
_RUBRIC = """\
You are QGent's router. QGent is a QGIS/GIS agent. Decide how hard the user's
request is and which model + reasoning effort each role should use, so the
result is excellent without wasting time or money.

Complexity rubric:
1 = trivial: a fact lookup or one-line answer (what CRS is this file, list layers).
2 = simple: one GIS operation (one buffer, one reprojection, load and describe data).
3 = moderate: a short chain of operations or a styled map/PNG (clip + dissolve +
    export; style a layer and render it).
4 = complex: multi-step analysis or a finished print layout/PDF, several datasets,
    decisions about CRS/method (flood exposure with buffers and joins; A3 map
    with legend, scale bar, inset).
5 = hard: long pipelines, hydrology/terrain modelling, batch processing of many
    files, compliance deliverables (EIA/DENR vicinity + hazard maps), or anything
    needing careful method choice and verification.
The supervisor is never a fast-tier model (fast tier is for scout/verifier
only). Anything that could change data (delete, overwrite, edit, fill, set
values, save over, ...) is at least 2.

Roles: supervisor (plans, talks, reports, does the work on single-agent backends);
scout (read-only data inspection); executor (runs analysis/cartography);
verifier (independently checks the result). Cheap/fast models for scout and
easy verification; stronger models where judgement matters. Higher effort only
when reasoning depth pays off. Effort levels allowed: {efforts}.
delegate = true only when the work is big enough to benefit from subagents
(complexity >= 4){delegate_note}.

Available models (id — tier — description; allowed efforts):
{models}

{context}User request:
<<<
{prompt}
>>>
Return only the JSON object."""


def _schema(model_ids, efforts):
    model = {"type": "string", "enum": model_ids}
    effort = {"type": "string", "enum": efforts}
    props = {
        "task": {"type": "string", "enum": list(TASK_TYPES)},
        "complexity": {"type": "integer", "minimum": 1, "maximum": 5},
        "supervisor_model": model, "supervisor_effort": effort,
        "scout_model": model, "executor_model": model, "verifier_model": model,
        "delegate": {"type": "boolean"},
        "reason": {"type": "string", "maxLength": 200},
    }
    return {"type": "object", "additionalProperties": False,
            "properties": props, "required": list(props)}


def _router_model(backend, cat, cfg):
    configured = str(cfg.get("router_model") or "").strip()
    if configured:
        return configured
    entry = pick_tier(cat, "fast")
    return entry["id"] if entry else ""


def build_router_prompt(prompt, backend, cat, cap, context=""):
    allowed = [e for e in EFFORT_ORDER[1:EFFORT_ORDER.index(cap) + 1]]
    lines = [f"- {e['id']} — {e['tier']} — {e['label']}; efforts "
             f"{', '.join(x for x in e['efforts'] if x in allowed)}" for e in cat]
    note = "" if backend == "claude" else (
        "; this backend is single-agent, so set delegate=false and give the "
        "other roles the supervisor's model")
    ctx = f"Session context: {context}\n\n" if context else ""
    return _RUBRIC.format(efforts=", ".join(allowed), delegate_note=note,
                          models="\n".join(lines), context=ctx,
                          prompt=prompt.strip()[:4000])


def _run_claude_router(exe, model, text, schema, timeout):
    argv = [exe, "-p", "--model", model, "--effort", "low", "--output-format", "json",
            "--json-schema", json.dumps(schema), "--tools", "", "--strict-mcp-config",
            "--setting-sources", "project", "--no-session-persistence"]
    proc = subprocess.run(argv, input=text, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          cwd=tempfile.gettempdir())
    data = json.loads(proc.stdout)
    if data.get("is_error"):
        raise RuntimeError(str(data.get("result"))[:300])
    out = data.get("structured_output")
    return out if isinstance(out, dict) else json.loads(data.get("result") or "{}")


def _run_codex_router(exe, model, text, schema, timeout):
    with tempfile.TemporaryDirectory(prefix="qgent-route-") as tmp:
        schema_path = os.path.join(tmp, "schema.json")
        out_path = os.path.join(tmp, "out.json")
        with open(schema_path, "w", encoding="utf-8") as handle:
            json.dump(schema, handle)
        argv = [exe, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                "--ignore-user-config", "--disable", "plugins", "--disable", "apps",
                "-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"',
                "-m", model, "-c", 'model_reasoning_effort="low"',
                "--output-schema", schema_path, "-o", out_path, "-"]
        subprocess.run(argv, input=text, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, cwd=tmp)
        with open(out_path, encoding="utf-8") as handle:
            return json.loads(handle.read())


def _validate(raw, backend, cat, cap, fallback):
    if not isinstance(raw, dict):
        raise ValueError(f"router returned {type(raw).__name__}, not an object")
    ids = {e["id"]: e for e in cat}
    complexity = raw.get("complexity")
    if not isinstance(complexity, int) or not 1 <= complexity <= 5:
        raise ValueError(f"bad complexity {complexity!r}")
    roles = {}
    for role in ROLES:
        model = raw.get(f"{role}_model")
        entry = ids.get(model)
        if entry is None:
            roles[role] = fallback["roles"][role]
            continue
        effort = raw.get("supervisor_effort") if role == "supervisor" else \
            fallback["roles"][role]["effort"]
        roles[role] = {"model": model, "effort": clamp_effort(entry, effort, cap)}
    task = raw.get("task") if raw.get("task") in TASK_TYPES else fallback["task"]
    return {"task": task, "complexity": complexity, "roles": roles,
            "delegate": bool(raw.get("delegate")) and backend == "claude",
            "reason": str(raw.get("reason") or "")[:200], "source": "ai"}


# -- entry point -------------------------------------------------------------------
class Router:
    def __init__(self, cfg):
        self.cfg = cfg
        self.last = None

    def mode(self):
        mode = str(self.cfg.get("router") or "auto").lower()
        return mode if mode in MODES else "auto"

    def cap(self):
        cap = str(self.cfg.get("router_max_effort") or "xhigh").lower()
        return cap if cap in EFFORT_ORDER else "xhigh"

    def decide(self, prompt, backend, exe, user_model="", context="", on_status=None):
        """Return the decision dict for this turn (never raises)."""
        cat = catalog(backend, user_model)
        cap = self.cap()
        mode = self.mode()
        pins = self._pins(backend)
        if mode == "off" or not cat:
            decision = {"task": "conversation", "complexity": 0, "roles": {
                role: {"model": "", "effort": ""} for role in ROLES},
                "delegate": False, "reason": "router off: CLI defaults",
                "source": "off"}
            return self._finish(decision, pins, backend)
        if self.last and is_follow_up(prompt) and self.last.get("backend") == backend:
            decision = dict(self.last, source="follow-up",
                            reason="short follow-up: keeping the previous route")
            # "yes, delete it" after a cheap question must still be raised.
            decision = _destructive_floor(prompt, backend, cat, cap, decision)
            return self._finish(decision, pins, backend)
        decision = heuristic_decision(prompt, backend, cat, cap)
        # Only plainly read-only prompts skip the AI router; everything else is
        # sized by it (and floored below whatever it says).
        trivial = (decision["complexity"] <= 1 and len(prompt.split()) <= 25
                   and is_read_only(prompt))
        if mode == "ai" or (mode == "auto" and not trivial):
            model = _router_model(backend, cat, self.cfg)
            if on_status:
                on_status(f"Routing with {model}")
            try:
                text = build_router_prompt(prompt, backend, cat, cap, context)
                schema = _schema([e["id"] for e in cat],
                                 list(EFFORT_ORDER[1:EFFORT_ORDER.index(cap) + 1]))
                runner = _run_claude_router if backend == "claude" else _run_codex_router
                raw = runner(exe, model, text, schema, timeout=int(
                    self.cfg.get("router_timeout") or 45))
                decision = _validate(raw, backend, cat, cap, decision)
                decision["router_model"] = model
            except Exception as exc:
                # to the heuristic decision; routing must never break a turn.
                decision["reason"] += f" (AI router unavailable: {type(exc).__name__})"
        # The floor binds every route — heuristic, AI or fallback.
        decision = _destructive_floor(prompt, backend, cat, cap, decision)
        return self._finish(decision, pins, backend)

    def _pins(self, backend):
        model = ((self.cfg.get("models") or {}).get(backend) or "").strip()
        effort = str(self.cfg.get("effort") or "").strip()
        return {"model": model, "effort": effort}

    def _finish(self, decision, pins, backend):
        decision = json.loads(json.dumps(decision))  # detached copy
        supervisor = decision["roles"]["supervisor"]
        pinned = []
        if pins["model"]:
            supervisor["model"] = pins["model"]
            pinned.append("model")
        if pins["effort"]:
            supervisor["effort"] = pins["effort"]
            pinned.append("effort")
        decision["pinned"] = pinned
        decision["backend"] = backend
        self.last = decision
        return decision


def guidance(decision, backend):
    """One line for the per-turn context block."""
    if not decision or decision.get("source") == "off":
        return ""
    head = (f"route: {decision['task']}, complexity {decision['complexity']}/5"
            f" (supervisor {decision['roles']['supervisor']['model'] or 'default'}"
            f"/{decision['roles']['supervisor']['effort'] or 'default'})")
    complexity = decision.get("complexity", 0)
    if backend == "claude":
        if decision.get("delegate"):
            tail = ("delegate the heavy steps to the subagents (data-scout, "
                    "geoprocessor, cartographer) and get a qa-verifier PASS before "
                    "reporting done")
        else:
            tail = "act directly; subagents are not needed for this"
    elif complexity >= 4:
        tail = "write a Goal Contract, then run a full self-verification pass"
    elif complexity <= 1:
        tail = "answer directly and briefly"
    else:
        tail = "act directly and verify the outputs"
    return f"{head} — {tail}"
