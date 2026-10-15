"""Rich terminal rendering for agent turns.

Assistant text streams into a small live region and is committed to the
scrollback one finished paragraph at a time (outside code fences), so long
answers render as markdown without re-drawing the whole reply per token.
"""
import json
import os
import time

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.spinner import Spinner
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

# Palette: violet (QGent) → teal (QGIS green-blue), used as a gradient in the
# logo and as single accents elsewhere.
ACCENT = "#a78bfa"
ACCENT_2 = "#2dd4bf"
TOOL = "#5eead4"
DIM = "grey58"
FAINT = "grey35"
OK = "#4ade80"
WARN = "#fbbf24"
ERR = "#f87171"
SUBAGENT = "#f0abfc"
SERVER_COLORS = {"qgis": "#2dd4bf", "qgis_live": "#4ade80", "fetch": "#60a5fa",
                 "shell": "#fbbf24", "edit_files": "#fbbf24", "web_search": "#60a5fa"}

_LOGO = [
    " ██████   ██████  ███████ ███    ██ ████████",
    "██    ██ ██       ██      ████   ██    ██   ",
    "██    ██ ██   ███ █████   ██ ██  ██    ██   ",
    "██ ▄▄ ██ ██    ██ ██      ██  ██ ██    ██   ",
    " ██████   ██████  ███████ ██   ████    ██   ",
    "    ▀▀                                      ",
]


