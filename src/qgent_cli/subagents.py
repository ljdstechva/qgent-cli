"""Claude subagents, rebuilt each turn with the router's model per role.

The definitions live in ``runtime/subagents/*.md`` (synced from the plugin).
They are passed with ``claude --agents <file>`` instead of being loaded from
the plugin folder, because the plugin route would freeze each subagent's
model in its frontmatter.
"""
import json
import os
import re

from . import config

ROLE_OF = {"data-scout": "scout", "geoprocessor": "executor",
           "cartographer": "executor", "qa-verifier": "verifier"}


def _parse(path):
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not match:
        return None
    meta, key = {}, None
    for line in match.group(1).splitlines():
        top = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if top:
            key, value = top.group(1), top.group(2).strip()
            meta[key] = "" if value in (">", "|", ">-", "|-") else value
        elif key and line.startswith((" ", "\t")):
            meta[key] = (meta[key] + " " + line.strip()).strip()
    meta["prompt"] = match.group(2).strip()
    return meta


def definitions(decision=None):
    """{name: {description, prompt, tools, model}} for ``--agents``."""
    folder = os.path.join(config.RUNTIME_DIR, "subagents")
    roles = (decision or {}).get("roles") or {}
    out = {}
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".md"):
            continue
        meta = _parse(os.path.join(folder, name))
        if not meta:
            continue
        agent = meta.get("name") or name[:-3]
        model = (roles.get(ROLE_OF.get(agent, "executor")) or {}).get("model") \
            or meta.get("model") or "inherit"
        entry = {"description": meta.get("description", ""), "prompt": meta["prompt"],
                 "model": model}
        tools = [t.strip() for t in meta.get("tools", "").split(",") if t.strip()]
        if tools:
            entry["tools"] = tools
        out[agent] = entry
    return out


def write_file(path, decision=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(definitions(decision), handle, indent=1)
    return path
