"""User configuration (``~/.qgent/config.toml``) and QGent's home folders.

``QGENT_HOME`` relocates everything (tests use it). Unknown keys are kept, so
a newer config survives an older qgent.
"""
import copy
import os
import re
import tomllib

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.join(PACKAGE_DIR, "runtime")
ENGINE_DIR = os.path.join(PACKAGE_DIR, "engine")

DEFAULTS = {
    # "codex" | "claude"; "" picks whichever CLI is installed (codex first).
    "backend": "",
    # Pinned models; "" lets the router choose per turn.
    "models": {"claude": "", "codex": ""},
    # Pinned effort ("low" ... "max"); "" lets the router choose.
    "effort": "",
    # auto: a fast model routes every non-trivial turn; ai: always;
    # heuristic: keyword rules only; off: CLI defaults.
    "router": "auto",
    "router_model": "",          # "" = the backend's fastest model
    "router_max_effort": "xhigh",
    "router_timeout": 45,
    # ask: prompt before destructive QGIS operations; auto: never prompt;
    # deny: refuse them (the default for non-interactive -p runs).
    "approval": "ask",
    # Path to a QGIS Python launcher (python-qgis*.bat / python3 with PyQGIS).
    "qgis_python": "",
    # "" = QGIS's default user profile; a folder path = that profile.
    "qgis_profile": "",
    # Claude Code setting sources for agent turns ("project" keeps the
    # user's global hooks and settings out of QGent sessions).
    "claude_setting_sources": "project",
    # Let the agent run shell commands (Claude: Bash; Codex: its sandbox).
    "allow_shell": False,
    "mcp": {
        "qgis_live": "auto",   # auto | on | off
        "fetch": True,
    },
    # Extra MCP servers, same shape as Claude's mcpServers entries:
    # [mcp_servers.name] command = "...", args = [...], env = {...}
    "mcp_servers": {},
}


def home():
    return os.path.abspath(os.path.expanduser(
        os.environ.get("QGENT_HOME") or os.path.join("~", ".qgent")))


def path(*parts):
    return os.path.join(home(), *parts)


def config_path():
    return path("config.toml")


def user_plugin_args():
    """``--plugin-dir`` for skills installed with `qgent skills add`, if any."""
    plugin = path("plugin")
    skills = os.path.join(plugin, "skills")
    manifest = os.path.join(plugin, ".claude-plugin", "plugin.json")
    if os.path.isfile(manifest) and os.path.isdir(skills) and os.listdir(skills):
        return ["--plugin-dir", plugin]
    return []


def ensure_home():
    for sub in ("", "sessions", "renders", "runtime", "logs"):
        os.makedirs(path(sub), exist_ok=True)
    return home()


def _merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load():
    try:
        with open(config_path(), "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        data = {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path()}: {exc}") from exc
    return _merge(DEFAULTS, data)


def save(cfg):
    ensure_home()
    text = dumps(cfg)
    tmp = config_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, config_path())


def set_value(cfg, dotted, raw):
    """Set ``a.b.c`` from a command-line string (TOML-ish typed)."""
    value = parse_scalar(raw)
    node = cfg
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            raise ConfigError(f"{dotted}: {part} is not a table")
    node[parts[-1]] = value
    return cfg


def parse_scalar(raw):
    text = str(raw).strip()
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if text[:1] in "[{\"":
        try:
            return tomllib.loads("v = " + text)["v"]
        except tomllib.TOMLDecodeError:
            pass
    return text


class ConfigError(Exception):
    pass


# -- minimal TOML writer (the stdlib only reads TOML) -----------------------
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(key):
    return key if _BARE_KEY.match(key) else _string(key)


def _string(value):
    out = ['"']
    for char in value:
        if char in '"\\':
            out.append("\\" + char)
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(v)}"
                                for k, v in value.items()) + " }"
    raise ConfigError(f"cannot store {type(value).__name__} in config.toml")


def dumps(cfg, prefix=""):
    """Serialize: scalars first, then every dict as a ``[table]``."""
    scalars = [(k, v) for k, v in cfg.items() if not isinstance(v, dict)]
    tables = [(k, v) for k, v in cfg.items() if isinstance(v, dict)]
    lines = [f"{_key(k)} = {_value(v)}" for k, v in scalars]
    for key, value in tables:
        name = f"{prefix}.{_key(key)}" if prefix else _key(key)
        if lines:
            lines.append("")
        lines.append(f"[{name}]")
        body = dumps(value, name).strip("\n")
        if body:
            lines.append(body)
    return "\n".join(lines) + "\n"
