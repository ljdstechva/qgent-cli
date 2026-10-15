"""Bundled and user skills (``SKILL.md`` playbooks).

Bundled skills live in ``runtime/skills`` and load natively in Claude Code
(through ``--plugin-dir``, as ``qgent:<name>``). Every backend can also reach
them, plus the user's own ``~/.qgent/plugin/skills/<name>/SKILL.md``, through the
``list_skills`` / ``read_skill`` MCP tools.
"""
import json
import os
import re

from . import config


def skill_dirs():
    # ~/.qgent/plugin is also a Claude Code plugin (see marketplace), so the
    # skills `qgent skills add` installs there load natively on Claude.
    return [("user", config.path("plugin", "skills")),
            ("bundled", os.path.join(config.RUNTIME_DIR, "skills"))]


def _frontmatter(text):
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        return {}
    meta, key = {}, None
    for line in match.group(1).splitlines():
        top = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if top:
            key, value = top.group(1), top.group(2).strip()
            meta[key] = "" if value in (">", "|", ">-", "|-") else value
        elif key and line.startswith((" ", "\t")):
            meta[key] = (meta[key] + " " + line.strip()).strip()
    return {key: _unquote(value) for key, value in meta.items()}


def _unquote(value):
    """YAML scalar quoting: "a \\u2014 b" -> "a — b", 'it''s' -> "it's"."""
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def list_skills():
    """[{name, description, path, source}], user skills shadowing bundled."""
    found = {}
    for source, folder in skill_dirs():
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name, "SKILL.md")
            if name in found or not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as handle:
                meta = _frontmatter(handle.read(4000))
            found[name] = {"name": meta.get("name") or name,
                           "description": meta.get("description", ""),
                           "path": path, "source": source}
    return sorted(found.values(), key=lambda item: item["name"])


def read_skill(name):
    wanted = str(name or "").strip().split(":")[-1].casefold()
    for skill in list_skills():
        if skill["name"].casefold() == wanted or os.path.basename(
                os.path.dirname(skill["path"])).casefold() == wanted:
            with open(skill["path"], encoding="utf-8") as handle:
                return skill, handle.read()
    return None, ""


def index_text():
    lines = []
    for skill in list_skills():
        description = skill["description"]
        if len(description) > 220:
            description = description[:217].rstrip() + "..."
        lines.append(f"- {skill['name']}: {description}")
    return "\n".join(lines)
