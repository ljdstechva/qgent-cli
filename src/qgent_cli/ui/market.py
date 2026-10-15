"""Interactive browse-and-install screens for GIS MCP servers and skills."""
import getpass
import os
import re
import textwrap

from rich import box
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from .. import config, marketplace, mcp_registry, skills
from .terminal import ACCENT, ACCENT_2, DIM, ERR, FAINT, OK, WARN, short_path

_KIND_LABEL = {"pypi": "uvx", "npm": "npx", "remote": "remote", "oci": "docker"}


def _summary(text, width=72):
    text = " ".join(str(text or "").split())
    return textwrap.shorten(text, width=150, placeholder="…") if text else ""


def parse_selection(raw, count):
    """'1,3-5 7' -> [1, 3, 4, 5, 7] (1-based, in range, unique, ordered)."""
    picked = []
    for part in re.split(r"[,\s]+", raw.strip()):
        if not part:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            raise ValueError(f"not a number or range: {part!r}")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        for index in range(min(start, end), max(start, end) + 1):
            if 1 <= index <= count and index not in picked:
                picked.append(index)
    return picked


def _ask_selection(console, count, noun):
    raw = console.input(Text.assemble(
        ("  › ", f"bold {ACCENT}"),
        (f"install which {noun}? numbers like 1,3 or 2-4 — Enter to cancel: ", DIM)))
    try:
        return parse_selection(raw, count)
    except ValueError as exc:
        console.print(Text(f"  {exc}", style=ERR))
        return []


def _secret_input(console, prompt):
    try:
        from prompt_toolkit import prompt as pt_prompt
        return pt_prompt(prompt, is_password=True)
    except Exception:
        return getpass.getpass(prompt)


# -- MCP servers --------------------------------------------------------------------
def browse_mcp(console, query=None, refresh=False):
    with console.status(f"[{ACCENT}]Searching the MCP Registry for GIS servers"
                        "… (first search takes about a minute, then it is cached)"):
        try:
            items = marketplace.search_mcp(query, refresh=refresh)
        except marketplace.MarketplaceError as exc:
            console.print(Text(f"  {exc}", style=ERR))
            return []
    if not items:
        console.print(Text("  No GIS MCP servers found" + (f" for {query!r}" if query else ""),
                           style=DIM))
        return []
    installed = set(config.load().get("mcp_servers") or {})
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False,
                  header_style=f"bold {ACCENT}", title_justify="left",
                  title=Text(f"GIS MCP servers · MCP Registry{' · ' + query if query else ''}",
                             style="bold"))
    table.add_column("#", justify="right", style=ACCENT, no_wrap=True)
    table.add_column("server", style="bold", overflow="fold", max_width=34)
    table.add_column("runs via", style=ACCENT_2, no_wrap=True)
    table.add_column("what it does", overflow="fold")
    for index, item in enumerate(items, 1):
        how = ", ".join(_KIND_LABEL.get(k, k) for k in item["kinds"]) or "?"
        name = Text(item["title"])
        name.append("\n" + item["id"], style=FAINT)
        if marketplace.server_slug(item) in installed:
            name.append("  installed", style=OK)
        table.add_row(str(index), name, Text(how) if item["installable"] else
                      Text(how + " (unavailable)", style=WARN), Text(_summary(item["summary"])))
    console.print(table)
    console.print(Text("  Third-party servers run code on your machine or send data to "
                       "their service — install only what you trust.", style=FAINT))
    picks = _ask_selection(console, len(items), "servers")
    done = []
    for index in picks:
        result = install_mcp_interactive(console, items[index - 1])
        if result:
            done.append(result)
    return done


