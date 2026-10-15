"""``qgent`` command line.

    qgent                       interactive agent in this folder
    qgent "make a 500 m buffer"  start interactive with a first message
    qgent -p "..."              one-shot: answer, then exit (scriptable)
    qgent -c | --resume [ID]    continue the last / a saved session
    qgent doctor | mcp | skills | sessions | config | native | tool
"""
import argparse
import json
import os
import sys

from . import __version__, cli_detect, config, sessions, skills

SUBCOMMANDS = ("doctor", "mcp", "skills", "sessions", "config", "native", "tool", "route")


def _utf8_console():
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _enable_ctrl_c():
    """Make Ctrl+C reach qgent even when the launching shell disabled it.

    Windows passes "ignore Ctrl+C" down from parent to child process (Git Bash
    and some IDE terminals launch programs that way); with it, Ctrl+C could
    never interrupt a turn.
    """
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
        except (OSError, AttributeError):
            pass


def _main_parser():
    parser = argparse.ArgumentParser(
        prog="qgent", description="QGent CLI — a QGIS AI agent for the terminal. "
        "Subcommands: " + ", ".join(SUBCOMMANDS) + " (qgent <cmd> --help).")
    parser.add_argument("prompt", nargs="*", help="first message (or the -p task)")
    parser.add_argument("-p", "--print", action="store_true", dest="one_shot",
                        help="run one task non-interactively and print the answer")
    parser.add_argument("-b", "--backend", choices=["claude", "codex"],
                        help="agent CLI to use (default: config, else installed)")
    parser.add_argument("-m", "--model", help="model for this run")
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"],
                        help="reasoning effort for this run")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="approve destructive QGIS operations without asking")
    parser.add_argument("--approval", choices=["ask", "auto", "deny"],
                        help="destructive-operation policy (default: ask; -p: deny)")
    parser.add_argument("-c", "--continue", action="store_true", dest="cont",
                        help="continue the most recent session in this folder")
    parser.add_argument("-r", "--resume", nargs="?", const="", metavar="ID",
                        help="resume a saved session (latest here if no ID)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show tool code and longer results")
    parser.add_argument("--json", action="store_true",
                        help="with -p: print a JSON result object")
    parser.add_argument("-C", "--cwd", help="working folder (default: current)")
    parser.add_argument("--version", action="version",
                        version=f"qgent {__version__}")
    return parser


