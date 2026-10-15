"""Locate the Claude Code and Codex CLIs that power QGent's agent turns."""
import glob
import os
import shutil
import subprocess


def _first_existing(*paths):
    for path in paths:
        if path and os.path.isfile(path):
            return path
    return ""


def _codex_native_from_npm_shim(path):
    """npm's codex.cmd needs Node and cmd.exe quoting; prefer the bundled exe."""
    if os.name != "nt" or not path or os.path.splitext(path)[1].lower() not in (
            ".cmd", ".bat", ".ps1", ""):
        return ""
    roots = [os.path.dirname(os.path.abspath(path)),
             os.path.join(os.environ.get("APPDATA", ""), "npm")]
    for root in roots:
        pattern = os.path.join(
            root, "node_modules", "@openai", "codex", "node_modules",
            "@openai", "codex-win32-*", "vendor", "*", "bin", "codex.exe")
        found = _first_existing(*sorted(glob.glob(pattern)))
        if found:
            return found
    return ""


def find_claude():
    override = os.environ.get("QGENT_CLAUDE_PATH")
    if override:
        return override
    found = (shutil.which("claude") or shutil.which("claude.exe")
             or _first_existing(
                 os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe"),
                 os.path.join(os.path.expanduser("~"), ".local", "bin", "claude"),
                 os.path.join(os.path.expanduser("~"), ".claude", "local", "claude")))
    if found and os.name == "nt" and found.lower().endswith((".cmd", ".bat")):
        exe = os.path.join(os.path.dirname(found), "node_modules",
                           "@anthropic-ai", "claude-code", "bin", "claude.exe")
        found = _first_existing(exe) or found
    return found or ""


def find_codex():
    override = os.environ.get("QGENT_CODEX_PATH")
    if override:
        return override
    found = shutil.which("codex") or _first_existing(
        os.path.join(os.environ.get("APPDATA", ""), "npm", "codex.cmd"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "codex",
                     "codex.exe"))
    return _codex_native_from_npm_shim(found) or found or ""


def find(backend):
    return find_claude() if backend == "claude" else find_codex()


def version(path):
    if not path:
        return ""
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True,
                             timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (out.stdout or out.stderr).strip().splitlines()[0] if (
        out.stdout or out.stderr).strip() else ""


def pick_backend(cfg):
    """Configured backend, else the first installed (Codex, then Claude)."""
    chosen = (cfg.get("backend") or "").strip().lower()
    if chosen in ("claude", "codex"):
        return chosen
    if find_codex():
        return "codex"
    if find_claude():
        return "claude"
    return "codex"
