"""Minimal frozen entry point: prints, as JSON, the runtime it runs on.

CI freezes it with PyInstaller and checks the result is the python.org CPython
3.13 framework build on arm64, the runtime the packaged app ships.
"""

import json
import platform
import sqlite3
import sys


def runtime() -> dict:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "machine": platform.machine(),
        "macos": platform.mac_ver()[0],
        "frozen": bool(getattr(sys, "frozen", False)),
        # "Python" for the python.org framework build, "" for a non-framework build
        "framework": getattr(sys, "_framework", None),
        "sqlite": sqlite3.sqlite_version,
    }


if __name__ == "__main__":
    print(json.dumps(runtime()))
