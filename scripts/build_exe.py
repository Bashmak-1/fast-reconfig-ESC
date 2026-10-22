"""Build the standalone exes with PyInstaller.

Usage: .venv/Scripts/python.exe scripts/build_exe.py
Output: dist/EscHotline.exe (GUI, no console window) and
        dist/EscHotlineUpdater.exe (console updater, Qt-free)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build(name: str, entry: str, windowed: bool) -> int:
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--windowed" if windowed else "--console",
        "--name",
        name,
        "--paths",
        str(ROOT / "src"),
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build"),
        "--specpath",
        str(ROOT / "build"),
        "--noconfirm",
        "--noupx",
        "--version-file",
        str(ROOT / "scripts" / "version_info.txt"),
        str(ROOT / "scripts" / entry),
    ]
    return subprocess.call(args, cwd=ROOT)


def main() -> int:
    return build("EscHotline", "run.py", windowed=True) or build("EscHotlineUpdater", "run_updater.py", windowed=False)


if __name__ == "__main__":
    sys.exit(main())
