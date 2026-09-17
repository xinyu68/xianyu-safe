#!/usr/bin/env python3
"""Create an isolated runtime for the Xianyu skill."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import venv
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
RUNTIME_DIR = SKILL_DIR / ".runtime"
REQUIREMENTS = SKILL_DIR / "requirements.txt"


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def runtime_python() -> Path:
    if os.name == "nt":
        return RUNTIME_DIR / "Scripts" / "python.exe"
    return RUNTIME_DIR / "bin" / "python"


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upgrade", action="store_true", help="upgrade locked dependencies")
    args = parser.parse_args()

    python_path = runtime_python()
    if not python_path.exists():
        print(f"Creating isolated runtime: {RUNTIME_DIR}")
        venv.EnvBuilder(with_pip=True).create(RUNTIME_DIR)

    command = [
        str(python_path),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
    ]
    if args.upgrade:
        command.append("--upgrade")
    command.extend(["-r", str(REQUIREMENTS)])
    subprocess.run(command, check=True)
    print(python_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
