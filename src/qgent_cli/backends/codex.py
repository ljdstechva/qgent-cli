"""Codex backend: ``codex exec --json`` / ``codex exec resume <id> --json``.

The user's ``~/.codex/config.toml`` is ignored (its MCP servers and plugins
would otherwise join every turn); authentication still comes from
CODEX_HOME. QGent's rules and skill index are passed as developer
instructions, and its MCP servers as ``-c mcp_servers.*`` overrides. The
model defaults to the one the user configured for Codex.
"""
import os
import tomllib

from .. import mcp_registry, prompts
from .base import Backend, flatten_content


def user_codex_model():
    home = os.environ.get("CODEX_HOME") or os.path.join(
        os.path.expanduser("~"), ".codex")
    try:
        with open(os.path.join(home, "config.toml"), "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return "", ""
    return str(data.get("model") or ""), str(data.get("model_reasoning_effort") or "")


class CodexBackend(Backend):
    name = "codex"

    def build(self, prompt, session_id, server_map, hub_env, cwd):
        server_args, server_env = mcp_registry.codex_args(server_map)
        user_model, user_effort = user_codex_model()
        model, effort = self.supervisor(user_model, user_effort)
        sandbox = "workspace-write"
        common = [
            "--json", "--skip-git-repo-check", "--ignore-user-config",
            "--disable", "plugins", "--disable", "apps",
            "-c", 'approval_policy="never"',
            "-c", f"sandbox_mode={mcp_registry.toml_literal(sandbox)}",
            "-c", "developer_instructions="
            + mcp_registry.toml_literal(prompts.codex_instructions()),
            *server_args,
        ]
        if model:
            common += ["--model", model]
        if effort:
            common += ["-c", "model_reasoning_effort="
                       + mcp_registry.toml_literal(str(effort))]
        if session_id:
            argv = [self.exe, "exec", "resume", *common, session_id, "-"]
        else:
            argv = [self.exe, "exec", *common, "-"]
        env = dict(os.environ)
        env.update(server_env)
        return argv, env, CodexParser()


class CodexParser:
    def __init__(self):
        self.session_id = None
        self.final = None
        self.failure = ""
        self.started = set()

    @staticmethod
    def _tool_name(item):
        itype = item.get("type") or item.get("item_type")
        if itype == "mcp_tool_call":
            server, tool = item.get("server") or "", item.get("tool") or "tool"
            return f"{server}.{tool}" if server else tool
        if itype == "command_execution":
            return "shell"
        if itype == "web_search":
            return "web_search"
        if itype == "file_change":
            return "edit_files"
        return itype or "tool"

    @staticmethod
    def _tool_input(item):
        itype = item.get("type") or item.get("item_type")
        if itype == "command_execution":
            return {"command": item.get("command", "")}
        if itype == "web_search":
            return {"query": item.get("query", "")}
        if itype == "file_change":
            return {"files": [c.get("path") for c in item.get("changes") or []]}
        return item.get("arguments") or item.get("input") or {}

    def feed(self, event):
        etype = event.get("type", "")
        out = []
        if etype == "thread.started" and event.get("thread_id"):
            self.session_id = event["thread_id"]
            out.append({"type": "session", "id": self.session_id})
        elif etype in ("item.started", "item.completed"):
            item = event.get("item") or {}
            itype = item.get("type") or item.get("item_type")
            item_id = item.get("id")
            if itype in ("agent_message", "assistant_message"):
                if etype == "item.completed" and item.get("text"):
                    out.append({"type": "text", "text": item["text"]})
            elif itype in ("mcp_tool_call", "command_execution", "web_search",
                           "file_change"):
                if item_id not in self.started:
                    self.started.add(item_id)
                    out.append({"type": "tool_call", "id": item_id,
                                "name": self._tool_name(item),
                                "input": self._tool_input(item), "agent": None})
                if etype == "item.completed":
                    out.append(self._result(item, itype))
            elif itype == "error" and item.get("message"):
                out.append({"type": "notice", "text": item["message"]})
        elif etype == "turn.completed":
            self.final = event
            self.failure = ""   # a completion supersedes earlier retries
        elif etype in ("turn.failed", "error"):
            error = event.get("error") or event.get("message") or "Codex turn failed."
            self.failure = str(error.get("message") or error) if isinstance(
                error, dict) else str(error)
        return out

    def _result(self, item, itype):
        if itype == "command_execution":
            text = item.get("aggregated_output") or ""
            code = item.get("exit_code")
            is_error = code not in (0, None)
            if code not in (0, None):
                text = f"(exit {code})\n{text}"
        elif itype == "mcp_tool_call":
            error = item.get("error")
            if error:
                text = flatten_content(error.get("message") if isinstance(
                    error, dict) else error)
                is_error = True
            else:
                result = item.get("result") or {}
                text = flatten_content(result.get("content") if isinstance(
                    result, dict) else result)
                is_error = bool(isinstance(result, dict) and result.get("is_error"))
                is_error = is_error or item.get("status") == "failed"
        elif itype == "file_change":
            text = ", ".join(f"{c.get('kind', 'edit')} {c.get('path')}"
                             for c in item.get("changes") or [])
            is_error = item.get("status") == "failed"
        else:
            text, is_error = "", False
        return {"type": "tool_result", "id": item.get("id"),
                "name": self._tool_name(item), "text": text,
                "is_error": is_error, "agent": None}

    def finish(self, returncode, stderr):
        if self.failure or (returncode != 0 and self.final is None):
            message = self.failure or stderr.strip() or (
                f"codex exited with code {returncode}")
            return [{"type": "error", "text": message[-2000:]}]
        if self.final is None:
            return [{"type": "error", "text": "Codex ended without completing the turn."}]
        usage = self.final.get("usage") or {}
        return [{"type": "done", "result": "", "is_error": False,
                 "session_id": self.session_id,
                 "usage": {"input_tokens": usage.get("input_tokens", 0),
                           "output_tokens": usage.get("output_tokens", 0),
                           "cost_usd": None, "duration_s": None, "turns": None}}]
