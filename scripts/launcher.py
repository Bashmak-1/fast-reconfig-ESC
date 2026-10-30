"""EscHotlineLauncher: login window -> unpack the real exe to temp -> run one session.

Usage: EscHotlineLauncher.exe            (or: python scripts/launcher.py)
       python scripts/launcher.py --hash "Name"   prints one line for users.sha256 (EscHotlineAdmin does it for you)

See src/esc_hotline/session_gate.py for what this is (and is not). The session
ends when the app window is closed or when the date changes; then the temp
copy is deleted, and after midnight the login window comes back.
"""
from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
BUNDLE = Path(getattr(sys, "_MEIPASS", HERE.parent))  # PyInstaller unpack dir, or the repo when run from source
sys.path.insert(0, str(HERE.parent / "src"))

from esc_hotline import session_gate as gate  # noqa: E402
from esc_hotline import updater_core as core  # noqa: E402
from esc_hotline._version import __version__  # noqa: E402

PREFIX = "esc_hotline_session_"
APP_EXE = "EscHotline.exe"
LOG_FILE = Path.home() / ".esc_hotline" / "logs" / "launcher.log"

log = logging.getLogger("launcher")


def setup_log() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def load_users() -> set[str]:
    # a users.sha256 next to the exe (written by EscHotlineAdmin) wins over the one baked in at build time
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else HERE.parent / "private"
    return gate.find_users(exe_dir, BUNDLE, HERE.parent / "private")


def login(users: set[str]) -> Optional[str]:
    def on_attempt(status: str, password: str) -> None:
        log.info("login %s", "ok" if status == "ok" else "denied")

    return gate.login_dialog(users, on_attempt)


def clean_stale() -> None:
    for prefix in (PREFIX, core.UPDATE_PREFIX):
        for old in glob.glob(os.path.join(tempfile.gettempdir(), prefix + "*")):
            shutil.rmtree(old, ignore_errors=True)


def offer_update() -> bool:
    """Look for a newer launcher on GitHub (same channel as this build). If the user agrees, fetch the
    updater into a temp folder and start it; it waits for us to exit, swaps this exe, starts the new one
    and deletes the temp folder. Returns True when the updater took over and we must exit."""
    if not getattr(sys, "frozen", False):
        return False
    channel = core.channel_of(__version__)
    try:
        release = core.fetch_latest(channel=channel)
        if not core.is_newer(release.version, __version__) or core.LAUNCHER_EXE not in release.assets:
            return False
    except Exception as e:  # offline, rate limit, no releases: never block the login
        log.info("update check failed: %s", type(e).__name__)
        return False
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    ask = messagebox.askyesno("EscHotline", f"Доступна нова версія {release.version} (зараз {__version__})."+chr(10)+"Оновити?", parent=root)
    root.destroy()
    if not ask:
        return False
    tmp = Path(tempfile.mkdtemp(prefix=core.UPDATE_PREFIX))
    try:
        core.fetch_verified(release, core.UPDATER_EXE, tmp / core.UPDATER_EXE)
        cmd = [str(tmp / core.UPDATER_EXE), "--launcher", "--yes", "--channel", channel, "--current", __version__,
               "--wait-pid", str(os.getpid()), "--target", sys.executable, "--cleanup", str(tmp)]
        # the updater inherits a valid session token, so it does not ask for the password again
        subprocess.Popen(cmd, cwd=tmp, env={**os.environ, **gate.session_env(os.getpid())}, creationflags=0x00000010)  # new console
    except Exception as e:
        log.info("update start failed: %s", type(e).__name__)
        shutil.rmtree(tmp, ignore_errors=True)
        return False
    log.info("update to %s started", release.version)
    return True


def run_session(day: str) -> str:
    """Runs the app until it exits or the date changes. Returns 'closed' | 'expired' | 'error'."""
    payload = BUNDLE / APP_EXE
    if not payload.is_file():
        log.error("payload %s missing", payload)
        return "error"
    tmp = Path(tempfile.mkdtemp(prefix=PREFIX))
    reason = "closed"
    try:
        exe = tmp / APP_EXE
        shutil.copyfile(payload, exe)
        proc = subprocess.Popen([str(exe)], cwd=tmp, env={**os.environ, **gate.session_env(os.getpid())})
        log.info("session start")
        while proc.poll() is None:
            if date.today().isoformat() != day:
                reason = "expired"
                # taskkill /T: a onefile exe is a bootloader plus a child process
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
                break
            time.sleep(1)
        proc.wait()
    except Exception:
        log.exception("session failed")
        reason = "error"
    finally:
        for _ in range(20):  # the exe stays locked for a moment after the process dies
            shutil.rmtree(tmp, ignore_errors=True)
            if not tmp.exists():
                break
            time.sleep(0.5)
        log.info("session end reason=%s temp_removed=%s", reason, not tmp.exists())
    return reason


def main(argv: list[str]) -> int:
    if argv[:1] == ["--version"]:
        print(f"EscHotlineLauncher {__version__}")
        return 0
    if argv[:1] == ["--hash"] and len(argv) == 2:
        print(gate.name_hash(argv[1]))
        return 0
    setup_log()
    users = load_users()
    clean_stale()
    while True:
        day = login(users)
        if day is not None and offer_update():
            return 0
        if day is None or run_session(day) != "expired":
            return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
