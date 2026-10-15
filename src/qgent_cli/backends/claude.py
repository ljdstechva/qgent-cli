"""Claude Code backend: ``claude -p --output-format stream-json``.

QGent's runtime folder is loaded as a Claude Code plugin (``--plugin-dir``),
so the bundled skills and subagents load natively as ``qgent:<name>``; the
operating rules ride on ``--append-system-prompt-file``. MCP servers come
only from QGent's generated config (``--strict-mcp-config``).
"""
import json
import os

from .. import config, mcp_registry, prompts, subagents
from ..config import user_plugin_args
from .base import Backend, flatten_content, short_tool_name

_BASE_ALLOWED = ["Read", "Glob", "Grep", "TodoWrite", "Task", "Agent", "Skill",
                 "WebSearch", "WebFetch", "Write", "Edit", "MultiEdit"]
# AskUserQuestion needs Claude's own TUI; under -p QGent's ask_user is the
# way to ask.
_BASE_DISALLOWED = ["AskUserQuestion", "NotebookEdit"]


class ClaudeBackend(Backend):
    name = "claude"

    def build(self, prompt, session_id, server_map, hub_env, cwd):
        runtime = config.path("runtime")
        mcp_path = mcp_registry.write_claude_config(
            server_map, os.path.join(runtime, f"mcp-claude-{os.getpid()}.json"))
        prompt_path = prompts.write_claude_prompt_file(
            os.path.join(runtime, "qgent-claude-prompt.md"))
        allowed = list(_BASE_ALLOWED) + [f"mcp__{name}" for name in server_map]
        disallowed = list(_BASE_DISALLOWED)
        if self.cfg.get("allow_shell"):
            allowed.append("Bash")
        else:
            disallowed.append("Bash")
        settings = {"env": {"ENABLE_TOOL_SEARCH": "false",
                            "MCP_TOOL_TIMEOUT": "3600000",
                            "MCP_TIMEOUT": "120000"}}
        argv = [self.exe, "-p", "--output-format", "stream-json", "--verbose",
                "--include-partial-messages",
                "--mcp-config", mcp_path, "--strict-mcp-config",
                "--plugin-dir", config.RUNTIME_DIR, *user_plugin_args(),
                "--append-system-prompt-file", prompt_path,
                "--permission-mode", "acceptEdits",
                "--settings", json.dumps(settings),
                "--allowedTools", ",".join(allowed),
                "--disallowedTools", ",".join(disallowed)]
        sources = str(self.cfg.get("claude_setting_sources") or "").strip()
        if sources:
            argv += ["--setting-sources", sources]
        model, effort = self.supervisor()
        if model:
            argv += ["--model", model]
            if model.startswith("fable"):
                # Not every Claude plan includes Fable.
                argv += ["--fallback-model", "opus"]
        if effort:
            argv += ["--effort", effort]
        argv += ["--agents", subagents.write_file(
            os.path.join(runtime, f"agents-{os.getpid()}.json"), self.route)]
        if session_id:
            argv += ["--resume", session_id]
        env = dict(os.environ)
        env.update(hub_env)
        return argv, env, ClaudeParser()


class ClaudeParser:
    def __init__(self):
        self.session_id = None
        self.streamed = False
        self.tools = {}         # tool_use id -> display name
        self.subagents = {}     # Agent/Task tool_use id -> subagent name
        self.final = None

    def _agent(self, event):
        parent = event.get("parent_tool_use_id")
        return self.subagents.get(parent, "subagent") if parent else None

    def feed(self, event):
        etype = event.get("type")
        out = []
        if etype == "system" and event.get("subtype") == "init":
            if event.get("session_id"):
                self.session_id = event["session_id"]
                out.append({"type": "session", "id": self.session_id})
            for server in event.get("mcp_servers") or []:
                if server.get("status") not in (None, "connected", "pending"):
                    out.append({"type": "notice", "text":
                                f"MCP server {server.get('name')!r} "
                                f"{server.get('status')}"})
        elif etype == "stream_event":
            if event.get("parent_tool_use_id"):
                return out
            inner = event.get("event") or {}
            delta = inner.get("delta") or {}
            if (inner.get("type") == "content_block_delta"
                    and delta.get("type") == "text_delta" and delta.get("text")):
                self.streamed = True
                out.append({"type": "text_delta", "text": delta["text"]})
        elif etype == "assistant":
            agent = self._agent(event)
            for block in (event.get("message") or {}).get("content") or []:
                btype = block.get("type")
                if btype == "text" and not agent:
                    if not self.streamed and block.get("text"):
                        out.append({"type": "text", "text": block["text"]})
                elif btype == "tool_use":
                    name = block.get("name", "")
                    tool_input = block.get("input") or {}
                    if name in ("Task", "Agent"):
                        sub = str(tool_input.get("subagent_type") or "subagent")
                        self.subagents[block.get("id")] = sub
                        out.append({"type": "subagent", "name": sub,
                                    "state": "started",
                                    "description": tool_input.get("description", "")})
                    else:
                        display = short_tool_name(name)
                        self.tools[block.get("id")] = display
                        out.append({"type": "tool_call", "id": block.get("id"),
                                    "name": display, "input": tool_input,
                                    "agent": agent})
            if not agent:
                if self.streamed:
                    # Each assistant message is its own paragraph; the deltas
                    # alone would run "…first.Next…" together.
                    out.append({"type": "text_delta", "text": "\n\n"})
                self.streamed = False
        elif etype == "user":
            agent = self._agent(event)
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_id = block.get("tool_use_id")
                if tool_id in self.subagents:
                    out.append({"type": "subagent", "name": self.subagents[tool_id],
                                "state": "finished"})
                    continue
                out.append({"type": "tool_result", "id": tool_id,
                            "name": self.tools.get(tool_id, ""),
                            "text": flatten_content(block.get("content")),
                            "is_error": bool(block.get("is_error")),
                            "agent": agent})
        elif etype == "result":
            self.final = event
        return out

    def finish(self, returncode, stderr):
        final = self.final
        if final is not None:
            usage = final.get("usage") or {}
            return [{"type": "done", "result": final.get("result") or "",
                     "is_error": bool(final.get("is_error")),
                     "session_id": final.get("session_id") or self.session_id,
                     "usage": {
                         "input_tokens": usage.get("input_tokens", 0)
                         + usage.get("cache_read_input_tokens", 0)
                         + usage.get("cache_creation_input_tokens", 0),
                         "output_tokens": usage.get("output_tokens", 0),
                         "cost_usd": final.get("total_cost_usd"),
                         "duration_s": round((final.get("duration_ms") or 0) / 1000, 1),
                         "turns": final.get("num_turns"),
                         # Every model the turn used, subagents included.
                         "models": sorted(final.get("modelUsage") or {})}}]
        message = stderr.strip() or f"claude exited with code {returncode}"
        return [{"type": "error", "text": message[-2000:]}]
