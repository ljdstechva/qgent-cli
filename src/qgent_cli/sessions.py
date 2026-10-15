"""Saved QGent sessions (``~/.qgent/sessions``).

A session maps QGent's id to the backend's conversation id, the working
folder and the last project file, so ``qgent --resume`` continues the
conversation *and* reopens the project in a fresh engine. The transcript
(``<id>.jsonl``) keeps prompts and final answers for the user.
"""
import json
import os
import time
import uuid

from . import config


def _dir():
    return config.path("sessions")


class Session:
    def __init__(self, backend, cwd, sid=None, data=None):
        data = data or {}
        self.id = sid or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.backend = data.get("backend", backend)
        self.backend_session = data.get("backend_session")
        self.cwd = data.get("cwd", cwd)
        self.title = data.get("title", "")
        self.created = data.get("created", time.time())
        self.updated = data.get("updated", self.created)
        self.turns = data.get("turns", 0)
        self.project = data.get("project")
        self.usage = data.get("usage", {"input_tokens": 0, "output_tokens": 0,
                                        "cost_usd": 0.0})

    def to_dict(self):
        return {"id": self.id, "backend": self.backend,
                "backend_session": self.backend_session, "cwd": self.cwd,
                "title": self.title, "created": self.created,
                "updated": self.updated, "turns": self.turns,
                "project": self.project, "usage": self.usage}

    def save(self):
        config.ensure_home()
        self.updated = time.time()
        path = os.path.join(_dir(), self.id + ".json")
        with open(path + ".tmp", "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=1)
        os.replace(path + ".tmp", path)

    def record(self, role, text):
        config.ensure_home()
        with open(os.path.join(_dir(), self.id + ".jsonl"), "a",
                  encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": time.time(), "role": role,
                                     "text": text}) + "\n")

    def add_usage(self, usage):
        for key in ("input_tokens", "output_tokens"):
            self.usage[key] = self.usage.get(key, 0) + int(usage.get(key) or 0)
        if usage.get("cost_usd"):
            self.usage["cost_usd"] = round(
                (self.usage.get("cost_usd") or 0) + float(usage["cost_usd"]), 4)


def list_sessions(cwd=None, limit=50):
    folder = _dir()
    if not os.path.isdir(folder):
        return []
    out = []
    for name in os.listdir(folder):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if cwd and os.path.normcase(data.get("cwd", "")) != os.path.normcase(cwd):
            continue
        out.append(Session(data.get("backend", ""), data.get("cwd", ""),
                           sid=data.get("id"), data=data))
    out.sort(key=lambda s: s.updated, reverse=True)
    return out[:limit]


def load(sid):
    for session in list_sessions(limit=10_000):
        if session.id == sid or session.id.startswith(sid) or (
                session.backend_session and session.backend_session == sid):
            return session
    return None


def latest(cwd):
    found = list_sessions(cwd=cwd, limit=1)
    return found[0] if found else None
