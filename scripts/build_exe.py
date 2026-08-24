"""Build a standalone AM32Flasher.exe with PyInstaller.

Usage: .venv\\Scripts\\python.exe scripts\\build_exe.py
Output: dist\\AM32Flasher.exe (single file, no console window)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--windowed",
        "--name",
        "AM32Flasher",
        "--paths",
        str(ROOT / "src"),
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build"),
        "--specpath",
        str(ROOT / "build"),
        "--noconfirm",
        str(ROOT / "scripts" / "run.py"),
    ]
    return subprocess.call(args, cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
