"""The interactive ``qgent`` prompt: input, slash commands, turns."""
import json
import os
import subprocess
import sys

from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from . import __version__, cli_detect, config, mcp_registry, router, sessions, skills
from .agent import QGentSession
from .hub import APPROVAL_MODES
from .ui import market
from .ui.terminal import (
    ACCENT,
    ACCENT_2,
    DIM,
    FAINT,
    OK,
    WARN,
    TerminalUI,
    banner,
    route_renderable,
    short_path,
)

COMMAND_GROUPS = [
    ("Conversation", {
        "/new": "start a new conversation (engine and project are kept)",
        "/resume": "/resume [id] — continue a saved session",
        "/sessions": "saved sessions for this folder",
        "/usage": "tokens (and cost) used in this session",
    }),
    ("Models & routing", {
        "/route": "how the last turn was routed (task, complexity, models, efforts)",
        "/router": "/router [auto|ai|heuristic|off] — who picks models and effort",
        "/backend": "/backend [claude|codex] — the agent CLI",
        "/model": "/model [name|auto] — pin the main model, or let the router pick",
        "/effort": "/effort [low…max|auto] — pin the reasoning effort",
        "/approval": "/approval [ask|auto|deny] — destructive-operation policy",
    }),
    ("QGIS engine", {
        "/project": "the engine's project and layers",
        "/open": "/open <file.qgz> — open a project",
        "/save": "/save [file.qgz] — save the project",
        "/render": "render the current map to a PNG and open it",
        "/engine": "/engine [status|restart]",
    }),
    ("MCP servers & skills", {
        "/mcp": "/mcp [list|add [query]|remove <name>|test <name>]",
        "/skills": "/skills [list|add [query]|remove <name>]",
        "/skill": "/skill <name> — read a skill's playbook",
    }),
    ("Other", {
        "/doctor": "check CLIs, QGIS, engine and MCP servers",
        "/clear": "clear the screen",
        "/help": "this help",
        "/exit": "quit (also ctrl+d)",
    }),
]
COMMANDS = {name: text for _group, cmds in COMMAND_GROUPS for name, text in cmds.items()}
_SUBCOMMANDS = {"/mcp": ["list", "add", "remove", "test"],
                "/skills": ["list", "add", "remove"],
                "/router": list(router.MODES), "/approval": list(APPROVAL_MODES),
                "/backend": ["claude", "codex"], "/engine": ["status", "restart"],
                "/effort": ["auto", "low", "medium", "high", "xhigh", "max"]}

PROMPT_STYLE = {
    "badge": "bg:#7c3aed #ffffff bold",
    "arrow": "#a78bfa bold",
    "bottom-toolbar": "bg:#16131f #9ca3af noreverse",
    "tb.backend": "bg:#7c3aed #ffffff bold",
    "tb.route": "bg:#16131f #5eead4",
    "tb.dim": "bg:#16131f #6b7280",
    "tb.ok": "bg:#16131f #4ade80",
    "tb.warn": "bg:#16131f #fbbf24",
    "tb.err": "bg:#16131f #f87171",
    "completion-menu.completion": "bg:#1f1b2e #d1d5db",
    "completion-menu.completion.current": "bg:#7c3aed #ffffff",
    "completion-menu.meta.completion": "bg:#16131f #9ca3af",
    "completion-menu.meta.completion.current": "bg:#5b21b6 #ffffff",
}


