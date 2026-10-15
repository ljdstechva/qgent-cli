#!/usr/bin/env python3
"""Re-vendor shared code and runtime content from the QGent QGIS plugin.

QGent CLI shares its safety gate, print-layout engine, skills, subagents and
assets with the plugin (github.com/ljdstechva/qgent). The CLI must install
on its own with pip, so those files are copied here instead of imported.
Run this after changing them in the plugin, then review the diff:

    python scripts/sync_from_plugin.py "D:/11 QGIS Agent/qgis_chat_agent"

Skills and subagents are copied verbatim and then adapted by
``_adapt_markdown`` for the headless engine (no canvas, no Layout Designer).
"""
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(os.path.dirname(HERE), "src", "qgent_cli")
RUNTIME = os.path.join(PKG, "runtime")


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _vendor_header(source_rel, commit):
    return (f"# Vendored from the QGent plugin ({source_rel} @ {commit}) by\n"
            "# scripts/sync_from_plugin.py. Edit it there and re-sync.\n")


def sync_layout_tools(plugin, commit):
    text = _read(os.path.join(plugin, "bridge", "layout_tools.py"))
    old = ('_PLUGIN_DIR = os.path.dirname(os.path.dirname('
           'os.path.abspath(__file__)))')
    if old not in text:
        raise SystemExit("layout_tools.py: _PLUGIN_DIR line changed upstream")
    # The engine sets QGENT_ASSETS to runtime/assets; the plugin layout was
    # <plugin>/claude_runtime/assets.
    text = text.replace(old, '_ASSETS_DIR = os.environ.get("QGENT_ASSETS", "")')
    old_dirs = ('dirs.append(("qgent", os.path.join(_PLUGIN_DIR, '
                '"claude_runtime",\n                                       '
                '"assets")))')
    if old_dirs not in text:
        raise SystemExit("layout_tools.py: bundled template dir changed")
    text = text.replace(
        old_dirs,
        'if _ASSETS_DIR:\n        dirs.append(("qgent", _ASSETS_DIR))')
    if "_PLUGIN_DIR" in text:
        raise SystemExit("layout_tools.py: unpatched _PLUGIN_DIR use remains")
    target = os.path.join(PKG, "engine", "layout_tools.py")
    _write(target, _vendor_header("bridge/layout_tools.py", commit) + text)


def sync_vicinity_text(plugin, commit):
    text = _read(os.path.join(plugin, "vicinity_text.py"))
    _write(os.path.join(PKG, "engine", "vicinity_text.py"),
           _vendor_header("vicinity_text.py", commit) + text)


def sync_safety(plugin, commit):
    text = _read(os.path.join(plugin, "bridge", "safety.py"))
    _write(os.path.join(PKG, "safety.py"),
           _vendor_header("bridge/safety.py", commit) + text)


_HEADLESS_NOTE = (
    "\n> **QGent CLI (headless engine):** there is no QGIS window. `iface` is\n"
    "> a stand-in: `activeLayer()`, `setActiveLayer()`, `zoomToActiveLayer()`\n"
    "> and `mapCanvas()` extent/CRS/refresh drive a virtual view that\n"
    "> `render_map_snapshot` renders; GUI-only calls (Layout Designer, message\n"
    "> bar dialogs) are no-ops or errors. `QGENT_ASSETS` (pre-injected in\n"
    "> `execute_pyqgis`) holds the bundled template and outline files.\n")


def _adapt_markdown(text):
    """Point plugin-specific wording at the CLI runtime."""
    text = text.replace("claude_runtime/assets/", "QGENT_ASSETS/")
    # The engine puts its own folder (with vicinity_text.py) on sys.path.
    text = text.replace("import qgis_chat_agent\n", "")
    text = text.replace("from qgis_chat_agent.vicinity_text import",
                        "from vicinity_text import")
    text = re.sub(r"package_root = Path\(qgis_chat_agent[^\n]*\n", "", text)
    text = re.sub(
        r'package_root\s*/\s*"claude_runtime"\s*/\s*"assets"',
        'Path(QGENT_ASSETS)', text)
    return text


# Tools the CLI engine adds that each subagent needs (Read: to look at the
# rendered PNGs it is told to check).
_AGENT_EXTRA_TOOLS = {
    "data-scout": ["mcp__qgis__inspect_dataset", "mcp__qgis__stat_path",
                   "mcp__qgis__read_skill", "Read", "Glob"],
    "geoprocessor": ["mcp__qgis__inspect_dataset", "mcp__qgis__load_layer",
                     "mcp__qgis__list_algorithms", "mcp__qgis__algorithm_help",
                     "mcp__qgis__stat_path", "mcp__qgis__read_skill"],
    "cartographer": ["mcp__qgis__read_skill", "mcp__qgis__load_layer", "Read"],
    "qa-verifier": ["mcp__qgis__inspect_dataset", "Read"],
}


def _adapt_agent(name, text):
    extra = _AGENT_EXTRA_TOOLS.get(name, [])

    def add(match):
        tools = [t.strip() for t in match.group(1).split(",") if t.strip()]
        tools += [t for t in extra if t not in tools]
        return "tools: " + ", ".join(tools)

    text = re.sub(r"^tools:\s*(.*)$", add, text, count=1, flags=re.MULTILINE)
    # The plugin calls the main agent "the Supervisor", sometimes wrapped
    # across a line break.
    text = re.sub(r"\bthe(\s+)Supervisor\b", r"the\1main QGent agent", text)
    return re.sub(r"\bSupervisor\b", "main QGent agent", text)


def sync_runtime(plugin):
    src_rt = os.path.join(plugin, "claude_runtime")
    for sub in ("skills", "subagents", "assets"):
        dst = os.path.join(RUNTIME, sub)
        if os.path.isdir(dst):
            shutil.rmtree(dst)
    shutil.copytree(os.path.join(src_rt, ".claude", "skills"),
                    os.path.join(RUNTIME, "skills"))
    shutil.copytree(os.path.join(src_rt, ".claude", "agents"),
                    os.path.join(RUNTIME, "subagents"))
    shutil.copytree(os.path.join(src_rt, "assets"),
                    os.path.join(RUNTIME, "assets"))
    for root, _dirs, files in os.walk(RUNTIME):
        for name in files:
            if not name.endswith(".md") or root == RUNTIME:
                continue
            path = os.path.join(root, name)
            text = _adapt_markdown(_read(path))
            if os.path.basename(root) == "subagents":
                text = _adapt_agent(name[:-3], text)
            if name == "SKILL.md" and _HEADLESS_NOTE not in text:
                # Insert the note right after the YAML frontmatter.
                parts = text.split("---", 2)
                if len(parts) == 3 and not parts[0].strip():
                    text = "---" + parts[1] + "---\n" + _HEADLESS_NOTE + parts[2]
            _write(path, text)


def main(argv):
    if len(argv) != 2:
        raise SystemExit(__doc__)
    plugin = os.path.abspath(argv[1])
    if not os.path.isfile(os.path.join(plugin, "bridge", "layout_tools.py")):
        raise SystemExit(f"not a QGent plugin checkout: {plugin}")
    try:
        commit = subprocess.check_output(
            ["git", "-C", plugin, "rev-parse", "--short", "HEAD"],
            text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    sync_safety(plugin, commit)
    sync_layout_tools(plugin, commit)
    sync_vicinity_text(plugin, commit)
    sync_runtime(plugin)
    print(f"synced from {plugin} @ {commit}")


if __name__ == "__main__":
    main(sys.argv)
