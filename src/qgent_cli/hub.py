"""The hub: QGent's policy point between the agent and the QGIS engine.

The Claude/Codex CLI spawns ``qgent_cli.bridge`` as its ``qgis`` MCP server;
the bridge forwards every tool call here over a token-authenticated
localhost socket. The hub answers hub tools itself (ask_user, skills),
applies the destructive-operation gate — enforced here, never trusted to a
prompt — and runs everything else on the engine.
"""
import json
import os
import secrets
import socketserver
import threading
import time

from . import safety, skills
from .tool_catalog import ENGINE_TOOL_NAMES, HUB_TOOL_NAMES

APPROVAL_MODES = ("ask", "auto", "deny")


class Hub:
    def __init__(self, engine, mode="ask", approver=None, asker=None,
                 on_event=None, log_path=None):
        if mode not in APPROVAL_MODES:
            raise ValueError(f"approval mode must be one of {APPROVAL_MODES}")
        self.engine = engine
        self.mode = mode
        # approver(request) -> "yes" | "no" | "always"; asker(q, options,
        # allow_other) -> answer text or None. Both may block for input.
        self.approver = approver
        self.asker = asker
        self.on_event = on_event or (lambda *args: None)
        self.log_path = log_path
        self.token = secrets.token_hex(16)
        self._server = None
        self._thread = None
        self._ui_lock = threading.Lock()   # one prompt on screen at a time
        self._log_lock = threading.Lock()

    # -- socket server ---------------------------------------------------------
    def start(self):
        hub = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                line = self.rfile.readline()
                try:
                    request = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return
                if not secrets.compare_digest(
                        str(request.get("token", "")), hub.token):
                    reply = {"ok": False, "error": "bad token"}
                else:
                    ok, text = hub.handle(request.get("tool"),
                                          request.get("args") or {})
                    reply = {"ok": ok, "result": text} if ok else {
                        "ok": False, "error": text}
                self.wfile.write((json.dumps(reply) + "\n").encode("utf-8"))

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False

            def handle_error(self, request, client_address):
                # An interrupted turn closes the bridge before a slow engine
                # call returns; the default handler would print a traceback
                # into the user's terminal.
                pass

        self._server = Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="qgent-hub", daemon=True)
        self._thread.start()
        return self

    @property
    def port(self):
        return self._server.server_address[1] if self._server else 0

    def bridge_env(self):
        return {"QGENT_HUB_HOST": "127.0.0.1", "QGENT_HUB_PORT": str(self.port),
                "QGENT_HUB_TOKEN": self.token}

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # -- policy ----------------------------------------------------------------
    def handle(self, tool, args):
        started = time.time()
        if not isinstance(args, dict):
            args = {}
        if tool in HUB_TOOL_NAMES:
            ok, text = self._hub_tool(tool, args)
        elif tool in ENGINE_TOOL_NAMES:
            ok, text = self._engine_tool(tool, args)
        else:
            ok, text = False, f"unknown tool: {tool}"
        self._log(tool, args, ok, text, time.time() - started)
        return ok, text

    def _engine_tool(self, tool, args):
        if tool == "execute_pyqgis":
            fatal = safety.fatal_calls(str(args.get("code") or ""))
            if fatal:
                return False, ("Refused: this code would crash QGIS.\n"
                               + "\n".join(f"- {why}" for why in fatal))
        reasons = self.gate_reasons(tool, args)
        if reasons:
            allowed, message = self._approve(tool, args, reasons)
            if not allowed:
                return False, message
        self.on_event("engine_call", tool, args)
        return self.engine.call(tool, args)

    def gate_reasons(self, tool, args):
        """Why this call needs the user's approval ([] = run it)."""
        if tool == "execute_pyqgis":
            return safety.scan(str(args.get("code") or ""))
        if tool == "manage_layouts":
            return safety.layout_reasons(args)
        if tool == "save_project":
            target = args.get("path") or self._current_project_file()
            if target and os.path.exists(os.path.expanduser(str(target))):
                return [f"overwrites the existing project file {target}"]
            return []
        if tool == "run_processing":
            return [f"overwrites the existing file {value}"
                    for key, value in (args.get("params") or {}).items()
                    if _looks_like_output(key) and isinstance(value, str)
                    and os.path.isfile(value)]
        if (tool == "render_map_snapshot" and args.get("path")
                and os.path.exists(str(args["path"]))):
            return [f"overwrites the existing file {args['path']}"]
        return []

    def _current_project_file(self):
        ok, text = self.engine.call("get_project_context", {})
        if not ok:
            return None
        try:
            return json.loads(text).get("project_file")
        except ValueError:
            return None

    def _approve(self, tool, args, reasons):
        if self.mode == "auto":
            return True, ""
        declined = ("Not run: this operation needs the user's approval ("
                    + "; ".join(reasons) + ").")
        if self.mode == "deny" or self.approver is None:
            return False, (declined + " Approvals are off in this run "
                           "(non-interactive). Take a non-destructive route: "
                           "write new outputs with run_processing (OUTPUT = a "
                           "new file path, e.g. .gpkg) and save_project to a "
                           "new path — those run without approval. If nothing "
                           "else works, tell the user what you would do and "
                           "that they can rerun with --yes.")
        with self._ui_lock:
            answer = self.approver({"tool": tool, "args": args,
                                    "reasons": reasons})
        if answer == "always":
            self.mode = "auto"
            return True, ""
        if answer == "yes":
            return True, ""
        return False, (declined + " The user declined. Do not retry it; "
                       "ask what they want instead or take another route.")

    # -- hub tools ---------------------------------------------------------------
    def _hub_tool(self, tool, args):
        if tool == "list_skills":
            items = skills.list_skills()
            return True, "\n".join(
                f"- {s['name']} ({s['source']}): {s['description']}"
                for s in items) or "no skills installed"
        if tool == "read_skill":
            skill, text = skills.read_skill(args.get("name"))
            if skill is None:
                names = ", ".join(s["name"] for s in skills.list_skills())
                return False, f"no skill named {args.get('name')!r}. Skills: {names}"
            return True, text
        if tool == "ask_user":
            return self._ask(args)
        return False, f"unknown hub tool: {tool}"

    def _ask(self, args):
        question = str(args.get("question") or "").strip()
        options = [str(o) for o in (args.get("options") or []) if str(o).strip()]
        if not question or len(options) < 2:
            return False, "ask_user needs a question and at least two options"
        if self.asker is None:
            return True, ("The user is not available (non-interactive run). "
                          "Proceed with the safest reasonable choice and state "
                          "the assumption in your answer.")
        with self._ui_lock:
            answer = self.asker(question, options[:5],
                                bool(args.get("allow_other", True)))
        if not answer:
            return True, "The user skipped the question. Use the safest option and say so."
        return True, f"The user answered: {answer}"

    # -- audit log -----------------------------------------------------------------
    def _log(self, tool, args, ok, text, seconds):
        if not self.log_path:
            return
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": tool,
                 "ok": ok, "seconds": round(seconds, 2),
                 "args": json.dumps(args, default=str)[:2000],
                 "result": str(text)[:1000]}
        try:
            with self._log_lock, open(self.log_path, "a",
                                      encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        except OSError:
            pass


def _looks_like_output(key):
    key = str(key).upper()
    return key.startswith("OUTPUT") or key in ("DESTINATION", "TARGET")
