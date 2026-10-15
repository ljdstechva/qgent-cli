from .claude import ClaudeBackend
from .codex import CodexBackend

BACKENDS = {"claude": ClaudeBackend, "codex": CodexBackend}


def make(name, exe, cfg):
    return BACKENDS[name](exe, cfg)
