"""A QGent session: engine + hub + backend, driven one turn at a time.

The UI object (interactive terminal or plain one-shot output) receives
backend events and answers approval/question requests. Those requests
arrive on hub threads, so they are posted into the turn's event queue and
answered on the main thread — the only thread that touches the terminal.
"""
import os
import queue
import re
import threading

from . import backends, cli_detect, config, mcp_registry, prompts, sessions
from .backends.codex import user_codex_model
from .engine_client import EngineClient
from .hub import Hub
from .router import Router
from .router import guidance as route_guidance

_STALE_SESSION = re.compile(
    r"no conversation found|session not found|no such session|"
    r"unknown session|could not find (a )?(session|thread)|thread not found",
    re.IGNORECASE)


class TurnResult:
    def __init__(self):
        self.text = ""
        self.ok = False
        self.error = ""
        self.cancelled = False
        self.usage = {}


class QGentSession:
    def __init__(self, cfg, ui, backend=None, approval=None, session=None,
                 cwd=None):
        self.cfg = cfg
        self.ui = ui
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.backend_name = backend or (session.backend if session else None) \
            or cli_detect.pick_backend(cfg)
        self.session = session or sessions.Session(self.backend_name, self.cwd)
        self.engine = EngineClient(cfg)
        mode = approval or cfg.get("approval") or "ask"
        self.hub = Hub(self.engine, mode=mode,
                       approver=self._approve if ui.interactive else None,
                       asker=self._ask if ui.interactive else None,
                       log_path=config.path("logs", "tools.jsonl"))
        self._events = None
        self._backend = None
        self._turn = None
        self.router = Router(cfg)
        self.last_route = None

    # -- lifecycle ------------------------------------------------------------
    def start(self, warm=True):
        config.ensure_home()
        self.hub.start()
        if self.session.project:
            self.engine.last_project = self.session.project
        if warm:
            self.engine.warm_up()
        return self

    def close(self):
        if self._turn is not None:
            self._turn.cancel()
        self.hub.stop()
        self.engine.stop()
        for name in (f"mcp-claude-{os.getpid()}.json", f"agents-{os.getpid()}.json"):
            try:
                os.remove(os.path.join(config.path("runtime"), name))
            except OSError:
                pass

    def reopen_project(self):
        """After --resume: reopen the session's project in the new engine."""
        project = self.session.project
        if not project or not os.path.isfile(project):
            return None
        ok, text = self.engine.call("open_project", {"path": project})
        return (ok, text)

    @property
    def backend(self):
        exe = cli_detect.find(self.backend_name)
        if self._backend is None or self._backend.name != self.backend_name \
                or self._backend.exe != exe:
            if not exe:
                return None
            self._backend = backends.make(self.backend_name, exe, self.cfg)
        self._backend.cfg = self.cfg
        return self._backend

    def switch_backend(self, name):
        """New conversation on another CLI; the engine (project) is kept."""
        self.backend_name = name
        self.session.backend = name
        self.session.backend_session = None
        self._backend = None

    def new_conversation(self):
        self.session = sessions.Session(self.backend_name, self.cwd)
        self.engine.last_project = None

    # -- one turn -------------------------------------------------------------
    def run_turn(self, user_text):
        result = TurnResult()
        backend = self.backend
        if backend is None:
            result.error = (f"The {self.backend_name} CLI was not found. Install it "
                            "or switch with /backend (see `qgent doctor`).")
            self.ui.on_event({"type": "error", "text": result.error})
            return result
        live = None
        if str((self.cfg.get("mcp") or {}).get("qgis_live", "auto")) == "auto":
            live = mcp_registry.live_qgis_listening()
        server_map = mcp_registry.servers(self.cfg, self.hub.bridge_env(), live)
        if not self.session.title:
            self.session.title = " ".join(user_text.split())[:70]
        self.session.record("user", user_text)
        self.ui.begin_turn()
        try:
            context = prompts.context_block(self.engine, "qgis_live" in server_map,
                                            self.cwd, list(server_map))
            decision = self.router.decide(
                user_text, self.backend_name, backend.exe,
                user_model=user_codex_model()[0] if self.backend_name == "codex" else "",
                context=context[:1500], on_status=self.ui.set_status)
            backend.route = decision
            self.last_route = decision
            self.ui.on_event({"type": "route", "decision": decision})
            line = route_guidance(decision, self.backend_name)
            if line:
                context = context.replace("</qgent_context>",
                                          line + "\n</qgent_context>")
            prompt = f"{context}\n\n{user_text}"
            result = self._run(backend, prompt, server_map, result, retry=True)
        except KeyboardInterrupt:
            result.cancelled = True
            self.ui.on_event({"type": "cancelled"})
        finally:
            self.ui.end_turn()
        if result.ok:
            self.session.turns += 1
            self.session.add_usage(result.usage)
            if self.engine.last_project:
                self.session.project = self.engine.last_project
            self.session.save()
            self.session.record("assistant", result.text)
        return result

    def _run(self, backend, prompt, server_map, result, retry):
        events = queue.Queue()
        self._events = events
        resumed = self.session.backend_session
        turn = backend.run_turn(prompt, resumed, server_map,
                                self.hub.bridge_env(), self.cwd, events.put)
        self._turn = turn
        texts = []
        try:
            while True:
                try:
                    event = events.get(timeout=0.1)
                except queue.Empty:
                    if turn.finished.is_set() and events.empty():
                        break
                    continue
                kind = event.get("type")
                if kind == "_request":
                    self._answer(event)
                    continue
                if kind == "session":
                    # Codex/Claude may announce a new id even on resume.
                    self.session.backend_session = event["id"]
                elif kind == "text_delta":
                    texts.append(event["text"])
                elif kind == "text":
                    # Separate complete messages (Codex sends several per turn).
                    if texts and not "".join(texts).endswith("\n\n"):
                        texts.append("\n\n")
                    texts.append(event["text"])
                elif kind == "done":
                    result.ok = not event.get("is_error")
                    result.usage = event.get("usage") or {}
                    if event.get("is_error"):
                        result.error = event.get("result") or "turn failed"
                    if event.get("session_id"):
                        self.session.backend_session = event["session_id"]
                elif kind == "error":
                    result.error = event.get("text", "")
                if kind == "error" and retry and resumed and _STALE_SESSION.search(
                        result.error):
                    continue  # handled below without alarming the user
                self.ui.on_event(event)
        except KeyboardInterrupt:
            turn.cancel()
            result.cancelled = True
            self.ui.on_event({"type": "cancelled"})
        finally:
            self._turn = None
            self._events = None
            # Release any hub thread still waiting on an unanswered prompt.
            while True:
                try:
                    leftover = events.get_nowait()
                except queue.Empty:
                    break
                if leftover.get("type") == "_request":
                    leftover["box"]["done"].set()
        result.text = "".join(texts).strip()
        if (not result.ok and not result.cancelled and retry and resumed
                and _STALE_SESSION.search(result.error or "")):
            self.ui.on_event({"type": "notice", "text":
                              "The saved conversation could not be resumed; "
                              "starting a new one (the project is kept)."})
            self.session.backend_session = None
            return self._run(backend, prompt, server_map, TurnResult(), retry=False)
        if result.cancelled and self.session.backend_session is None:
            self.session.backend_session = resumed
        return result

    # -- requests from hub threads -----------------------------------------------
    def _request(self, kind, payload):
        events = self._events
        if events is None:
            return None
        box = {"done": threading.Event(), "answer": None}
        events.put({"type": "_request", "kind": kind, "payload": payload,
                    "box": box})
        box["done"].wait()
        return box["answer"]

    def _answer(self, event):
        box = event["box"]
        try:
            if event["kind"] == "approval":
                box["answer"] = self.ui.approve(event["payload"])
            else:
                payload = event["payload"]
                box["answer"] = self.ui.ask(payload["question"], payload["options"],
                                            payload["allow_other"])
        except (KeyboardInterrupt, EOFError):
            box["answer"] = None
        finally:
            box["done"].set()

    def _approve(self, request):
        return self._request("approval", request) or "no"

    def _ask(self, question, options, allow_other):
        return self._request("ask", {"question": question, "options": options,
                                     "allow_other": allow_other})
