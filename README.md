# QGent CLI

**A QGIS AI agent in your terminal.** Run `qgent` in a folder of GIS data and
ask for real work, like "buffer these sites by 500 m in UTM 51N, export a
GeoPackage and a map PDF". QGent plans the job, runs it in a headless QGIS
engine (PyQGIS, Processing, print layouts, rendering), checks the outputs, and
reports back with the evidence.

QGent CLI is the terminal sibling of the [QGent QGIS plugin](https://github.com/ljdstechva/qgent).
It uses the same skills, subagents and safety gate, and it doesn't need QGIS
to be open.

```
 qgent  › What GIS data is in this folder? Buffer the wells by 250 m and save to wells_250m.gpkg
  ◇ analysis  ▰▰▱▱▱ 2/5   gpt-6.1-sol · low   heuristic
  ▸ qgis inspect_dataset  ./
    ╰ {"folder": "D:\\project", "files": [{"path": "...wells.shp", ...}]}
  ▸ qgis read_skill  processing-recipes
  ▸ qgis run_processing  native:buffer {"INPUT": "wells", "DISTANCE": 250, ...}
  ▸ qgis stat_path  wells_250m.gpkg
    ╰ {"exists": true, "is_file": true, "size_bytes": 98304, ...}
  Created wells_250m.gpkg — 14 polygons, EPSG:32651 (98 KB).
  ✓ 41s · 5 tools · 38.2k tokens
```

## How it works

```
 you ──► qgent (REPL / -p)
          │  per turn: claude -p … / codex exec …   ← your existing subscription, no API key
          │                    │
          │                    ├─ MCP qgis       ─► qgent hub ─► headless QGIS engine
          │                    │   (stdio bridge)    (approval     (QGIS's own Python,
          │                    │                      gate)         alive all session)
          │                    ├─ MCP qgis_live  ─► your running QGIS (QGIS MCP plugin)
          │                    └─ MCP fetch      ─► web pages
          └─ skills + subagents (Claude: native plugin; Codex: read_skill tool)
```

* **Brains:** your installed **Codex CLI** or **Claude Code** CLI, driven one
  turn at a time and resumed by session id, the same way the QGent plugin
  does it. QGent never asks for an API key.
* **Hands:** a **headless QGIS engine**, which is QGIS's own Python started
  once per session. Projects, layers, layouts and Python variables persist
  between turns. If the engine crashes, QGent restarts it and reopens the
  last saved project.
* **Safety:** every tool call goes through the **hub**. Code that deletes or
  overwrites data (`os.remove`, `deleteFeatures`, `QgsProject.write`, writing
  over an existing file, deleting a layout, …) stops for your approval with
  `y` / `n` / `always`. Calls known to crash QGIS are refused. Every call is
  logged to `~/.qgent/logs/tools.jsonl`.

### Model routing

QGent sizes every turn before it runs. A small, fast model reads the request
(Claude `haiku`, or the fastest model in your Codex model list) and returns:

* a task type and a complexity from 1 to 5;
* a model and reasoning effort for each role.

The roles are:

* **supervisor:** the main agent that plans, talks to you and reports. This is
  the only role on Codex, which works as a single agent.
* **scout:** the read-only `data-scout` subagent (Claude).
* **executor:** the `geoprocessor` and `cartographer` subagents (Claude).
* **verifier:** the independent `qa-verifier` subagent (Claude).

```
› Delineate the watershed from dem.tif, clip land cover to it and build an A3 PDF map
  ◇ analysis  ▰▰▰▰▰ 5/5   opus · high   ai · by haiku
    team scout haiku · executor sonnet · verifier opus   delegate
```

Trivial prompts ("what CRS is this?") are settled by keyword rules with no
model call. A short follow-up such as "yes, do it" keeps the previous route.

Codex models come from Codex's own model list (`~/.codex/models_cache.json`),
so new models are used as soon as Codex knows about them. The routing call
takes about 5 seconds.

To control routing:

* `/route` shows the last decision.
* `/router auto|ai|heuristic|off` changes who decides.
* `/model <name>` and `/effort <level>` pin the main model or effort, and
  `/model auto` hands the choice back to the router.
* `qgent route "<request>"` shows how a request would be routed without
  running it.
* `router_max_effort` in the config caps the effort (default `xhigh`).

### Add GIS MCP servers and skills

```
qgent mcp add              # or /mcp add in the REPL
qgent mcp add elevation    # narrow the search
qgent skills add           # or /skills add
qgent skills add postgis
```

`mcp add` searches the official [MCP Registry](https://registry.modelcontextprotocol.io).
`skills add` searches [skills.sh](https://skills.sh), reading each skill's
summary from its `SKILL.md` on GitHub.

Both catalogs match names loosely: "gis" also finds "registry" and
"logistics". QGent scores each result's description for GIS relevance and
puts QGIS-specific entries first. You get a numbered table with a summary of
each entry, then you pick numbers (`1,3` or `2-4`) to install.

What installing does:

* **MCP servers:** the server is written to `~/.qgent/config.toml`. It runs
  through `uvx` for PyPI packages, `npx` for npm packages, or connects
  remotely by URL. QGent asks for any required API keys, starts the server
  once to check that it lists its tools, and the agent can use it from the
  next turn.
* **Skills:** the skill folder is copied into `~/.qgent/plugin/skills`. Claude
  loads it natively; on any backend the agent can read it with `read_skill`.

To remove one, use `qgent mcp remove <name>` or `qgent skills remove <name>`.
Search results are cached for 24 hours; add `--refresh` to search again. The
first MCP search takes about a minute because the registry is slow.

### Bundled MCP servers

| server | what | when |
|---|---|---|
| `qgis` | QGent's headless engine: 18 tools (`execute_pyqgis`, `run_processing`, `inspect_dataset`, `load_layer`, `render_map_snapshot`, `layout_info`, `manage_layouts`, `ask_user`, `read_skill`, …) | always |
| `qgis_live` | the community [QGIS MCP](https://github.com/nkarasiak/qgis-mcp) server, which drives the QGIS you have open | automatically, on turns where its QGIS plugin listens on `localhost:9876` (`mcp.qgis_live = auto/on/off`) |
| `fetch` | fetch web pages as markdown (data portals, docs) | on by default (`mcp.fetch`) |
| yours | any stdio MCP server you add to the config | always |

`qgis_live` and `fetch` run through [`uvx`](https://docs.astral.sh/uv/).

### Bundled skills and subagents

These are synced from the plugin with `scripts/sync_from_plugin.py`:

* **Skills:** `pyqgis-patterns`, `processing-recipes`,
  `cartography-print-layout`, `layout-templates`, `ph-environmental-maps`
  (DENR/EMB conventions and PH data sources) and `vicinity-map-template`
  (A4 PH vicinity maps).
* **Subagents (Claude backend):** `qgent:data-scout`, `qgent:geoprocessor`,
  `qgent:cartographer`, `qgent:qa-verifier`. On Codex, QGent works as a single
  agent and does its own verification pass.

Skills you install, or write yourself, go in
`~/.qgent/plugin/skills/<name>/SKILL.md`. A skill there takes precedence over a
bundled skill with the same name.

## Install

Requirements: **QGIS 3.x** (OSGeo4W or a standalone installer on Windows; the
QGIS app on macOS; PyQGIS on Linux), **Python 3.11+**, and at least one of the
[Codex CLI](https://github.com/openai/codex) or
[Claude Code](https://docs.claude.com/en/docs/claude-code), already logged in.

```bash
uv tool install git+https://github.com/ljdstechva/qgent-cli
# or: pipx install git+https://github.com/ljdstechva/qgent-cli
qgent doctor
```

`qgent doctor` finds QGIS, starts the engine, completes a real MCP handshake
through the bridge, and checks the CLIs, `uvx` and a live QGIS. If QGIS isn't
found automatically, point QGent at its Python launcher:

```bash
qgent config set qgis_python "C:\OSGeo4W\bin\python-qgis-ltr.bat"
```

## Use

```bash
qgent                          # interactive, in the current folder
qgent "map the flood layer"    # interactive, starting with a message
qgent -p "list the CRS of every shapefile here"        # one-shot, prints the answer
qgent -p --json -y "export layout 'Vicinity' to PDF"   # scripting; -y approves
qgent -c                       # continue the last session in this folder
qgent --resume 20261015-2152   # resume a saved session (reopens its project)
qgent -b claude                # pick the backend for this run
qgent native                   # the full Claude Code / Codex UI with QGent loaded
```

In one-shot mode (`-p`), destructive operations are **refused** unless you
pass `-y`. The agent is told to take a non-destructive route, such as writing
a new file instead.

**In the REPL:** `/help` lists every command, grouped. Tab completes commands
and their options.

* **Conversation:** `/new`, `/resume`, `/sessions`, `/usage`
* **Models and routing:** `/route`, `/router`, `/backend`, `/model`,
  `/effort`, `/approval ask|auto|deny`
* **QGIS engine:** `/project`, `/open <file.qgz>`, `/save [file.qgz]`,
  `/render`, `/engine status|restart`
* **MCP servers and skills:** `/mcp [list|add|remove|test]`,
  `/skills [list|add|remove]`, `/skill <name>`
* **Other:** `/doctor`, `/clear`, `/exit`

Type `!cmd` to run a shell command. `Alt+Enter` inserts a newline, `Ctrl+C`
interrupts a turn (the engine keeps its state) and `Ctrl+D` exits. The bottom
bar shows the backend, the routing mode, the engine state and the approval
policy.

### Use QGent's engine from other agents

```bash
qgent mcp serve                  # stdio MCP server with its own engine
qgent mcp config --client claude # prints: claude mcp add qgent -- "…qgent" mcp serve
qgent mcp config --client codex  # prints a [mcp_servers.qgent] block
qgent mcp test fetch             # handshake with any configured server
```

`mcp serve` has no terminal for approvals, so it refuses destructive
operations unless you start it with `--allow-destructive`.

## Configure

`~/.qgent/config.toml` (`qgent config show | path | set <key> <value>`):

```toml
backend = "codex"            # codex | claude ("" = whichever is installed)
approval = "ask"             # ask | auto | deny
router = "auto"              # auto | ai | heuristic | off
router_model = ""            # "" = the backend's fastest model
router_max_effort = "xhigh"
effort = ""                  # pin an effort; "" = the router decides
qgis_python = ""             # QGIS Python launcher, if auto-detection fails
qgis_profile = ""            # "" = your QGIS default profile (templates, styles)
allow_shell = false          # let Claude run shell commands
claude_setting_sources = "project"   # keep global Claude hooks out of QGent turns

[models]                     # pin a model; "" = the router decides per turn
codex = ""
claude = ""

[mcp]
qgis_live = "auto"
fetch = true

[mcp_servers.postgis]        # add any stdio MCP server
command = "uvx"
args = ["postgres-mcp", "--access-mode=restricted"]
env = { DATABASE_URI = "postgresql://…" }
```

## Develop

```bash
uv tool install --editable . --force
uv run --with pytest --with rich --with prompt_toolkit python -m pytest -q
uvx ruff check src tests scripts
python scripts/sync_from_plugin.py ../qgis_chat_agent   # re-vendor skills, safety, layouts
```

`tests/test_engine_integration.py` runs against a real QGIS installation and
is skipped when none is found.
