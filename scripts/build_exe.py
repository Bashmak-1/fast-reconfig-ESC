"""Build a standalone EscHotline.exe with PyInstaller.

Usage: .venv\\Scripts\\python.exe scripts\\build_exe.py
Output: dist\\EscHotline.exe (single file, no console window)
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
        "EscHotline",
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
