"""Build the standalone exes with PyInstaller.

Usage: .venv/Scripts/python.exe scripts/build_exe.py
Output: dist/EscHotline.exe (GUI, no console window) and
        dist/EscHotlineUpdater.exe (console updater, Qt-free)
Add --launcher (after the normal build, with private/users.sha256 in place)
for the private login launcher dist/EscHotlineLauncher.exe.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build(name: str, entry: str, windowed: bool, extra: tuple[str, ...] = ()) -> int:
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
        *extra,
        str(ROOT / "scripts" / entry),
    ]
    return subprocess.call(args, cwd=ROOT)


def build_launcher() -> int:
    """Private login launcher: embeds dist/EscHotline.exe and private/users.sha256 (not built in CI)."""
    users = ROOT / "private" / "users.sha256"
    app = ROOT / "dist" / "EscHotline.exe"
    if not users.is_file() or not app.is_file():
        print(f"Need {users} and {app} first")
        return 1
    data = ("--add-data", f"{app};.", "--add-data", f"{users};.")
    return build("EscHotlineLauncher", "launcher.py", windowed=True, extra=data)


def main() -> int:
    if "--launcher" in sys.argv:
        return build_launcher()
    return build("EscHotline", "run.py", windowed=True) or build("EscHotlineUpdater", "run_updater.py", windowed=False)


if __name__ == "__main__":
    sys.exit(main())