def _blend(start, end, t):
    a = [int(start[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(end[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b, strict=True))


def logo():
    """The QGENT wordmark with a violet → teal gradient."""
    width = max(len(line) for line in _LOGO)
    out = Text()
    for row, line in enumerate(_LOGO):
        for col, char in enumerate(line):
            out.append(char, style=_blend(ACCENT, ACCENT_2, col / max(1, width - 1)))
        if row < len(_LOGO) - 1:
            out.append("\n")
    return out


def _one_line(value, width=90):
    text = " ".join(str(value).split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _relative(path):
    """Show paths inside the working folder relative to it."""
    try:
        rel = os.path.relpath(path, os.getcwd())
    except ValueError:  # another drive on Windows
        return path
    return path if rel.startswith("..") else (rel if rel != "." else "./")


def describe_input(name, tool_input):
    """Short, human summary of a tool call's arguments."""
    if not isinstance(tool_input, dict):
        return _one_line(tool_input)
    if "code" in tool_input:
        code = str(tool_input["code"]).strip().splitlines()
        first = next((ln for ln in code if ln.strip() and not
                      ln.strip().startswith(("#", "import ", "from "))),
                     code[0] if code else "")
        more = f"  (+{len(code) - 1} lines)" if len(code) > 1 else ""
        return _one_line(first, 70) + more
    for key in ("alg_id", "path", "command", "query", "layer", "action",
                "name", "question", "url", "files", "description", "prompt"):
        if tool_input.get(key):
            value = tool_input[key]
            if key == "path" and isinstance(value, str):
                value = _relative(value)
            if key == "alg_id" and tool_input.get("params"):
                return _one_line(f"{value} {json.dumps(tool_input['params'])}")
            return _one_line(value if not isinstance(value, list)
                             else ", ".join(map(str, value)))
    return _one_line(json.dumps(tool_input, default=str)) if tool_input else ""


def _split_tool(name):
    """'qgis.run_processing' -> ('qgis', 'run_processing')."""
    if "." in name:
        server, _, tool = name.partition(".")
        return server, tool
    return {"shell": ("shell", "run"), "edit_files": ("edit", "files"),
            "web_search": ("web", "search")}.get(name, ("", name))


def complexity_bar(level):
    level = max(0, min(5, int(level or 0)))
    return Text.assemble(("▰" * level, ACCENT), ("▱" * (5 - level), FAINT))


def route_renderable(decision, backend):
    """Two compact lines describing the router's choice."""
    roles = decision.get("roles") or {}
    sup = roles.get("supervisor") or {}
    line = Text("  ◇ ", style=ACCENT)
    if decision.get("source") == "off":
        line.append("route off — CLI defaults", style=DIM)
        return line
    line.append(decision.get("task", ""), style="bold")
    line.append("  ")
    line.append_text(complexity_bar(decision.get("complexity")))
    line.append(f" {decision.get('complexity', 0)}/5   ", style=DIM)
    line.append(sup.get("model") or "default", style=f"bold {ACCENT_2}")
    line.append(" · ", style=DIM)
    line.append(sup.get("effort") or "default", style=ACCENT_2)
    tags = [decision.get("source", "")]
    if decision.get("pinned"):
        tags.append("pinned " + "+".join(decision["pinned"]))
    if decision.get("router_model"):
        tags.append(f"by {decision['router_model']}")
    line.append("   " + " · ".join(t for t in tags if t), style=FAINT)
    lines = [line]
    if backend == "claude":
        team = Text("    team ", style=FAINT)
        for index, (role, label) in enumerate((("scout", "scout"), ("executor", "executor"),
                                               ("verifier", "verifier"))):
            if index:
                team.append(" · ", style=FAINT)
            team.append(f"{label} ", style=DIM)
            team.append((roles.get(role) or {}).get("model") or "inherit", style=TOOL)
        team.append("   delegate" if decision.get("delegate") else "   direct",
                    style=SUBAGENT if decision.get("delegate") else FAINT)
        lines.append(team)
    if decision.get("reason") and decision.get("source") == "ai":
        lines.append(Text("    " + _one_line(decision["reason"], 110), style=FAINT))
    return Group(*lines)


class TerminalUI:
    interactive = True

    def __init__(self, console=None, verbose=False, backend=""):
        self.console = console or Console(highlight=False)
        self.verbose = verbose
        self.backend = backend
        self.live = None
        self.pending = ""
        self.status = "Thinking"
        self.turn_started = 0.0
        self.tool_count = 0
        self.failed = False

    # -- live region ----------------------------------------------------------
    def _renderable(self):
        elapsed = time.time() - self.turn_started
        spinner = Spinner("dots", style=ACCENT, text=Text.assemble(
            (self.status, ACCENT), (f"  {elapsed:3.0f}s", DIM),
            ("  ·  ctrl+c to interrupt", FAINT)))
        if self.pending.strip():
            return Group(Padding(Markdown(self.pending, code_theme="monokai"), (0, 0, 0, 2)),
                         spinner)
        return spinner

    def set_status(self, text):
        self.status = text
        if self.live is not None:
            self.live.refresh()

    def begin_turn(self):
        self.pending = ""
        self.status = "Thinking"
        self.turn_started = time.time()
        self.tool_count = 0
        self.failed = False
        self.live = Live(console=self.console, refresh_per_second=10, transient=True,
                         get_renderable=self._renderable)
        self.live.start()

    def end_turn(self):
        self._commit(force=True)
        if self.live is not None:
            self.live.stop()
            self.live = None

    def _print(self, renderable):
        self.console.print(renderable)

    # -- markdown streaming -----------------------------------------------------
    def _commit(self, force=False):
        text = self.pending
        if not text.strip():
            self.pending = "" if force else text
            return
        cut = len(text) if force else _paragraph_cut(text)
        if cut <= 0:
            return
        chunk, self.pending = text[:cut], text[cut:].lstrip("\n")
        if chunk.strip():
            self._print(Padding(Markdown(chunk.strip("\n"), code_theme="monokai"),
                                (0, 0, 0, 2)))

    # -- events ---------------------------------------------------------------------
    def on_event(self, event):
        kind = event.get("type")
        if kind == "text_delta":
            self.pending += event["text"]
            self._commit()
            self.status = "Writing"
        elif kind == "text":
            if self.pending and not self.pending.endswith("\n"):
                self.pending += "\n\n"
            self.pending += event["text"].rstrip() + "\n\n"
            self._commit()
        elif kind == "route":
            self._print(route_renderable(event["decision"],
                                         event["decision"].get("backend", self.backend)))
            self.status = "Thinking"
        elif kind == "tool_call":
            self._tool_call(event)
        elif kind == "tool_result":
            self._tool_result(event)
            self.status = "Thinking"
        elif kind == "subagent":
            self._commit(force=True)
            if event.get("state") == "started":
                desc = event.get("description") or ""
                self._print(Text.assemble(("  ◆ ", SUBAGENT), (event["name"], f"bold {SUBAGENT}"),
                                          ("  " + _one_line(desc, 70), DIM)))
                self.status = f"{event['name']} working"
            else:
                self._print(Text.assemble(("  ◆ ", SUBAGENT),
                                          (f"{event['name']} done", DIM)))
                self.status = "Thinking"
        elif kind == "notice":
            self._commit(force=True)
            self._print(Text("  ! " + event["text"], style=WARN))
        elif kind == "error":
            self._commit(force=True)
            self.failed = True
            self._print(Panel(Text(event.get("text", "").strip()[-3000:]),
                              title=Text(" error ", style=f"bold {ERR}"), title_align="left",
                              border_style=ERR, box=box.ROUNDED, expand=False))
        elif kind == "cancelled":
            self._commit(force=True)
            self._print(Text("  ■ interrupted", style=WARN))
        elif kind == "done":
            self._commit(force=True)
            self._footer(event.get("usage") or {})
        if self.live is not None:
            self.live.refresh()

    def _tool_call(self, event):
        self._commit(force=True)
        self.tool_count += 1
        name = event.get("name", "tool")
        server, tool = _split_tool(name)
        agent = event.get("agent")
        line = Text("    " if agent else "  ")
        line.append("↳ " if agent else "▸ ", style=SUBAGENT if agent else ACCENT)
        if agent:
            line.append(f"{agent} ", style=SUBAGENT)
        if server:
            line.append(f"{server} ", style=f"bold {SERVER_COLORS.get(server, DIM)}")
        line.append(tool, style="bold")
        summary = describe_input(name, event.get("input"))
        if summary:
            line.append("  " + summary, style=DIM)
        self._print(line)
        if self.verbose and isinstance(event.get("input"), dict) and \
                event["input"].get("code"):
            self._print(Padding(Syntax(event["input"]["code"], "python",
                                       theme="monokai", word_wrap=True),
                                (0, 0, 0, 6)))
        self.status = f"Running {tool}"

    def _tool_result(self, event):
        text = (event.get("text") or "").strip()
        style = ERR if event.get("is_error") else FAINT
        indent = "      " if event.get("agent") else "    "
        if not text:
            self._print(Text(indent + "╰ (no output)", style=style))
            return
        if text[:1] in "{[" and not self.verbose:
            # JSON results: one flattened line says more than "{".
            self._print(Text(indent + "╰ " + _one_line(text, 108), style=style))
            return
        lines = text.splitlines()
        limit = 12 if self.verbose else (4 if event.get("is_error") else 2)
        out = Text()
        for index, line in enumerate(lines[:limit]):
            out.append(indent + ("╰ " if index == 0 else "  ") + _one_line(line, 108) + "\n",
                       style=style)
        if len(lines) > limit:
            out.append(f"{indent}  … {len(lines) - limit} more lines\n", style=FAINT)
        out.rstrip()
        self._print(out)

    def _footer(self, usage):
        elapsed = time.time() - self.turn_started
        bits = [f"{elapsed:.0f}s"]
        if self.tool_count:
            bits.append(f"{self.tool_count} tool" + ("s" if self.tool_count > 1 else ""))
        tokens = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
        if tokens:
            bits.append(f"{tokens / 1000:.1f}k tokens" if tokens >= 1000 else f"{tokens} tokens")
        if usage.get("cost_usd"):
            bits.append(f"${usage['cost_usd']:.3f}")
        if usage.get("models"):
            bits.append("models " + ", ".join(m.replace("claude-", "") for m in usage["models"]))
        mark = ("  ✗ ", ERR) if self.failed else ("  ✓ ", OK)
        self._print(Text.assemble(mark, (" · ".join(bits), FAINT)))

    # -- prompts (main thread, during a turn) -------------------------------------
    def _pause(self):
        self._commit(force=True)
        if self.live is not None:
            self.live.stop()

    def _resume(self):
        if self.live is not None:
            self.live.start()

    def approve(self, request):
        self._pause()
        try:
            body = [Text("The agent wants to run an operation that can delete or "
                         "overwrite data:", style="bold")]
            for reason in request.get("reasons", []):
                body.append(Text("  • " + reason, style=WARN))
            args = request.get("args") or {}
            if args.get("code"):
                code = str(args["code"])
                lines = code.splitlines()
                if len(lines) > 40:
                    code = "\n".join(lines[:40]) + f"\n# … {len(lines) - 40} more lines"
                body.append(Text(""))
                body.append(Syntax(code, "python", theme="monokai", word_wrap=True))
            else:
                body.append(Text(_one_line(json.dumps(args, default=str), 400), style=DIM))
            body.append(Text(""))
            body.append(Text.assemble((" y ", "bold black on #4ade80"), (" allow   ", DIM),
                                      (" n ", "bold black on #f87171"), (" deny   ", DIM),
                                      (" a ", "bold black on #fbbf24"),
                                      (" allow all this session", DIM)))
            title = Text(f" ⚠ approve {request.get('tool')} ", style=f"bold {WARN}")
            self.console.print(Panel(
                Group(*body), title=title, title_align="left", border_style=WARN,
                box=box.ROUNDED, expand=False, padding=(1, 2)))
            while True:
                answer = self.console.input(Text("  › ", style=f"bold {WARN}")).strip().lower()
                if answer in ("y", "yes"):
                    return "yes"
                if answer in ("n", "no", ""):
                    return "no"
                if answer in ("a", "always"):
                    return "always"
        finally:
            self._resume()

    def ask(self, question, options, allow_other):
        self._pause()
        try:
            body = [Text(question, style="bold"), Text("")]
            for index, option in enumerate(options, 1):
                body.append(Text.assemble((f" {index} ", f"bold black on {ACCENT}"),
                                          ("  " + option, "")))
            if allow_other:
                body.append(Text.assemble((f" {len(options) + 1} ", "bold black on grey50"),
                                          ("  other — type your own answer", DIM)))
            title = Text(" ? question ", style=f"bold {ACCENT}")
            self.console.print(Panel(Group(*body), title=title, title_align="left",
                                     border_style=ACCENT, box=box.ROUNDED, expand=False,
                                     padding=(1, 2)))
            while True:
                raw = self.console.input(Text("  › ", style=f"bold {ACCENT}")).strip()
                if raw.isdigit() and 1 <= int(raw) <= len(options):
                    return options[int(raw) - 1]
                if raw.isdigit() and allow_other and int(raw) == len(options) + 1:
                    raw = self.console.input("  your answer: ").strip()
                if raw and (allow_other or raw in options):
                    return raw
                if not raw:
                    return None
        finally:
            self._resume()


def _paragraph_cut(text):
    """Index just past the last blank line that is outside a code fence."""
    cut, in_fence, pos = 0, False, 0
    lines = text.split("\n")
    for index, line in enumerate(lines[:-1]):  # last line may be partial
        pos += len(line) + 1
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        elif not line.strip() and not in_fence and index > 0:
            cut = pos
    return cut


class PlainUI:
    """One-shot output (``qgent -p``): final text to stdout, progress to stderr."""

    interactive = False

    def __init__(self, verbose=False, json_output=False, stream=None, err=None):
        import sys
        self.verbose = verbose
        self.json_output = json_output
        self.out = stream or sys.stdout
        self.err = err or sys.stderr

    def begin_turn(self):
        pass

    def end_turn(self):
        pass

    def set_status(self, _text):
        pass

    def on_event(self, event):
        kind = event.get("type")
        if kind == "route" and self.verbose:
            decision = event["decision"]
            sup = decision["roles"]["supervisor"]
            self.err.write(f"◇ route {decision.get('task')} {decision.get('complexity')}/5 → "
                           f"{sup.get('model') or 'default'} · {sup.get('effort') or 'default'}"
                           f" ({decision.get('source')})\n")
        elif kind == "tool_call" and self.verbose:
            self.err.write(f"▸ {event.get('name')}  "
                           f"{describe_input(event.get('name'), event.get('input'))}\n")
        elif kind == "tool_result" and self.verbose:
            text = (event.get("text") or "").strip()
            if text[:1] not in "{[":
                text = text.splitlines()[0] if text else ""
            self.err.write(f"  ╰ {_one_line(text, 110) if text else '(no output)'}\n")
        elif kind in ("notice", "error"):
            self.err.write(("error: " if kind == "error" else "! ")
                           + event.get("text", "") + "\n")
        elif kind == "subagent" and self.verbose:
            self.err.write(f"◆ {event.get('name')} {event.get('state')}\n")
        elif kind == "done" and self.verbose and (event.get("usage") or {}).get("models"):
            self.err.write("✓ models used: " + ", ".join(event["usage"]["models"]) + "\n")
        self.err.flush()

    def approve(self, _request):
        return "no"

    def ask(self, *_args):
        return None


def banner(console, info):
    """Welcome screen: gradient wordmark beside the session facts."""
    facts = Table.grid(padding=(0, 1))
    facts.add_column(style=DIM, no_wrap=True)
    facts.add_column(overflow="fold")
    for icon, label, value, style in info["rows"]:
        facts.add_row(Text(f"{icon} {label}", style=DIM), Text(str(value), style=style or ""))
    title = Text.assemble(("QGent CLI ", f"bold {ACCENT}"), (info["version"], "bold"),
                          ("  ·  a QGIS agent in your terminal", DIM))
    if console.width >= 104:
        grid = Table.grid(padding=(0, 3))
        grid.add_column(no_wrap=True)
        grid.add_column()
        grid.add_row(logo(), Group(title, Text(""), facts))
        body = grid
    else:
        body = Group(logo(), Text(""), title, Text(""), facts)
    tips = Text.assemble(("  /help", ACCENT), (" commands  ", FAINT),
                         ("/mcp add", ACCENT), (" GIS servers  ", FAINT),
                         ("/skills add", ACCENT), (" GIS skills  ", FAINT),
                         ("alt+enter", ACCENT), (" newline  ", FAINT),
                         ("ctrl+d", ACCENT), (" exit", FAINT))
    console.print()
    console.print(Panel(body, border_style=FAINT, box=box.ROUNDED, expand=False,
                        padding=(1, 2)))
    console.print(tips)
    console.print()


def short_path(path, width=60):
    path = str(path)
    home = os.path.expanduser("~")
    if path.lower().startswith(home.lower()):
        path = "~" + path[len(home):]
    return path if len(path) <= width else "…" + path[-(width - 1):]
