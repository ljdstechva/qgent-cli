"""``qgent doctor``: verify every moving part, end to end."""
import json
import os
import subprocess
import sys
import time

from rich.table import Table
from rich.text import Text

from . import __version__, cli_detect, config, mcp_registry, qgis_locator, skills
from .engine_client import EngineClient, EngineError
from .hub import Hub


def bridge_handshake(hub, timeout=60):
    """Spawn the stdio bridge exactly as the agent CLIs do and call a tool."""
    spec = mcp_registry.servers({"mcp": {"qgis_live": "off", "fetch": False}},
                                hub.bridge_env())["qgis"]
    env = dict(os.environ)
    env.update(spec["env"])
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "qgent-doctor", "version": __version__}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "get_project_context", "arguments": {}}},
    ]
    payload = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run([spec["command"], *spec["args"]], input=payload,
                          capture_output=True, text=True, encoding="utf-8",
                          env=env, timeout=timeout)
    replies = {}
    for line in proc.stdout.splitlines():
        try:
            reply = json.loads(line)
        except json.JSONDecodeError:
            continue
        replies[reply.get("id")] = reply
    tools = (replies.get(2, {}).get("result") or {}).get("tools") or []
    call = replies.get(3, {}).get("result") or {}
    ok = bool(tools) and call.get("isError") is False
    detail = f"{len(tools)} tools; get_project_context " + (
        "ok" if call.get("isError") is False else
        "failed: " + str(call or proc.stderr.strip()[-300:]))
    return ok, detail


def run_doctor(cfg, console, engine=None, refresh=False):
    rows = []

    def add(name, ok, detail):
        rows.append((name, ok, detail))

    add("qgent", True, f"{__version__} on Python {sys.version.split()[0]} "
        f"({sys.executable})")
    backend = cli_detect.pick_backend(cfg)
    for name in ("codex", "claude"):
        exe = cli_detect.find(name)
        version = cli_detect.version(exe) if exe else ""
        mark = " (selected)" if name == backend else ""
        add(f"{name} CLI{mark}", bool(exe) or name != backend,
            f"{version or 'found'} — {exe}" if exe else
            ("not installed" + (" — needed for the selected backend" if name == backend
                                else "")))
    try:
        info = qgis_locator.resolve(cfg, refresh=refresh)
        add("QGIS Python", True, f"QGIS {info.get('qgis_version')} / Python "
            f"{info.get('python')} via {info.get('launcher')}")
    except qgis_locator.QgisNotFound as exc:
        add("QGIS Python", False, str(exc))
        info = None

    own_engine = engine is None
    engine = engine or EngineClient(cfg)
    if info is not None:
        started = time.time()
        try:
            details = engine.start()
            add("QGIS engine", True,
                f"ready in {time.time() - started:.1f}s · "
                f"{details.get('algorithms')} algorithms · profile "
                f"{details.get('profile')}")
            hub = Hub(engine, mode="deny").start()
            try:
                ok, detail = bridge_handshake(hub)
                add("MCP bridge (qgis)", ok, detail)
            finally:
                hub.stop()
        except EngineError as exc:
            add("QGIS engine", False, str(exc))
        finally:
            if own_engine:
                engine.stop()

    uvx = mcp_registry._uvx()
    add("uvx (qgis_live, fetch)", bool(uvx), uvx or
        "not found — install uv (https://docs.astral.sh/uv/) for the bundled "
        "qgis_live and fetch servers")
    live = mcp_registry.live_qgis_listening()
    add("live QGIS", True, f"QGIS MCP plugin listening on :{mcp_registry.QGIS_LIVE_PORT}"
        if live else "not running (optional: start the QGIS MCP plugin server "
        "in QGIS to let QGent drive your open QGIS)")
    count = len(skills.list_skills())
    add("skills", count > 0, f"{count} available")
    add("config", True, config.config_path())

    table = Table(show_header=False, box=None, padding=(0, 1))
    for name, ok, detail in rows:
        table.add_row(Text("✓" if ok else "✗", style="green" if ok else "red"),
                      Text(name, style="bold", no_wrap=True),
                      Text(detail, overflow="fold"))
    console.print(table)
    return all(ok for _name, ok, _detail in rows)
