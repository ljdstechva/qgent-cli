"""System instructions and the per-turn ``<qgent_context>`` block."""
import json
import os

from . import config, skills

_CLAUDE_ADDENDUM = """
## Subagents (Claude backend)
Specialists are available through the Agent/Task tool: `data-scout`
(read-only facts), `geoprocessor` (analysis/edits), `cartographer` (styling,
layouts, exports) and `qa-verifier` (independent PASS/FAIL against the
Definition of Done). QGent's router has already given each one a model sized
for this turn, and the `route:` line in `<qgent_context>` says whether to
delegate — follow it. When you delegate, paste the full Goal Contract into
each subagent prompt, accept only reports with evidence, and get a
qa-verifier PASS before reporting done; never overrule its FAIL. Skills load
natively as `qgent:<name>` (the Skill tool) or through `read_skill`. Only you
can call `ask_user`.
"""

_CODEX_ADDENDUM = """
## Codex backend
You work single-agent: do the work yourself, then run an explicit
self-verification pass that re-checks each Definition-of-Done item with the
tools before you report. Skills are read with the `read_skill` tool (you
cannot load them any other way); read the matching one before the work.
Prefer the `qgis` MCP tools for GIS work over shell commands.

The Codex sandbox (read-only or workspace-write) limits only your shell
commands. The `qgis` tools run outside it, in QGent's engine: they can read,
write and delete files, and QGent itself asks the user to approve anything
destructive. Never refuse or skip a GIS task because the sandbox looks
read-only; do it with the `qgis` tools (execute_pyqgis, run_processing,
save_project, ...). If QGent declines a call, report that instead.
"""


def base_instructions():
    with open(os.path.join(config.RUNTIME_DIR, "QGENT.md"),
              encoding="utf-8") as handle:
        return handle.read()


def _skills_section():
    index = skills.index_text()
    return f"\n## Skill index\n{index}\n" if index else ""


def claude_append_prompt():
    return base_instructions() + _CLAUDE_ADDENDUM


def codex_instructions():
    return base_instructions() + _CODEX_ADDENDUM + _skills_section()


def write_claude_prompt_file(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(claude_append_prompt())
    return path


def context_block(engine, live, cwd=None, servers=()):
    """Compact grounding facts prepended to each user message."""
    lines = [f"working folder: {cwd or os.getcwd()}"]
    if engine.state == "ready":
        info = engine.info
        lines.append(f"engine: QGIS {info.get('qgis_version', '?')} headless, ready")
        ok, text = engine.call("get_project_context", {}, timeout=20)
        if ok:
            try:
                ctx = json.loads(text)
            except ValueError:
                ctx = None
            if ctx:
                lines += _project_lines(ctx)
    elif engine.state == "starting":
        lines.append("engine: QGIS starting (tool calls wait for it)")
    elif engine.state == "failed":
        lines.append("engine: UNAVAILABLE — " + engine.error.splitlines()[0][:200])
    else:
        lines.append("engine: QGIS starts on first tool call")
    lines.append("live QGIS: attached as server `qgis_live`" if live
                 else "live QGIS: not attached (no running QGIS with the QGIS MCP plugin)")
    extra = [name for name in servers if name not in ("qgis", "qgis_live")]
    if extra:
        lines.append("other MCP servers: " + ", ".join(extra))
    return "<qgent_context>\n" + "\n".join(lines) + "\n</qgent_context>"


def _project_lines(ctx):
    out = []
    project = ctx.get("project_file") or "none (unsaved session project)"
    dirty = " — unsaved changes" if ctx.get("unsaved_changes") else ""
    out.append(f"project: {project}{dirty}; CRS {ctx.get('project_crs') or 'unset'}")
    layers = ctx.get("layers") or []
    if layers:
        out.append(f"layers ({len(layers)}):")
        for layer in layers[:25]:
            bits = [layer.get("type", "").replace("Qgs", "").replace("Layer", "")]
            if layer.get("geometry"):
                bits.append(layer["geometry"])
            if layer.get("crs"):
                bits.append(layer["crs"])
            if "feature_count" in layer:
                bits.append(f"{layer['feature_count']} features")
            if not layer.get("valid", True):
                bits.append("INVALID SOURCE")
            if not layer.get("visible", True):
                bits.append("hidden")
            out.append(f"  - {layer.get('name')} ({', '.join(b for b in bits if b)})")
        if len(layers) > 25:
            out.append(f"  … {len(layers) - 25} more (get_project_context)")
    else:
        out.append("layers: none")
    if ctx.get("active_layer"):
        out.append(f"active layer: {ctx['active_layer']}")
    if ctx.get("layouts"):
        out.append("print layouts: " + ", ".join(ctx["layouts"][:15]))
    return out
