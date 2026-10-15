"""Print the QGIS Python environment as one JSON line.

Run through a QGIS launcher (OSGeo4W ``python-qgis*.bat``, the macOS bundle
python, or a system python with PyQGIS) so QGent can start the engine later
with that exact interpreter and environment, without the batch wrapper.
"""
import json
import os
import sys


def main():
    payload = {"executable": sys.executable, "python": sys.version.split()[0],
               "env": dict(os.environ)}
    try:
        from qgis.core import Qgis
        payload["qgis_version"] = Qgis.version()
    except Exception as exc:
        payload["error"] = f"{type(exc).__name__}: {exc}"
    sys.stdout.write("QGENT_ENV_PROBE " + json.dumps(payload) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
