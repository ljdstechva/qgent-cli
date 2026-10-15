import io
import json
import sys

from qgent_cli import bridge, mcp_registry
from qgent_cli.tool_catalog import ALL_TOOLS, TOOL_NAMES


def _run(messages, call=None, tools=None):
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages) + "not json\n")
    stdout = io.StringIO()
    bridge.run_stdio(call or (lambda tool, args: (True, f"{tool}:{args}")),
                     tools=tools, stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def test_mcp_handshake_list_and_call():
    replies = _run([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "stat_path", "arguments": {"path": "°ñ"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "nope", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "resources/list"},
    ])
    by_id = {r["id"]: r for r in replies}
    assert len(replies) == 5  # the notification gets no reply
    assert by_id[1]["result"]["protocolVersion"] == "2025-06-18"
    assert {t["name"] for t in by_id[2]["result"]["tools"]} == TOOL_NAMES
    assert by_id[3]["result"]["content"][0]["text"] == "stat_path:{'path': '°ñ'}"
    assert by_id[3]["result"]["isError"] is False
    assert by_id[4]["error"]["code"] == -32602
    assert by_id[5]["error"]["code"] == -32601


def test_unknown_protocol_version_falls_back():
    replies = _run([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "1999-01-01"}}])
    assert replies[0]["result"]["protocolVersion"] == bridge.PROTOCOL_VERSION


def test_tool_errors_are_flagged_and_exceptions_survive():
    def call(tool, args):
        if tool == "execute_pyqgis":
            raise RuntimeError("boom")
        return False, "bad"
    replies = _run([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "execute_pyqgis", "arguments": {"code": "x"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "stat_path", "arguments": {}}},
    ], call=call)
    assert replies[0]["error"]["code"] == -32603
    assert replies[1]["result"]["isError"] is True


def test_hidden_tools(monkeypatch):
    monkeypatch.setenv("QGENT_HIDE_TOOLS", "ask_user, read_skill")
    names = {t["name"] for t in bridge.visible_tools()}
    assert "ask_user" not in names and "read_skill" not in names
    assert "execute_pyqgis" in names


def test_tool_schemas_are_valid_objects():
    for tool in ALL_TOOLS:
        assert tool["inputSchema"]["type"] == "object"
        for required in tool["inputSchema"].get("required", []):
            assert required in tool["inputSchema"]["properties"]
        assert len(tool["description"]) > 20


def test_servers_default(monkeypatch):
    monkeypatch.setattr(mcp_registry, "_uvx", lambda: "uvx")
    servers = mcp_registry.servers({"mcp": {"qgis_live": "auto", "fetch": True}},
                                   {"QGENT_HUB_PORT": "1"}, live=False)
    assert list(servers) == ["qgis", "fetch"]
    assert servers["qgis"]["command"] == sys.executable
    assert servers["qgis"]["args"] == ["-I", "-m", "qgent_cli.bridge"]
    live = mcp_registry.servers({"mcp": {"qgis_live": "auto"}}, {}, live=True)
    assert "qgis_live" in live
    off = mcp_registry.servers({"mcp": {"qgis_live": "off", "fetch": False}}, {}, live=True)
    assert list(off) == ["qgis"]


def test_user_servers_and_disabled(monkeypatch):
    monkeypatch.setattr(mcp_registry, "_uvx", lambda: "")
    cfg = {"mcp": {}, "mcp_servers": {
        "pg": {"command": "pg-mcp", "args": ["--ro"], "env": {"A": 1}},
        "off": {"command": "x", "enabled": False}, "broken": {"args": []}}}
    servers = mcp_registry.servers(cfg, {}, live=False)
    assert list(servers) == ["qgis", "pg"]
    assert servers["pg"]["env"] == {"A": "1"}


def test_codex_args_keep_token_off_the_command_line():
    servers = {"qgis": {"command": "C:\\py\\python.exe", "args": ["-I", "-m", "x"],
                        "env": {"QGENT_HUB_TOKEN": "s3cret", "QGENT_HUB_PORT": "5"},
                        "timeout": 60, "startup": 10}}
    args, env = mcp_registry.codex_args(servers)
    joined = " ".join(args)
    assert "s3cret" not in joined
    assert env["QGENT_HUB_TOKEN"] == "s3cret"
    assert 'mcp_servers.qgis.env_vars=["QGENT_HUB_PORT","QGENT_HUB_TOKEN"]' in args
    assert "mcp_servers.qgis.required=true" in args
    assert any(a.startswith("shell_environment_policy.exclude=") and "QGENT_HUB_TOKEN" in a
               for a in args)
    assert 'mcp_servers.qgis.command="C:\\\\py\\\\python.exe"' in args


def test_claude_config_file(tmp_path):
    path = mcp_registry.write_claude_config(
        {"qgis": {"command": "py", "args": ["-m", "b"], "env": {"K": "v"},
                  "timeout": 1, "startup": 1}}, str(tmp_path / "c.json"))
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    assert data == {"mcpServers": {"qgis": {"type": "stdio", "command": "py",
                                            "args": ["-m", "b"], "env": {"K": "v"}}}}


def test_toml_literal():
    lit = mcp_registry.toml_literal
    assert lit(True) == "true" and lit(3) == "3"
    assert lit('a"b\n') == '"a\\"b\\n"'
    assert lit({"k": [1, "x"]}) == '{ "k" = [1,"x"] }'
