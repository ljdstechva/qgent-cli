"""One agent turn = one CLI process (``claude -p`` / ``codex exec``).

Like the QGent plugin, QGent CLI spawns a fresh process per turn and
resumes the conversation by session id, so a crashed or interrupted turn
never wedges the session. The process's JSON event stream is parsed by a
backend-specific parser into plain dict events:

    {"type": "session", "id"}                 conversation id (resume key)
    {"type": "text_delta", "text"}            streamed assistant text
    {"type": "text", "text"}                  a complete assistant text block
    {"type": "tool_call", "id", "name", "input", "agent"}
    {"type": "tool_result", "id", "text", "is_error", "agent"}
    {"type": "subagent", "name", "state"}     started | finished
    {"type": "notice", "text"}                warnings worth showing
    {"type": "done", "result", "usage", "is_error"}
    {"type": "error", "text"}
"""
import json
import os
import subprocess
import threading

CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def short_tool_name(name):
    """mcp__qgis__execute_pyqgis -> qgis.execute_pyqgis"""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return f"{parts[1]}.{parts[2]}"
    return name


def flatten_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    out.append(str(item.get("text", "")))
                elif item.get("type") == "image":
                    out.append("[image]")
            elif isinstance(item, str):
                out.append(item)
        return "\n".join(out)
    if isinstance(content, dict):
        return flatten_content(content.get("content")) or json.dumps(content)[:2000]
    return "" if content is None else str(content)


class Turn:
    """Runs one CLI process and feeds parsed events to ``on_event``."""

    def __init__(self, argv, env, cwd, prompt, parser, on_event):
        self.argv, self.env, self.cwd = argv, env, cwd
        self.prompt = prompt
        self.parser = parser
        self.on_event = on_event
        self.proc = None
        self.stderr_lines = []
        self.cancelled = False
        self.finished = threading.Event()
        self.returncode = None

    def start(self):
        try:
            self.proc = subprocess.Popen(
                self.argv, env=self.env, cwd=self.cwd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", bufsize=1,
                # Ctrl+C belongs to qgent: it cancels the turn deliberately.
                creationflags=CREATE_NEW_PROCESS_GROUP)
        except OSError as exc:
            self.on_event({"type": "error", "text": f"could not start "
                           f"{os.path.basename(self.argv[0])}: {exc}"})
            self.finished.set()
            return False
        threading.Thread(target=self._feed_stdin, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        return True

    def _feed_stdin(self):
        try:
            self.proc.stdin.write(self.prompt)
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass

    def _read_stderr(self):
        for line in self.proc.stderr:
            self.stderr_lines.append(line.rstrip("\n"))
            if len(self.stderr_lines) > 400:
                del self.stderr_lines[:100]

    def _read_stdout(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    for out in self.parser.feed(event):
                        self.on_event(out)
        finally:
            self.returncode = self.proc.wait()
            if not self.cancelled:
                for out in self.parser.finish(self.returncode,
                                              "\n".join(self.stderr_lines[-40:])):
                    self.on_event(out)
            self.finished.set()

    def cancel(self):
        self.cancelled = True
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        if os.name == "nt":
            # /T takes the MCP server children down with the CLI.
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True)
        else:
            proc.kill()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            pass


class Backend:
    name = ""

    def __init__(self, exe, cfg):
        self.exe = exe
        self.cfg = cfg
        self.route = None   # the router's decision for the next turn

    def model(self):
        return ((self.cfg.get("models") or {}).get(self.name) or "").strip()

    def supervisor(self, default_model="", default_effort=""):
        """(model, effort) for the main agent: route, then pins, then defaults."""
        roles = (self.route or {}).get("roles") or {}
        chosen = roles.get("supervisor") or {}
        model = chosen.get("model") or self.model() or default_model
        effort = chosen.get("effort") or str(self.cfg.get("effort") or "") or default_effort
        return model, effort

    def build(self, prompt, session_id, server_map, hub_env, cwd):
        """Return (argv, env, parser)."""
        raise NotImplementedError

    def run_turn(self, prompt, session_id, server_map, hub_env, cwd, on_event):
        argv, env, parser = self.build(prompt, session_id, server_map,
                                       hub_env, cwd)
        turn = Turn(argv, env, cwd, prompt, parser, on_event)
        turn.start()
        return turn
