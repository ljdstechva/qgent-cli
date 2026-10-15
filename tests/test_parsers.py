from qgent_cli.backends.claude import ClaudeParser
from qgent_cli.backends.codex import CodexParser


def _feed(parser, events):
    out = []
    for event in events:
        out += parser.feed(event)
    return out


def test_claude_stream():
    parser = ClaudeParser()
    out = _feed(parser, [
        {"type": "system", "subtype": "init", "session_id": "S1",
         "mcp_servers": [{"name": "qgis", "status": "connected"},
                         {"name": "fetch", "status": "failed"}]},
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "text_delta", "text": "Hel"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "text_delta", "text": "lo"}}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Hello"},
            {"type": "tool_use", "id": "t1", "name": "mcp__qgis__execute_pyqgis",
             "input": {"code": "print(1)"}},
            {"type": "tool_use", "id": "a1", "name": "Agent",
             "input": {"subagent_type": "geoprocessor", "description": "buffer"}}]}},
        {"type": "assistant", "parent_tool_use_id": "a1", "message": {"content": [
            {"type": "tool_use", "id": "t2", "name": "mcp__qgis__run_processing",
             "input": {"alg_id": "native:buffer"}}]}},
        {"type": "user", "parent_tool_use_id": "a1", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t2",
             "content": [{"type": "text", "text": "done"}]}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "1", "is_error": False},
            {"type": "tool_result", "tool_use_id": "a1", "content": "report"}]}},
        {"type": "result", "result": "Hello", "session_id": "S1", "is_error": False,
         "total_cost_usd": 0.02, "duration_ms": 2500, "num_turns": 3,
         "modelUsage": {"claude-sonnet-5": {}, "claude-haiku-5-5": {}},
         "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5}},
    ])
    kinds = [e["type"] for e in out]
    assert kinds == ["session", "notice", "text_delta", "text_delta", "tool_call",
                     "subagent", "text_delta", "tool_call", "tool_result", "tool_result",
                     "subagent"]
    assert out[4]["name"] == "qgis.execute_pyqgis" and out[4]["agent"] is None
    assert out[6] == {"type": "text_delta", "text": "\n\n"}  # paragraph break
    assert out[7]["agent"] == "geoprocessor"
    assert out[8]["text"] == "done" and out[8]["agent"] == "geoprocessor"
    assert out[10] == {"type": "subagent", "name": "geoprocessor", "state": "finished"}
    done = parser.finish(0, "")
    assert done[0]["type"] == "done"
    assert done[0]["usage"]["input_tokens"] == 100
    assert done[0]["usage"]["cost_usd"] == 0.02
    assert done[0]["usage"]["models"] == ["claude-haiku-5-5", "claude-sonnet-5"]


def test_claude_unstreamed_text_and_failure():
    parser = ClaudeParser()
    out = _feed(parser, [{"type": "assistant", "message": {"content": [
        {"type": "text", "text": "full block"}]}}])
    assert out == [{"type": "text", "text": "full block"}]
    failed = ClaudeParser().finish(1, "auth error")
    assert failed == [{"type": "error", "text": "auth error"}]


def test_codex_stream():
    parser = CodexParser()
    out = _feed(parser, [
        {"type": "thread.started", "thread_id": "T1"},
        {"type": "item.started", "item": {"id": "i1", "type": "mcp_tool_call",
                                          "server": "qgis", "tool": "stat_path",
                                          "arguments": {"path": "a"}}},
        {"type": "item.completed", "item": {"id": "i1", "type": "mcp_tool_call",
                                            "server": "qgis", "tool": "stat_path",
                                            "result": {"content": [{"type": "text",
                                                                    "text": "{}"}]},
                                            "status": "completed"}},
        {"type": "item.completed", "item": {"id": "i2", "type": "command_execution",
                                            "command": "dir", "aggregated_output": "x",
                                            "exit_code": 1}},
        {"type": "item.completed", "item": {"id": "i3", "type": "mcp_tool_call",
                                            "server": "qgis", "tool": "execute_pyqgis",
                                            "error": {"message": "denied"},
                                            "status": "failed"}},
        {"type": "item.completed", "item": {"id": "i4", "type": "agent_message",
                                            "text": "All done"}},
        {"type": "error", "message": "Reconnecting... 1/5"},
        {"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}},
    ])
    kinds = [e["type"] for e in out]
    assert kinds == ["session", "tool_call", "tool_result", "tool_call", "tool_result",
                     "tool_call", "tool_result", "text"]
    assert out[1]["name"] == "qgis.stat_path"
    assert out[4]["is_error"] and out[4]["text"].startswith("(exit 1)")
    assert out[6]["is_error"] and out[6]["text"] == "denied"
    done = parser.finish(0, "")
    assert done[0]["type"] == "done" and done[0]["usage"]["input_tokens"] == 7


def test_codex_failure_wins_after_completion():
    parser = CodexParser()
    _feed(parser, [{"type": "turn.completed"},
                   {"type": "turn.failed", "error": {"message": "usage limit"}}])
    assert parser.finish(0, "") == [{"type": "error", "text": "usage limit"}]
    assert CodexParser().finish(0, "")[0]["type"] == "error"
