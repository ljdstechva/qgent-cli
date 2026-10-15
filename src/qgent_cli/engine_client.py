"""Owns the headless QGIS engine process (``engine/worker.py``).

The engine is started once per qgent session (warmed up in the background
while the user types) and reused by every agent turn, so projects and layers
persist. Calls are serialized; a crashed engine is restarted on the next call
and the caller is told that in-memory state was lost.
"""
import json
import os
import subprocess
import threading
import time

from . import config, qgis_locator

CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class EngineError(Exception):
    pass


class EngineClient:
    def __init__(self, cfg=None, log_path=None):
        self.cfg = cfg or config.load()
        self.log_path = log_path or config.path("logs", "engine.log")
        self.info = {}
        self.state = "stopped"      # stopped | starting | ready | failed
        self.error = ""
        self.restarts = 0
        self.last_project = None    # re-opened after a crash
        self._proc = None
        self._stderr = None
        self._call_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._ready = threading.Event()
        self._next_id = 0
        self._started_before = False
        self._needs_recover = False
        self.default_timeout = 1800

    # -- lifecycle ------------------------------------------------------------
    def warm_up(self):
        """Start in a background thread; ``call`` waits for it."""
        if self.state in ("ready", "starting"):
            return
        threading.Thread(target=self._safe_start, name="qgent-engine-start",
                         daemon=True).start()

    def _safe_start(self):
        try:
            self.start()
        except EngineError:
            pass

    def alive(self):
        return self._proc is not None and self._proc.poll() is None

    def start(self, timeout=180):
        with self._start_lock:
            if self.alive() and self.state == "ready":
                return self.info
            self.state = "starting"
            self._ready.clear()
            try:
                info = qgis_locator.resolve(self.cfg)
            except qgis_locator.QgisNotFound as exc:
                return self._fail(str(exc))
            argv, env = qgis_locator.engine_command(info)
            env["QGENT_ASSETS"] = os.path.join(config.RUNTIME_DIR, "assets")
            env["QGENT_RENDER_DIR"] = config.path("renders")
            if self.cfg.get("qgis_profile"):
                env["QGENT_QGIS_PROFILE_DIR"] = os.path.abspath(
                    os.path.expanduser(self.cfg["qgis_profile"]))
            config.ensure_home()
            # Held open for the engine's lifetime; closed in stop().
            self._stderr = open(self.log_path, "a", encoding="utf-8",  # noqa: SIM115
                                errors="replace")
            self._stderr.write(f"\n--- engine start {time.ctime()}\n")
            self._stderr.flush()
            try:
                self._proc = subprocess.Popen(
                    argv, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=self._stderr, text=True, encoding="utf-8",
                    errors="replace", bufsize=1, cwd=os.getcwd(),
                    # Own process group: Ctrl+C in the terminal must interrupt
                    # the agent turn, not kill QGIS mid-write.
                    creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW)
            except OSError as exc:
                return self._fail(f"could not start QGIS engine: {exc}")
            result = {}

            def read_ready():
                result["line"] = self._proc.stdout.readline()

            reader = threading.Thread(target=read_ready, daemon=True)
            reader.start()
            reader.join(timeout)
            line = result.get("line", "")
            if not line:
                self.stop()
                return self._fail(
                    "QGIS engine did not start"
                    + (" (timed out)" if reader.is_alive() else "")
                    + f"; see {self.log_path}")
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                self.stop()
                return self._fail(f"QGIS engine said: {line.strip()[:300]}")
            if event.get("event") != "ready":
                self.stop()
                return self._fail("QGIS engine failed to initialise:\n"
                                  + str(event.get("error", event))[-1500:])
            self.info = event
            self.state = "ready"
            self.error = ""
            # Any start after the first one replaces a lost engine.
            self._needs_recover = self._started_before
            self._started_before = True
            self._ready.set()
            return self.info

    def _fail(self, message):
        self.state = "failed"
        self.error = message
        self._ready.set()
        raise EngineError(message)

    def stop(self):
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.stdin.close()
                proc.wait(3)
            except (OSError, subprocess.TimeoutExpired, ValueError):
                proc.kill()
        if self._stderr:
            self._stderr.close()
            self._stderr = None
        if self.state == "ready":
            self.state = "stopped"

    def restart(self):
        self.stop()
        self.state = "stopped"
        return self.start()

    # -- calls ----------------------------------------------------------------
    def call(self, tool, args=None, timeout=None):
        """Run one tool; returns (ok, text). Never raises for tool errors."""
        if self.state == "starting":
            self._ready.wait(200)
        notes = []
        if not self.alive() or self.state != "ready":
            try:
                self.start()
            except EngineError as exc:
                return False, f"QGIS engine unavailable: {exc}"
        if self._needs_recover:
            self._needs_recover = False
            notes.append(self._recover())
        acquired = self._call_lock.acquire(timeout=30)
        if not acquired:
            return False, ("The QGIS engine is still busy with an earlier "
                           "request (an interrupted turn). Wait, or run "
                           "/engine restart to discard it.")
        try:
            ok, text = self._roundtrip(tool, args or {},
                                       timeout or self.default_timeout)
        finally:
            self._call_lock.release()
        if ok and tool == "open_project":
            try:
                self.last_project = json.loads(text).get("opened")
            except ValueError:
                pass
        elif ok and tool == "save_project":
            try:
                self.last_project = json.loads(text).get("saved")
            except ValueError:
                pass
        elif ok and tool == "new_project":
            self.last_project = None
        if notes:
            text = "\n".join(n for n in notes if n) + "\n\n" + text
        return ok, text

    def _roundtrip(self, tool, args, timeout):
        self._next_id += 1
        request_id = self._next_id
        try:
            self._proc.stdin.write(json.dumps(
                {"id": request_id, "tool": tool, "args": args}) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError):
            return self._crashed()
        box = {}

        def read():
            box["line"] = self._proc.stdout.readline()

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(timeout)
        if reader.is_alive():
            # A wedged QGIS call cannot be interrupted from outside.
            self.stop()
            self.state = "stopped"
            self.restarts += 1
            return False, (f"QGIS engine call {tool!r} exceeded {timeout}s and "
                           "the engine was restarted; in-memory layers were "
                           "lost. Split the work into smaller steps.")
        line = box.get("line", "")
        if not line:
            return self._crashed()
        try:
            response = json.loads(line)
        except json.JSONDecodeError:
            return False, f"bad engine response: {line[:300]}"
        return bool(response.get("ok")), str(response.get("result", ""))

    def _crashed(self):
        code = None
        if self._proc is not None:
            try:
                # stdout closes a moment before the process is reaped.
                code = self._proc.wait(5)
            except subprocess.TimeoutExpired:
                code = None
        self.stop()
        self.state = "stopped"
        self.restarts += 1
        return False, (f"The QGIS engine crashed (exit code {code}). It will "
                       "restart on the next call; layers held only in memory "
                       f"were lost. Engine log: {self.log_path}")

    def _recover(self):
        """After a restart, reopen the last project file so work continues."""
        if not self.last_project or not os.path.isfile(self.last_project):
            return "Note: the QGIS engine restarted; the project is empty."
        with self._call_lock:
            ok, _ = self._roundtrip("open_project",
                                    {"path": self.last_project}, 120)
        if ok:
            return (f"Note: the QGIS engine restarted and reopened "
                    f"{self.last_project} (unsaved changes were lost).")
        return "Note: the QGIS engine restarted; reopening the project failed."