def install_mcp_interactive(console, item):
    option, needed = marketplace.requirements(item)
    title = item["title"]
    if option is None:
        console.print(Text(f"  ✗ {title}: no way to run it here (needs "
                           + ", ".join(item["kinds"]) + ")", style=ERR))
        return None
    values = {}
    if needed:
        console.print(Text(f"  {title} needs {len(needed)} setting(s):", style="bold"))
    for need in needed:
        if need["description"]:
            console.print(Text(f"    {need['key']}: {_summary(need['description'], 100)}",
                               style=DIM))
        prompt = f"    {need['key']} = "
        value = _secret_input(console, prompt) if need["secret"] else console.input(prompt)
        if not value.strip():
            console.print(Text(f"  skipped {title}: {need['key']} is required", style=WARN))
            return None
        values[need["key"]] = value.strip()
    try:
        slug, spec = marketplace.install_mcp(item, values)
    except marketplace.MarketplaceError as exc:
        console.print(Text(f"  ✗ {title}: {exc}", style=ERR))
        return None
    with console.status(f"[{ACCENT}]Starting {escape(slug)} to check it works…"):
        ok, detail = mcp_registry.handshake(spec)
    if ok:
        tools = ", ".join(detail[:8]) + (" …" if len(detail) > 8 else "")
        console.print(Text.assemble(("  ✓ ", OK), (slug, "bold"),
                                    (f"  installed · {len(detail)} tools: {tools}", DIM)))
    else:
        console.print(Text.assemble(("  ! ", WARN), (slug, "bold"),
                                    (f"  added to config, but the check failed: {detail}",
                                     WARN)))
        console.print(Text(f"    Fix its settings in {config.config_path()} or remove it "
                           f"with `qgent mcp remove {slug}`.", style=DIM))
    console.print(Text("    Available to the agent from the next turn.", style=FAINT))
    return slug


def remove_mcp(console, name):
    if marketplace.remove_mcp(name):
        console.print(Text.assemble(("  ✓ ", OK), (f"removed MCP server {name}", "")))
        return True
    console.print(Text(f"  No user MCP server named {name!r}. Installed: "
                       + (", ".join(config.load().get("mcp_servers") or {}) or "none"),
                       style=WARN))
    return False


# -- skills -------------------------------------------------------------------------
def browse_skills(console, query=None, refresh=False):
    with console.status(f"[{ACCENT}]Searching skills.sh for GIS skills and reading "
                        "their summaries…"):
        try:
            items = marketplace.search_skills(query, refresh=refresh)
        except marketplace.MarketplaceError as exc:
            console.print(Text(f"  {exc}", style=ERR))
            return []
    if not items:
        console.print(Text("  No GIS skills found" + (f" for {query!r}" if query else ""),
                           style=DIM))
        return []
    have = {s["name"] for s in skills.list_skills()}
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False,
                  header_style=f"bold {ACCENT}", title_justify="left",
                  title=Text(f"GIS skills · skills.sh{' · ' + query if query else ''}",
                             style="bold"))
    table.add_column("#", justify="right", style=ACCENT, no_wrap=True)
    table.add_column("skill", style="bold", overflow="fold", max_width=36)
    table.add_column("installs", justify="right", style=ACCENT_2, no_wrap=True)
    table.add_column("what it does", overflow="fold")
    for index, item in enumerate(items, 1):
        name = Text(item["name"])
        name.append("\n" + item["source"], style=FAINT)
        if item["name"] in have:
            name.append("  installed", style=OK)
        table.add_row(str(index), name, f"{item['installs']:,}", Text(_summary(item["summary"])))
    console.print(table)
    console.print(Text("  Skills are instructions (and sometimes scripts) written by "
                       "third parties — review what you install.", style=FAINT))
    picks = _ask_selection(console, len(items), "skills")
    done = []
    for index in picks:
        item = items[index - 1]
        with console.status(f"[{ACCENT}]Downloading {escape(item['name'])}…"):
            try:
                name, target, count = marketplace.install_skill(item)
            except (marketplace.MarketplaceError, OSError) as exc:
                console.print(Text(f"  ✗ {item['name']}: {exc}", style=ERR))
                continue
        console.print(Text.assemble(("  ✓ ", OK), (name, "bold"),
                                    (f"  installed · {count} file(s) · {short_path(target)}",
                                     DIM)))
        done.append(name)
    if done:
        console.print(Text("    Claude loads them natively (qgent-user:<name>); every "
                           "backend can read them with read_skill.", style=FAINT))
    return done


def remove_skill(console, name):
    if marketplace.remove_skill(name):
        console.print(Text.assemble(("  ✓ ", OK), (f"removed skill {name}", "")))
        return True
    installed = sorted(os.listdir(marketplace.user_skills_dir())) if os.path.isdir(
        marketplace.user_skills_dir()) else []
    console.print(Text(f"  No installed skill named {name!r}. Installed: "
                       + (", ".join(installed) or "none"), style=WARN))
    return False
