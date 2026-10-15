"""The MCP servers packaged with QGent, and their per-backend wiring.

* ``qgis``      — QGent's headless QGIS engine (always on).
* ``qgis_live`` — the community QGIS MCP server (github.com/nkarasiak/qgis-mcp)
  driving a *running* QGIS through its "QGIS MCP" plugin on localhost:9876.
  ``auto`` attaches it only on turns where that port answers.
* ``fetch``     — web page fetching (data portals, documentation).
* plus any ``[mcp_servers.<name>]`` the user adds to config.toml.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading

QGIS_LIVE_SOURCE = "https://github.com/nkarasiak/qgis-mcp/archive/refs/heads/main.zip"
QGIS_LIVE_PORT = 9876
FETCH_PACKAGE = "mcp-server-fetch==2026.10.10"

DESCRIPTIONS = {
    "qgis": "QGent headless QGIS engine (PyQGIS, Processing, layouts, rendering)",
    "qgis_live": "your running QGIS via the QGIS MCP plugin (nkarasiak/qgis-mcp)",
    "fetch": "fetch web pages as markdown (mcp-server-fetch)",
}


def live_qgis_listening(port=QGIS_LIVE_PORT, timeout=0.25):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _uvx():
    return shutil.which("uvx") or shutil.which("uvx.exe") or ""


def handshake(spec, timeout=180):
    """Start a stdio server, list its tools, stop it: (ok, tool names | error).

    The timeout is generous because a first ``uvx``/``npx`` run downloads the
    package.
    """
    if spec.get("url"):
        return True, ["(remote server: checked by the agent CLI when it connects)"]
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "qgent", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    env = dict(os.environ, **spec.get("env", {}))
    try:
        proc = subprocess.Popen([spec["command"], *spec.get("args", [])],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", env=env)
    except OSError as exc:
        return False, f"could not start {spec['command']}: {exc}"
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    stderr_tail = []
    threading.Thread(target=lambda: stderr_tail.extend(proc.stderr), daemon=True).start()
    try:
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        for line in proc.stdout:
            try:
                reply = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(reply, dict) and reply.get("id") == 2:
                tools = (reply.get("result") or {}).get("tools") or []
                return True, [tool.get("name", "?") for tool in tools]
    except (OSError, ValueError) as exc:
        return False, str(exc)
    finally:
        timer.cancel()
        proc.kill()
    detail = "".join(stderr_tail)[-400:].strip()
    return False, ("timed out or exited before listing tools"
                   + (f": {detail}" if detail else ""))


def servers(cfg, hub_env, live=None):
    """{name: {command, args, env, timeout, startup, note}} for this turn."""
    out = {
        "qgis": {
            # -I: ignore PYTHON* variables and the cwd, so a stray module in
            # the user's folder can never shadow the bridge.
            "command": sys.executable,
            "args": ["-I", "-m", "qgent_cli.bridge"],
            "env": dict(hub_env, PYTHONIOENCODING="utf-8"),
            "timeout": 3600, "startup": 30,
        },
    }
    mcp_cfg = cfg.get("mcp") or {}
    live_mode = str(mcp_cfg.get("qgis_live", "auto")).lower()
    uvx = _uvx()
    if live_mode != "off" and uvx:
        if live is None:
            live = live_qgis_listening()
        if live_mode == "on" or live:
            out["qgis_live"] = {
                "command": uvx,
                "args": ["--from", QGIS_LIVE_SOURCE, "qgis-mcp-server"],
                "env": {"PYTHONIOENCODING": "utf-8"},
                "timeout": 600, "startup": 120,
            }
    if mcp_cfg.get("fetch", True) and uvx:
        out["fetch"] = {"command": uvx, "args": [FETCH_PACKAGE],
                        "env": {"PYTHONIOENCODING": "utf-8"},
                        "timeout": 120, "startup": 120}
    for name, spec in (cfg.get("mcp_servers") or {}).items():
        if not isinstance(spec, dict) or spec.get("enabled", True) is False:
            continue
        common = {"env": {str(k): str(v) for k, v in (spec.get("env") or {}).items()},
                  "timeout": int(spec.get("timeout", 600)),
                  "startup": int(spec.get("startup", 60))}
        if spec.get("url"):
            # Remote server (streamable HTTP or SSE).
            out[name] = dict(common, url=str(spec["url"]),
                             transport=str(spec.get("transport") or "http"),
                             headers={str(k): str(v) for k, v in
                                      (spec.get("headers") or {}).items()})
        elif spec.get("command"):
            out[name] = dict(common, command=str(spec["command"]),
                             args=[str(a) for a in spec.get("args", [])])
    return out


def claude_config(server_map):
    servers = {}
    for name, spec in server_map.items():
        if spec.get("url"):
            entry = {"type": "sse" if spec.get("transport") == "sse" else "http",
                     "url": spec["url"]}
            if spec.get("headers"):
                entry["headers"] = spec["headers"]
        else:
            entry = {"type": "stdio", "command": spec["command"],
                     "args": spec["args"], "env": spec["env"]}
        servers[name] = entry
    return {"mcpServers": servers}


def write_claude_config(server_map, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(claude_config(server_map), handle, indent=1)
    return path


def toml_literal(value):
    """TOML for ``codex -c key=value`` (JSON strings are valid TOML)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(toml_literal(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(str(k))} = {toml_literal(v)}"
                                for k, v in value.items()) + " }"
    raise TypeError(f"unsupported value {value!r}")


def codex_args(server_map):
    """``-c`` overrides plus the env values the Codex process must carry.

    Secrets (the hub token) travel in the environment and are passed through
    by name (``env_vars``), never on the command line; the shell policy hides
    them from commands the model runs.
    """
    args, env, secret_names = [], {}, []
    for name, spec in server_map.items():
        prefix = f"mcp_servers.{name}."
        if spec.get("url"):
            # Header values ride in the environment (env_http_headers), so
            # API keys never appear on the command line.
            header_env = {}
            for index, (header, value) in enumerate(sorted(spec.get("headers", {}).items())):
                var = f"QGENT_MCP_{re.sub(r'[^A-Z0-9]', '_', name.upper())}_H{index}"
                env[var] = value
                header_env[header] = var
                secret_names.append(var)
            args += ["-c", prefix + "url=" + toml_literal(spec["url"]),
                     "-c", prefix + "enabled=true",
                     "-c", prefix + f"startup_timeout_sec={int(spec['startup'])}",
                     "-c", prefix + f"tool_timeout_sec={int(spec['timeout'])}",
                     "-c", prefix + 'default_tools_approval_mode="approve"']
            if header_env:
                args += ["-c", prefix + "env_http_headers=" + toml_literal(header_env)]
            continue
        passthrough = sorted(spec["env"])
        env.update(spec["env"])
        # Hide server credentials from shell commands the model runs.
        secret_names += [key for key in passthrough if key != "PYTHONIOENCODING"]
        args += [
            "-c", prefix + "command=" + toml_literal(spec["command"]),
            "-c", prefix + "args=" + toml_literal(spec["args"]),
            "-c", prefix + "env_vars=" + toml_literal(passthrough),
            "-c", prefix + "enabled=true",
            "-c", prefix + f"startup_timeout_sec={int(spec['startup'])}",
            "-c", prefix + f"tool_timeout_sec={int(spec['timeout'])}",
            "-c", prefix + 'default_tools_approval_mode="approve"',
        ]
        if name == "qgis":
            args += ["-c", prefix + "required=true"]
    if secret_names:
        args += ["-c", "shell_environment_policy.exclude="
                 + toml_literal(sorted(set(secret_names)))]
    return args, env
