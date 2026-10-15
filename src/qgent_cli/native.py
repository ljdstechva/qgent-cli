"""``qgent native``: the full Claude Code / Codex TUI, loaded with QGent.

Same engine, MCP servers, skills and rules as ``qgent``, but the agent CLI
owns the terminal — its own permission prompts gate every MCP tool call, so
the hub runs without terminal prompts of its own.
"""
import os
import signal
import subprocess

from . import cli_detect, config, mcp_registry, prompts, subagents
from .config import user_plugin_args
from .engine_client import EngineClient
from .hub import Hub


def run_native(cfg, backend, extra_args=()):
    exe = cli_detect.find(backend)
    if not exe:
        print(f"The {backend} CLI is not installed (see `qgent doctor`).")
        return 1
    config.ensure_home()
    engine = EngineClient(cfg)
    engine.warm_up()
    hub = Hub(engine, mode="auto", log_path=config.path("logs", "tools.jsonl")).start()
    hub_env = dict(hub.bridge_env(), QGENT_HIDE_TOOLS="ask_user")
    server_map = mcp_registry.servers(cfg, hub_env)
    env = dict(os.environ)
    if backend == "claude":
        runtime = config.path("runtime")
        mcp_path = mcp_registry.write_claude_config(
            server_map, os.path.join(runtime, f"mcp-native-{os.getpid()}.json"))
        prompt_path = prompts.write_claude_prompt_file(
            os.path.join(runtime, "qgent-claude-prompt.md"))
        agents_path = subagents.write_file(
            os.path.join(runtime, f"agents-native-{os.getpid()}.json"))
        argv = [exe, "--mcp-config", mcp_path, "--strict-mcp-config",
                "--plugin-dir", config.RUNTIME_DIR, *user_plugin_args(),
                "--agents", agents_path,
                "--append-system-prompt-file", prompt_path]
        model = (cfg.get("models") or {}).get("claude")
        if model:
            argv += ["--model", model]
        env.update(hub_env)
    else:
        server_args, server_env = mcp_registry.codex_args(server_map)
        argv = [exe, "-c", "developer_instructions="
                + mcp_registry.toml_literal(prompts.codex_instructions()),
                *server_args]
        model = (cfg.get("models") or {}).get("codex")
        if model:
            argv += ["--model", model]
        env.update(server_env)
    argv += list(extra_args)
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        return subprocess.call(argv, env=env)
    finally:
        signal.signal(signal.SIGINT, previous)
        hub.stop()
        engine.stop()
        if backend == "claude":
            try:
                os.remove(os.path.join(config.path("runtime"),
                                       f"mcp-native-{os.getpid()}.json"))
            except OSError:
                pass
