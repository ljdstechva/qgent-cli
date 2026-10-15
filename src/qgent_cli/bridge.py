"""QGent's ``qgis`` MCP server (JSON-RPC 2.0 over newline-delimited stdio).

Two ways to run it:

* ``python -m qgent_cli.bridge`` — spawned by the Claude/Codex CLI during a
  qgent session. Stdlib only; forwards every call to the session's hub
  (``QGENT_HUB_PORT`` / ``QGENT_HUB_TOKEN``), which owns the engine and the
  approval prompts.
* ``qgent mcp serve`` — a self-contained server with its own engine, for
  other MCP clients (Claude Desktop, other agents). See ``serve``.
"""
import json
import os
import socket
import sys

from . import __version__
from .tool_catalog import ALL_TOOLS

PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")


def _utf8_stdio():
    # Windows pipes otherwise decode as the ANSI code page and corrupt
    # non-ASCII tool arguments (the plugin learned this with Codex).
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")


def hub_caller():
    host = os.environ.get("QGENT_HUB_HOST", "127.0.0.1")
    port = int(os.environ.get("QGENT_HUB_PORT", "0") or 0)
    token = os.environ.get("QGENT_HUB_TOKEN", "")

    def call(tool, args):
        request = json.dumps({"token": token, "tool": tool, "args": args}) + "\n"
        try:
            with socket.create_connection((host, port), timeout=3600) as sock:
                sock.sendall(request.encode("utf-8"))
                buf = b""
                while b"\n" not in buf:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
        except OSError as exc:
            return False, (f"Could not reach the qgent session at {host}:{port} "
                           f"({exc}). Is qgent still running?")
        line = buf.split(b"\n", 1)[0].decode("utf-8", "replace")
        if not line:
            return False, "Empty response from the qgent session."
        try:
            reply = json.loads(line)
        except json.JSONDecodeError as exc:
            return False, f"Bad response from the qgent session: {exc}"
        if reply.get("ok"):
            return True, reply.get("result", "")
        return False, reply.get("error", "unknown error")

    return call


def run_stdio(call, tools=None, server_name="qgis", stdin=None, stdout=None):
    """Serve MCP on stdio until EOF. ``call(tool, args) -> (ok, text)``."""
    tools = tools if tools is not None else ALL_TOOLS
    names = {tool["name"] for tool in tools}
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    def send(message):
        stdout.write(json.dumps(message) + "\n")
        stdout.flush()

    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        method, msg_id = message.get("method"), message.get("id")
        try:
            if method == "initialize":
                asked = (message.get("params") or {}).get("protocolVersion")
                send({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "protocolVersion": asked if asked in SUPPORTED_VERSIONS
                    else PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": server_name, "version": __version__}}})
            elif method == "ping":
                send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
            elif method == "tools/list":
                send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}})
            elif method == "tools/call":
                params = message.get("params") or {}
                name = params.get("name")
                if name not in names:
                    send({"jsonrpc": "2.0", "id": msg_id, "error": {
                        "code": -32602, "message": f"unknown tool: {name}"}})
                    continue
                ok, text = call(name, params.get("arguments") or {})
                send({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "content": [{"type": "text", "text": str(text)}],
                    "isError": not ok}})
            elif msg_id is not None and not str(method or "").startswith(
                    "notifications/"):
                send({"jsonrpc": "2.0", "id": msg_id, "error": {
                    "code": -32601, "message": f"method not found: {method}"}})
        except Exception as exc:
            if msg_id is not None:
                send({"jsonrpc": "2.0", "id": msg_id, "error": {
                    "code": -32603, "message": f"internal error: {exc}"}})


def serve(approval="deny", warm=True):
    """Standalone server: own engine + hub, no terminal prompts.

    stdout carries the protocol, so approvals can only be "auto" or "deny".
    """
    from . import config
    from .engine_client import EngineClient
    from .hub import Hub

    _utf8_stdio()
    cfg = config.load()
    engine = EngineClient(cfg)
    if warm:
        engine.warm_up()
    hub = Hub(engine, mode=approval, log_path=config.path("logs", "tools.jsonl"))
    try:
        # No terminal to ask in: the client model asks its user itself.
        run_stdio(hub.handle, tools=visible_tools({"ask_user"}))
    finally:
        engine.stop()


def visible_tools(hidden=None):
    if hidden is None:
        hidden = {name.strip() for name in
                  os.environ.get("QGENT_HIDE_TOOLS", "").split(",") if name.strip()}
    return [tool for tool in ALL_TOOLS if tool["name"] not in hidden]


def main():
    _utf8_stdio()
    run_stdio(hub_caller(), tools=visible_tools())


if __name__ == "__main__":
    main()