class Repl:
    def __init__(self, cfg, backend=None, approval=None, session=None,
                 verbose=False):
        self.cfg = cfg
        self.ui = TerminalUI(verbose=verbose)
        self.console = self.ui.console
        self.agent = QGentSession(cfg, self.ui, backend=backend,
                                  approval=approval, session=session)
        self.prompt_session = None

    # -- setup ------------------------------------------------------------------
    def _make_prompt(self):
        if not sys.stdin.isatty():
            return None
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.styles import Style

        names = [*sorted(COMMANDS), "/quit"]

        class SlashCompleter(Completer):
            def get_completions(self, document, _event):
                text = document.text_before_cursor
                if not text.startswith("/"):
                    return
                if text.startswith("/skill "):
                    word = text[len("/skill "):]
                    for skill in skills.list_skills():
                        if skill["name"].startswith(word):
                            yield Completion(skill["name"], -len(word))
                    return
                head, _, rest = text.partition(" ")
                if _ and head in _SUBCOMMANDS and " " not in rest:
                    for sub in _SUBCOMMANDS[head]:
                        if sub.startswith(rest):
                            yield Completion(sub, -len(rest))
                    return
                for name in names:
                    if name.startswith(text) and " " not in text:
                        yield Completion(name, -len(text), display_meta=COMMANDS.get(name, ""))

        bindings = KeyBindings()

        @bindings.add("escape", "enter")
        def _newline(event):
            event.current_buffer.insert_text("\n")

        config.ensure_home()
        return PromptSession(history=FileHistory(config.path("history")),
                             completer=SlashCompleter(), key_bindings=bindings,
                             bottom_toolbar=self._toolbar, style=Style.from_dict(PROMPT_STYLE),
                             complete_while_typing=True)

    def _toolbar(self):
        agent = self.agent
        mode = router.Router(self.cfg).mode()
        pinned = (self.cfg.get("models") or {}).get(agent.backend_name)
        route = f"model {pinned}" if pinned else f"auto-route ({mode})"
        if mode == "off" and not pinned:
            route = "CLI default model"
        state = agent.engine.state
        if state == "ready":
            engine = ("tb.ok", "● QGIS " + str(agent.engine.info.get("qgis_version", ""))
                      .split("-")[0])
        elif state == "starting":
            engine = ("tb.warn", "◌ QGIS starting")
        elif state == "failed":
            engine = ("tb.err", "✗ QGIS unavailable")
        else:
            engine = ("tb.dim", "○ QGIS idle")
        approval_style = "tb.warn" if agent.hub.mode == "auto" else "tb.dim"
        return [("class:tb.backend", f" {agent.backend_name} "),
                ("class:tb.route", f"  ⚙ {route}"),
                ("class:tb.dim", "   "), ("class:" + engine[0], engine[1]),
                ("class:tb.dim", "   "), ("class:" + approval_style,
                                          f"approval {agent.hub.mode}"),
                ("class:tb.dim", f"   {short_path(agent.cwd, 40)} ")]

    def _banner(self):
        agent = self.agent
        exe = cli_detect.find(agent.backend_name)
        mode = router.Router(self.cfg).mode()
        pinned = (self.cfg.get("models") or {}).get(agent.backend_name)
        if not exe:
            backend = (f"{agent.backend_name} — not installed, run /doctor", WARN)
        else:
            backend = (agent.backend_name + (f" · pinned {pinned}" if pinned else ""), "bold")
        servers = mcp_registry.servers(self.cfg, agent.hub.bridge_env(), live=False)
        names = list(servers)
        if mcp_registry.live_qgis_listening() and "qgis_live" not in names:
            names.append("qgis_live")
        bundled = sum(1 for s in skills.list_skills() if s["source"] == "bundled")
        user = sum(1 for s in skills.list_skills() if s["source"] == "user")
        rows = [("◆", "backend", backend[0], backend[1]),
                ("⚙", "routing", {"auto": "auto — a fast model sizes every turn",
                                  "ai": "ai — every turn is routed by a model",
                                  "heuristic": "heuristic — keyword rules",
                                  "off": "off — CLI defaults"}[mode], ACCENT_2),
                ("▣", "engine", "headless QGIS · warming up", ""),
                ("⇄", "mcp", ", ".join(names), ""),
                ("✦", "skills", f"{bundled} bundled" + (f" · {user} installed" if user else ""),
                 ""),
                ("⌂", "folder", short_path(agent.cwd), DIM),
                ("⛨", "approval", agent.hub.mode, WARN if agent.hub.mode == "auto" else "")]
        if agent.session.turns:
            rows.append(("↺", "resumed", f"{agent.session.title}  ({agent.session.id})", DIM))
        banner(self.console, {"version": __version__, "rows": rows})

    # -- loop ---------------------------------------------------------------------
    def run(self, initial_prompt=None):
        self.agent.start()
        try:
            self._banner()
            if self.agent.session.project:
                self._reopen()
            self.prompt_session = self._make_prompt()
            if initial_prompt:
                self._turn(initial_prompt)
            while True:
                try:
                    text = self._read()
                except KeyboardInterrupt:
                    continue
                except EOFError:
                    break
                text = text.strip()
                if not text:
                    continue
                if text.startswith("/"):
                    if self._command(text) == "exit":
                        break
                elif text.startswith("!"):
                    self._shell(text[1:].strip())
                else:
                    self._turn(text)
        finally:
            self.agent.close()
            if self.agent.session.turns:
                self.console.print(Text(f"Session saved: {self.agent.session.id}  "
                                        f"(qgent --resume {self.agent.session.id})",
                                        style=DIM))
        return 0

    def _read(self):
        if self.prompt_session is None:
            line = sys.stdin.readline()
            if not line:
                raise EOFError
            return line
        return self.prompt_session.prompt(
            [("class:badge", " qgent "), ("class:arrow", " › ")], multiline=False)

    def _turn(self, text):
        self.console.print()
        result = self.agent.run_turn(text)
        if result.cancelled:
            self.console.print(Text("Interrupted. The engine keeps its state; "
                                    "continue or /new.", style=DIM))
        self.console.print()

    def _reopen(self):
        project = self.agent.session.project
        self.console.print(Text(f"Reopening {project} …", style=DIM))
        outcome = self.agent.reopen_project()
        if outcome and outcome[0]:
            self.console.print(Text("Project reopened in the engine.", style=DIM))
        elif outcome:
            self.console.print(Text("Could not reopen it: " + outcome[1][:300],
                                    style="yellow"))

    def _shell(self, command):
        if not command:
            return
        subprocess.run(command, shell=True, cwd=self.agent.cwd)

    # -- slash commands -------------------------------------------------------------
    def _command(self, text):
        name, _, arg = text.partition(" ")
        arg = arg.strip()
        handler = getattr(self, "cmd_" + name[1:].replace("-", "_"), None)
        if name in ("/exit", "/quit"):
            return "exit"
        if handler is None:
            self.console.print(Text(f"Unknown command {name}. /help lists them.",
                                    style="yellow"))
            return None
        return handler(arg)

    def cmd_help(self, _arg):
        table = Table(show_header=False, box=None, padding=(0, 2), pad_edge=False)
        table.add_column(no_wrap=True)
        table.add_column(style=DIM)
        for group, commands in COMMAND_GROUPS:
            table.add_row(Text(group, style=f"bold {ACCENT_2}"), "")
            for name, help_text in commands.items():
                table.add_row(Text("  " + name, style=ACCENT), Text(help_text))
            table.add_row("", "")
        table.add_row(Text("  !<cmd>", style=ACCENT), "run a shell command (not sent to the agent)")
        table.add_row(Text("  alt+enter", style=ACCENT), "new line   ·   ctrl+c interrupt a turn")
        self.console.print(table)

    def cmd_route(self, _arg):
        decision = self.agent.last_route
        if not decision:
            self.console.print(Text("No turn routed yet. Mode: "
                                    + router.Router(self.cfg).mode(), style=DIM))
            return
        self.console.print(route_renderable(decision, self.agent.backend_name))
        table = Table(box=None, padding=(0, 2), show_header=True,
                      header_style=f"bold {ACCENT}")
        for column in ("role", "model", "effort"):
            table.add_column(column)
        for role in router.ROLES:
            if self.agent.backend_name == "codex" and role != "supervisor":
                continue
            entry = decision["roles"].get(role) or {}
            effort = entry.get("effort", "") if role == "supervisor" else "(CLI default)"
            table.add_row(role, entry.get("model") or "default", effort or "default")
        self.console.print(table)
        self.console.print(Text(f"  reason: {decision.get('reason', '')}", style=FAINT))

    def cmd_router(self, arg):
        if arg not in router.MODES:
            self.console.print(Text.assemble(("router: ", DIM),
                                             (router.Router(self.cfg).mode(), ACCENT_2),
                                             ("   (auto | ai | heuristic | off)", FAINT)))
            return
        self.cfg["router"] = arg
        self.console.print(Text(f"router → {arg} (this session; `qgent config set router "
                                f"{arg}` to keep it)", style=DIM))

    def cmd_clear(self, _arg):
        self.console.clear()

    def cmd_new(self, _arg):
        self.agent.new_conversation()
        self.console.print(Text("New conversation. The engine's project and "
                                "layers are unchanged.", style=DIM))

    def cmd_sessions(self, _arg):
        found = sessions.list_sessions(cwd=self.agent.cwd, limit=20)
        if not found:
            self.console.print(Text("No saved sessions for this folder.", style=DIM))
            return
        table = Table(box=None, padding=(0, 2))
        for column in ("id", "backend", "turns", "title"):
            table.add_column(column, style=DIM if column != "title" else None)
        for item in found:
            table.add_row(Text(item.id), Text(item.backend), str(item.turns), Text(item.title))
        self.console.print(table)

    def cmd_resume(self, arg):
        target = sessions.load(arg) if arg else sessions.latest(self.agent.cwd)
        if target is None:
            self.console.print(Text("No such session. /sessions lists them.", style="yellow"))
            return
        self.agent.session = target
        self.agent.backend_name = target.backend
        self.agent._backend = None
        self.console.print(Text(f"Resumed {target.id} — {target.title}", style=DIM))
        if target.project:
            self._reopen()

    def cmd_backend(self, arg):
        if not arg:
            for name in ("codex", "claude"):
                exe = cli_detect.find(name)
                mark = "●" if name == self.agent.backend_name else " "
                self.console.print(f"{mark} {name:<7} {exe or 'not found'}", markup=False)
            return
        if arg not in ("claude", "codex"):
            self.console.print(Text("Backends: claude, codex", style="yellow"))
            return
        if not cli_detect.find(arg):
            self.console.print(Text(f"The {arg} CLI is not installed.", style="yellow"))
            return
        self.agent.switch_backend(arg)
        self.console.print(Text(f"Switched to {arg}: new conversation, same "
                                "engine and project.", style=DIM))

    def cmd_model(self, arg):
        backend = self.agent.backend_name
        models = self.cfg.setdefault("models", {})
        if not arg:
            cat = router.catalog(backend)
            self.console.print(Text.assemble(
                (f"{backend} model: ", DIM),
                (models.get(backend) or "auto (router picks per turn)", ACCENT_2)))
            for entry in cat:
                self.console.print(Text.assemble(("  ", ""), (f"{entry['id']:<16}", ACCENT),
                                                 (f"{entry['tier']:<9}", ACCENT_2),
                                                 (entry["label"][:70], DIM)))
            return
        models[backend] = "" if arg in ("auto", "default") else arg
        self.console.print(Text(f"{backend} model → {models[backend] or 'auto (router)'} "
                                "(this session; `qgent config set models."
                                f"{backend} {arg}` to keep it)", style=DIM))

    def cmd_effort(self, arg):
        levels = ("low", "medium", "high", "xhigh", "max")
        if not arg:
            self.console.print(Text.assemble(("effort: ", DIM),
                                             (self.cfg.get("effort") or "auto (router)",
                                              ACCENT_2)))
            return
        if arg not in (*levels, "auto", "default"):
            self.console.print(Text("effort: " + " | ".join(levels) + " | auto", style=WARN))
            return
        self.cfg["effort"] = "" if arg in ("auto", "default") else arg
        self.console.print(Text(f"effort → {self.cfg['effort'] or 'auto (router)'}",
                                style=DIM))

    def cmd_approval(self, arg):
        if arg not in APPROVAL_MODES:
            self.console.print(f"approval: {self.agent.hub.mode}  (ask | auto | deny)",
                               markup=False)
            return
        self.agent.hub.mode = arg
        self.console.print(Text(f"approval → {arg}", style=DIM))

    def _reload_cfg(self):
        """Pick up servers/skills written by an add/remove, keeping session pins."""
        fresh = config.load()
        for key in ("mcp_servers", "mcp"):
            self.cfg[key] = fresh.get(key, self.cfg.get(key))

    def cmd_mcp(self, arg):
        action, _, rest = arg.partition(" ")
        rest = rest.strip()
        if action == "add":
            market.browse_mcp(self.console, rest or None)
            self._reload_cfg()
            return
        if action in ("remove", "rm"):
            if not rest:
                self.console.print(Text("/mcp remove <name>", style=WARN))
                return
            market.remove_mcp(self.console, rest)
            self._reload_cfg()
            return
        if action == "test":
            name = rest or "qgis"
            spec = mcp_registry.servers(self.cfg, self.agent.hub.bridge_env(),
                                        live=True).get(name)
            if spec is None:
                self.console.print(Text(f"No server named {name!r}.", style=WARN))
                return
            with self.console.status(f"[{ACCENT}]Starting {escape(name)}…"):
                ok, detail = mcp_registry.handshake(spec)
            if ok:
                self.console.print(Text.assemble(("  ✓ ", OK), (name, "bold"),
                                                 (f"  {len(detail)} tools: "
                                                  + ", ".join(detail[:12]), DIM)))
            else:
                self.console.print(Text(f"  ✗ {name}: {detail}", style=WARN))
            return
        live = mcp_registry.live_qgis_listening()
        servers = mcp_registry.servers(self.cfg, self.agent.hub.bridge_env(), live)
        user = set(self.cfg.get("mcp_servers") or {})
        for name, spec in servers.items():
            desc = mcp_registry.DESCRIPTIONS.get(name) or (
                "installed · " + str((self.cfg["mcp_servers"].get(name) or {})
                                     .get("source", "user server")))
            self.console.print(Text.assemble(("  ● ", OK if name not in user else ACCENT_2),
                                             (f"{name:<16}", "bold"), (desc, DIM)))
            where = spec.get("url") or f"{spec.get('command')} {' '.join(spec.get('args', []))}"
            self.console.print(Text(f"      {where}", style=FAINT))
        if "qgis_live" not in servers:
            mode = (self.cfg.get("mcp") or {}).get("qgis_live", "auto")
            self.console.print(Text.assemble(("  ○ ", FAINT), (f"{'qgis_live':<16}", DIM),
                                             (f"({mode}) attaches when QGIS's QGIS MCP plugin "
                                              f"listens on :{mcp_registry.QGIS_LIVE_PORT}",
                                              FAINT)))
        self.console.print(Text("  /mcp add [query] finds GIS servers online · "
                                "/mcp remove <name> · /mcp test <name>", style=FAINT))

    def cmd_skills(self, arg):
        action, _, rest = arg.partition(" ")
        rest = rest.strip()
        if action == "add":
            market.browse_skills(self.console, rest or None)
            return
        if action in ("remove", "rm"):
            if not rest:
                self.console.print(Text("/skills remove <name>", style=WARN))
                return
            market.remove_skill(self.console, rest)
            return
        for skill in skills.list_skills():
            tag = ("bundled", FAINT) if skill["source"] == "bundled" else ("installed", OK)
            self.console.print(Text.assemble(("  ✦ ", ACCENT), (skill["name"], "bold"),
                                             (f"  {tag[0]}", tag[1])))
            self.console.print(Text("      " + skill["description"][:180], style=DIM))
        self.console.print(Text("  /skills add [query] finds GIS skills online · "
                                "/skills remove <name> · /skill <name> reads one",
                                style=FAINT))

    def cmd_skill(self, arg):
        skill, text = skills.read_skill(arg)
        if skill is None:
            self.console.print(Text("Unknown skill; /skills lists them.", style="yellow"))
            return
        self.console.print(Markdown(text))

    def _engine_tool(self, tool, args):
        with self.console.status(f"[{ACCENT}]{tool}…"):
            ok, text = self.agent.engine.call(tool, args)
        return ok, text

    def cmd_project(self, _arg):
        ok, text = self._engine_tool("get_project_context", {})
        if not ok:
            self.console.print(Text(text, style="red"))
            return
        ctx = json.loads(text)
        self.console.print(f"project: {ctx.get('project_file') or '(unsaved)'}"
                           f"  CRS {ctx.get('project_crs') or 'unset'}"
                           + ("  (unsaved changes)" if ctx.get("unsaved_changes") else ""),
                           markup=False)
        for layer in ctx.get("layers", []):
            count = f"{layer['feature_count']} features" if "feature_count" in layer else ""
            self.console.print(Text(f"  • {layer['name']}  {layer.get('crs', '')}  {count}"))
        if ctx.get("layouts"):
            self.console.print("layouts: " + ", ".join(ctx["layouts"]), markup=False)

    def cmd_open(self, arg):
        if not arg:
            self.console.print(Text("/open <project.qgz>", style="yellow"))
            return
        ok, text = self._engine_tool("open_project", {"path": arg.strip('"')})
        self.console.print(Text(text[:1500], style=None if ok else "red"))

    def cmd_save(self, arg):
        args = {"path": arg.strip('"')} if arg else {}
        reasons = self.agent.hub.gate_reasons("save_project", args)
        if reasons:
            answer = self.console.input(f"[yellow]{reasons[0]}. Overwrite? [y/N] [/]")
            if answer.strip().lower() not in ("y", "yes"):
                return
        ok, text = self._engine_tool("save_project", args)
        self.console.print(Text(text[:800], style=None if ok else "red"))

    def cmd_render(self, _arg):
        ok, text = self._engine_tool("render_map_snapshot", {})
        if ok:
            path = json.loads(text).get("snapshot_path")
            self.console.print(f"Rendered: {path}", markup=False)
            if os.name == "nt" and path:
                os.startfile(path)
        else:
            self.console.print(Text(text, style="red"))

    def cmd_engine(self, arg):
        engine = self.agent.engine
        if arg == "restart":
            with self.console.status(f"[{ACCENT}]restarting QGIS engine…"):
                try:
                    engine.restart()
                except Exception as exc:
                    self.console.print(Text(str(exc), style="red"))
                    return
        info = engine.info or {}
        self.console.print(f"engine: {engine.state}  QGIS {info.get('qgis_version', '?')}  "
                           f"pid {info.get('pid', '?')}  algorithms {info.get('algorithms', '?')}",
                           markup=False)
        self.console.print(Text(f"profile: {info.get('profile', '?')}\nlog: {engine.log_path}",
                                style=DIM))
        if engine.error:
            self.console.print(Text(engine.error, style="red"))

    def cmd_doctor(self, _arg):
        from .doctor import run_doctor
        run_doctor(self.cfg, self.console, engine=self.agent.engine)

    def cmd_usage(self, _arg):
        usage = self.agent.session.usage
        line = (f"{usage.get('input_tokens', 0):,} input · "
                f"{usage.get('output_tokens', 0):,} output tokens over "
                f"{self.agent.session.turns} turns")
        if usage.get("cost_usd"):
            line += f" · ${usage['cost_usd']:.3f} (Claude-reported)"
        self.console.print(line)
