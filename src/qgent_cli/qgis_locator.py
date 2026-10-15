"""Find a Python that can ``import qgis`` and capture its environment.

QGIS ships its own Python. On Windows (OSGeo4W / standalone installers) the
``bin/python-qgis*.bat`` launchers set PATH, GDAL/PROJ data and PYTHONPATH
before running ``python.exe``. QGent runs the launcher once with
``engine/env_probe.py`` to capture that environment, caches it, and from then
on starts the engine with the real interpreter directly — no ``cmd.exe``
wrapper to orphan or to mangle quoting.
"""
import glob
import json
import os
import shutil
import subprocess
import sys

from . import config

PROBE = os.path.join(config.ENGINE_DIR, "env_probe.py")
_MARK = "QGENT_ENV_PROBE "


class QgisNotFound(Exception):
    pass


def _windows_candidates():
    roots = []
    for base in (os.environ.get("LOCALAPPDATA", ""),
                 os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                 os.environ.get("PROGRAMW6432", "")):
        if not base:
            continue
        roots += sorted(glob.glob(os.path.join(base, "Programs", "OSGeo4W")))
        roots += sorted(glob.glob(os.path.join(base, "QGIS *")), reverse=True)
    for drive in ("C:", "D:"):
        roots += [drive + "\\OSGeo4W", drive + "\\OSGeo4W64"]
    out = []
    for root in roots:
        # A launcher only counts when its QGIS python package is installed;
        # half-uninstalled folders keep bin/ with nothing behind it.
        for name, app in (("python-qgis-ltr.bat", "qgis-ltr"),
                          ("python-qgis.bat", "qgis"),
                          ("python-qgis-dev.bat", "qgis-dev"),
                          ("python-qgis-qt6.bat", "qgis-qt6")):
            bat = os.path.join(root, "bin", name)
            if os.path.isfile(bat) and os.path.isdir(
                    os.path.join(root, "apps", app, "python", "qgis")):
                out.append(bat)
    return out


def _posix_candidates():
    out = []
    for app in sorted(glob.glob("/Applications/QGIS*.app"), reverse=True):
        for rel in ("Contents/MacOS/bin/python3", "Contents/MacOS/python3",
                    "Contents/Frameworks/Python.framework/Versions/Current/bin/python3"):
            exe = os.path.join(app, rel)
            if os.path.isfile(exe):
                out.append(exe)
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            out.append(found)
    out += ["/usr/bin/python3", "/usr/local/bin/python3"]
    seen = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def candidates(cfg=None):
    cfg = cfg or config.load()
    explicit = os.environ.get("QGENT_QGIS_PYTHON") or cfg.get("qgis_python")
    if explicit:
        return [os.path.abspath(os.path.expanduser(explicit))]
    return _windows_candidates() if os.name == "nt" else _posix_candidates()


def _launch_command(launcher, script):
    """argv (or a raw command line on Windows .bat) running ``script``."""
    if os.name == "nt" and launcher.lower().endswith((".bat", ".cmd")):
        # /s strips only the outermost quotes, so quoted paths with spaces
        # inside survive cmd.exe's parsing.
        return f'cmd.exe /d /s /c ""{launcher}" "{script}""'
    return [launcher, script]


# Host-interpreter variables that must not reach QGIS's Python (qgent itself
# usually runs from a uv/pipx venv).
_HOST_PYTHON_VARS = ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV",
                     "__PYVENV_LAUNCHER__", "PYTHONEXECUTABLE", "CONDA_PREFIX",
                     "UV_INTERNAL__PARENT_INTERPRETER")


def clean_env(base=None):
    env = dict(os.environ if base is None else base)
    for key in list(env):
        if key.upper() in _HOST_PYTHON_VARS:
            env.pop(key)
    return env


def _probe(launcher, timeout=90):
    base = clean_env()
    try:
        proc = subprocess.run(
            _launch_command(launcher, PROBE), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            stdin=subprocess.DEVNULL, env=base)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{launcher}: {exc}"
    for line in proc.stdout.splitlines():
        if line.startswith(_MARK):
            payload = json.loads(line[len(_MARK):])
            if payload.get("error"):
                return None, f"{launcher}: {payload['error']}"
            # Keep only what the launcher changed: the cache file then holds
            # QGIS paths, never the user's other (possibly secret) variables,
            # and the engine still sees the current environment otherwise.
            probed = payload.pop("env", {})
            payload["env_delta"] = {
                key: value for key, value in probed.items()
                if base.get(key) != value}
            payload["launcher"] = launcher
            return payload, ""
    tail = (proc.stderr or proc.stdout or "").strip()[-400:]
    return None, f"{launcher}: no probe output (exit {proc.returncode}) {tail}"


def _cache_path():
    return config.path("qgis_env.json")


def _signature(launcher):
    try:
        return f"{launcher}|{os.path.getmtime(launcher):.0f}"
    except OSError:
        return launcher


def resolve(cfg=None, refresh=False):
    """Return {"executable", "env", "qgis_version", "launcher", ...}.

    Raises QgisNotFound with every attempted launcher's reason.
    """
    cfg = cfg or config.load()
    tried = candidates(cfg)
    if not refresh:
        try:
            with open(_cache_path(), encoding="utf-8") as handle:
                cached = json.load(handle)
            if (cached.get("signature") == _signature(cached.get("launcher", ""))
                    and cached.get("launcher") in tried
                    and os.path.isfile(cached.get("executable", ""))):
                return cached
        except (OSError, ValueError):
            pass
    errors = []
    for launcher in tried:
        payload, error = _probe(launcher)
        if payload:
            payload["signature"] = _signature(launcher)
            config.ensure_home()
            with open(_cache_path(), "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            return payload
        errors.append(error)
    hint = ("Install QGIS (https://qgis.org) or point QGent at its Python "
            "launcher: qgent config set qgis_python <path to python-qgis*.bat "
            "or a python3 that can import qgis>.")
    detail = "\n  ".join(errors) if errors else "no QGIS installation found"
    raise QgisNotFound(f"{detail}\n{hint}")


def engine_command(info):
    """argv + env to run the engine worker under the probed interpreter."""
    env = clean_env()
    env.update(info.get("env_delta") or {})
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return [info["executable"], "-u",
            os.path.join(config.ENGINE_DIR, "worker.py")], env


if __name__ == "__main__":  # pragma: no cover - manual check
    print(json.dumps({k: v for k, v in resolve(refresh=True).items()
                      if k != "env"}, indent=1))
    sys.exit(0)