def _load_cfg(args=None):
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        print(f"qgent: bad config: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    if args is not None:
        backend = getattr(args, "backend", None)
        if getattr(args, "model", None):
            name = backend or cli_detect.pick_backend(cfg)
            cfg.setdefault("models", {})[name] = args.model
        if getattr(args, "effort", None):
            cfg["effort"] = args.effort
    return cfg


def main(argv=None):
    _utf8_console()
    _enable_ctrl_c()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in SUBCOMMANDS:
        return _subcommand(argv[0], argv[1:])
    args = _main_parser().parse_args(argv)
    if args.cwd:
        os.chdir(os.path.abspath(os.path.expanduser(args.cwd)))
    cfg = _load_cfg(args)
    session = None
    if args.cont or args.resume is not None:
        session = (sessions.load(args.resume) if args.resume
                   else sessions.latest(os.getcwd()))
        if session is None:
            print("qgent: no saved session to resume"
                  + (f" matching {args.resume!r}" if args.resume else " in this folder"),
                  file=sys.stderr)
            return 1
        if os.path.isdir(session.cwd) and os.path.normcase(session.cwd) != \
                os.path.normcase(os.getcwd()):
            os.chdir(session.cwd)
    approval = "auto" if args.yes else args.approval
    prompt = " ".join(args.prompt).strip()
    if args.one_shot:
        if not prompt and not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        if not prompt:
            print("qgent -p: give a task as an argument or on stdin", file=sys.stderr)
            return 2
        return _one_shot(cfg, args, prompt, approval or "deny", session)
    from .repl import Repl
    return Repl(cfg, backend=args.backend, approval=approval, session=session,
                verbose=args.verbose).run(initial_prompt=prompt or None)


def _one_shot(cfg, args, prompt, approval, session):
    from .agent import QGentSession
    from .ui.terminal import PlainUI
    ui = PlainUI(verbose=args.verbose, json_output=args.json)
    agent = QGentSession(cfg, ui, backend=args.backend, approval=approval,
                         session=session)
    agent.start()
    try:
        if session and session.project:
            agent.reopen_project()
        result = agent.run_turn(prompt)
    finally:
        agent.close()
    if args.json:
        print(json.dumps({"ok": result.ok, "result": result.text,
                          "error": result.error or None,
                          "session_id": agent.session.id,
                          "backend": agent.backend_name,
                          "backend_session": agent.session.backend_session,
                          "usage": result.usage}, indent=1))
    elif result.text:
        print(result.text)
    if result.ok:
        return 0
    if not args.json and result.error:
        print(f"qgent: {result.error}", file=sys.stderr)
    return 130 if result.cancelled else 1


# -- subcommands --------------------------------------------------------------
def _subcommand(name, argv):
    return globals()["_cmd_" + name](argv)


def _console():
    from rich.console import Console
    return Console(highlight=False)


def _esc(value):
    from rich.markup import escape
    return escape(str(value))


def _cmd_doctor(argv):
    parser = argparse.ArgumentParser(prog="qgent doctor",
                                     description="check CLIs, QGIS, engine and MCP")
    parser.add_argument("--refresh", action="store_true",
                        help="re-detect QGIS instead of using the cached result")
    args = parser.parse_args(argv)
    from .doctor import run_doctor
    return 0 if run_doctor(_load_cfg(), _console(), refresh=args.refresh) else 1


def _cmd_mcp(argv):
    parser = argparse.ArgumentParser(prog="qgent mcp", description="MCP servers")
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("list", help="servers QGent attaches to agent turns")
    add = sub.add_parser("add", help="find GIS MCP servers in the MCP Registry and "
                         "install the ones you pick")
    add.add_argument("query", nargs="*", help="narrow the search (e.g. elevation)")
    add.add_argument("--refresh", action="store_true", help="ignore the 24 h cache")
    remove = sub.add_parser("remove", help="remove an installed MCP server")
    remove.add_argument("name")
    test = sub.add_parser("test", help="handshake with a server and list its tools")
    test.add_argument("name", nargs="?", default="qgis")
    serve = sub.add_parser("serve", help="run QGent's QGIS engine as a stdio MCP "
                           "server for other clients (Claude Desktop, other agents)")
    serve.add_argument("--allow-destructive", action="store_true",
                       help="run destructive operations instead of refusing them")
    snippet = sub.add_parser("config", help="print a client config snippet for "
                             "`qgent mcp serve`")
    snippet.add_argument("--client", choices=["claude", "codex", "json"],
                         default="json")
    args = parser.parse_args(argv)
    from . import mcp_registry
    cfg = _load_cfg()
    if args.action == "serve":
        from .bridge import serve as serve_mcp
        serve_mcp(approval="auto" if args.allow_destructive else "deny")
        return 0
    if args.action == "config":
        exe = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else "qgent"
        command = exe if os.path.basename(exe).lower().startswith("qgent") else "qgent"
        if args.client == "claude":
            print(f'claude mcp add qgent -- "{command}" mcp serve')
        elif args.client == "codex":
            print(f'[mcp_servers.qgent]\ncommand = {json.dumps(command)}\n'
                  'args = ["mcp", "serve"]\ntool_timeout_sec = 1800')
        else:
            print(json.dumps({"mcpServers": {"qgent": {
                "command": command, "args": ["mcp", "serve"]}}}, indent=1))
        return 0
    if args.action == "test":
        return _mcp_test(cfg, args.name)
    if args.action == "add":
        from .ui import market
        market.browse_mcp(_console(), " ".join(args.query) or None, refresh=args.refresh)
        return 0
    if args.action == "remove":
        from .ui import market
        return 0 if market.remove_mcp(_console(), args.name) else 1
    console = _console()
    live = mcp_registry.live_qgis_listening()
    servers = mcp_registry.servers(cfg, {"QGENT_HUB_PORT": "<session>"}, live)
    for name, spec in servers.items():
        console.print(f"[bold]{_esc(name)}[/]  "
                      + _esc(mcp_registry.DESCRIPTIONS.get(name, "user server")))
        where = spec.get("url") or f"{spec['command']} {' '.join(spec['args'])}"
        console.print(f"   {where}", style="grey58", markup=False)
    if "qgis_live" not in servers:
        console.print("[bold]qgis_live[/]  not attached: no QGIS MCP plugin listening "
                      f"on localhost:{mcp_registry.QGIS_LIVE_PORT} (mode "
                      f"{(cfg.get('mcp') or {}).get('qgis_live', 'auto')})", style="grey58")
    return 0


def _mcp_test(cfg, name):
    from . import mcp_registry
    from .engine_client import EngineClient
    from .hub import Hub
    console = _console()
    if name == "qgis":
        from .doctor import bridge_handshake
        engine = EngineClient(cfg)
        hub = Hub(engine, mode="deny").start()
        try:
            with console.status("starting the QGIS engine…"):
                ok, detail = bridge_handshake(hub, timeout=240)
        finally:
            hub.stop()
            engine.stop()
        console.print(("[green]ok[/] " if ok else "[red]failed[/] ") + _esc(detail))
        return 0 if ok else 1
    servers = mcp_registry.servers(dict(cfg, mcp=dict(cfg.get("mcp") or {},
                                                      qgis_live="on")), {})
    spec = servers.get(name)
    if spec is None:
        console.print(f"[red]unknown server {_esc(repr(name))}[/]; known: "
                      + _esc(", ".join(servers)))
        return 1
    with console.status(f"starting {_esc(name)}…"):
        ok, detail = mcp_registry.handshake(spec)
    if not ok:
        console.print(f"[red]{_esc(name)}: {_esc(detail)}[/]")
        return 1
    console.print(f"[green]ok[/] {_esc(name)}: {len(detail)} tools — "
                  + _esc(", ".join(detail[:20])) + (" …" if len(detail) > 20 else ""))
    return 0


def _cmd_skills(argv):
    parser = argparse.ArgumentParser(
        prog="qgent skills", description="GIS playbooks. `add [query]` searches skills.sh "
        "for GIS skills and installs the ones you pick; `remove <name>` uninstalls.")
    parser.add_argument("action", nargs="?", default="list",
                        choices=["list", "show", "path", "add", "remove"])
    parser.add_argument("name", nargs="*", help="skill name (show/remove) or search query (add)")
    parser.add_argument("--refresh", action="store_true", help="add: ignore the 24 h cache")
    args = parser.parse_args(argv)
    console = _console()
    args.name = " ".join(args.name) or None
    if args.action == "add":
        from .ui import market
        market.browse_skills(console, args.name, refresh=args.refresh)
        return 0
    if args.action == "remove":
        from .ui import market
        if not args.name:
            print("usage: qgent skills remove <name>", file=sys.stderr)
            return 2
        return 0 if market.remove_skill(console, args.name) else 1
    if args.action == "path":
        for source, folder in skills.skill_dirs():
            print(f"{source}: {folder}")
        return 0
    if args.action == "show":
        skill, text = skills.read_skill(args.name)
        if skill is None:
            print(f"unknown skill {args.name!r}", file=sys.stderr)
            return 1
        from rich.markdown import Markdown
        console.print(Markdown(text))
        return 0
    for skill in skills.list_skills():
        console.print(f"[bold]{_esc(skill['name'])}[/] [grey58]({skill['source']})[/]")
        console.print("   " + skill["description"], style="grey58", markup=False)
    return 0


def _cmd_sessions(argv):
    parser = argparse.ArgumentParser(prog="qgent sessions")
    parser.add_argument("--all", action="store_true", help="every folder")
    args = parser.parse_args(argv)
    found = sessions.list_sessions(cwd=None if args.all else os.getcwd())
    if not found:
        print("no saved sessions" + ("" if args.all else " for this folder (--all)"))
        return 0
    for item in found:
        print(f"{item.id}  {item.backend:<6} {item.turns:>3} turns  {item.title}"
              + (f"  [{item.cwd}]" if args.all else ""))
    return 0


def _cmd_config(argv):
    parser = argparse.ArgumentParser(prog="qgent config")
    parser.add_argument("action", nargs="?", default="show",
                        choices=["show", "path", "set"])
    parser.add_argument("key", nargs="?")
    parser.add_argument("value", nargs="?")
    args = parser.parse_args(argv)
    if args.action == "path":
        print(config.config_path())
        return 0
    cfg = _load_cfg()
    if args.action == "set":
        if not args.key or args.value is None:
            print("usage: qgent config set <key> <value>  (e.g. backend claude, "
                  "models.codex gpt-5.6-sol, mcp.fetch false)", file=sys.stderr)
            return 2
        config.set_value(cfg, args.key, args.value)
        config.save(cfg)
        print(f"{args.key} = {json.dumps(config.parse_scalar(args.value))}")
        return 0
    print(config.dumps(cfg), end="")
    return 0


def _cmd_native(argv):
    parser = argparse.ArgumentParser(
        prog="qgent native", description="open the full Claude Code / Codex TUI "
        "with QGent's engine, MCP servers, skills and rules")
    parser.add_argument("-b", "--backend", choices=["claude", "codex"])
    parser.add_argument("rest", nargs=argparse.REMAINDER,
                        help="extra arguments passed to the CLI")
    args = parser.parse_args(argv)
    cfg = _load_cfg()
    from .native import run_native
    return run_native(cfg, args.backend or cli_detect.pick_backend(cfg), args.rest)


def _cmd_route(argv):
    parser = argparse.ArgumentParser(
        prog="qgent route", description="show how QGent's router would run a request "
        "(task, complexity, model and effort per role) without running it")
    parser.add_argument("prompt", nargs="+")
    parser.add_argument("-b", "--backend", choices=["claude", "codex"])
    parser.add_argument("--mode", choices=["auto", "ai", "heuristic", "off"])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    from . import router
    from .backends.codex import user_codex_model
    from .ui.terminal import route_renderable
    cfg = _load_cfg()
    if args.mode:
        cfg["router"] = args.mode
    backend = args.backend or cli_detect.pick_backend(cfg)
    console = _console()
    with console.status("routing…"):
        decision = router.Router(cfg).decide(
            " ".join(args.prompt), backend, cli_detect.find(backend),
            user_model=user_codex_model()[0] if backend == "codex" else "")
    if args.json:
        print(json.dumps(decision, indent=1))
        return 0
    console.print(route_renderable(decision, backend))
    if decision.get("source") != "ai":  # the AI's reason is already shown
        console.print(f"    {decision.get('reason', '')}", style="grey35", markup=False)
    return 0


def _cmd_tool(argv):
    parser = argparse.ArgumentParser(
        prog="qgent tool", description="run one QGent tool directly (no agent); "
        "e.g. qgent tool inspect_dataset '{\"path\": \".\"}'")
    parser.add_argument("name")
    parser.add_argument("args", nargs="?", default="{}", help="JSON arguments")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="allow destructive operations")
    args = parser.parse_args(argv)
    from .engine_client import EngineClient
    from .hub import Hub
    from .tool_catalog import TOOL_NAMES
    if args.name not in TOOL_NAMES:
        print(f"unknown tool; tools: {', '.join(sorted(TOOL_NAMES))}", file=sys.stderr)
        return 2
    try:
        tool_args = json.loads(args.args)
    except json.JSONDecodeError as exc:
        print(f"bad JSON arguments: {exc}", file=sys.stderr)
        return 2
    engine = EngineClient(_load_cfg())
    hub = Hub(engine, mode="auto" if args.yes else "deny")
    try:
        ok, text = hub.handle(args.name, tool_args)
    finally:
        engine.stop()
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
