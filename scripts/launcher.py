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
        log.info("login %s user=%s", status, gate.name_hash(password.strip()[:-4])[:8])

    return gate.login_dialog(users, on_attempt)


def clean_stale() -> None:
    for old in glob.glob(os.path.join(tempfile.gettempdir(), PREFIX + "*")):
        shutil.rmtree(old, ignore_errors=True)


def run_session(day: str) -> str:
    """Runs the app until it exits or the date changes. Returns 'closed' | 'date_changed' | 'error'."""
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
        log.info("session start day=%s", day)
        while proc.poll() is None:
            if date.today().isoformat() != day:
                reason = "date_changed"
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
    if argv[:1] == ["--hash"] and len(argv) == 2:
        print(gate.name_hash(argv[1]))
        return 0
    setup_log()
    users = load_users()
    clean_stale()
    while True:
        day = login(users)
        if day is None or run_session(day) != "date_changed":
            return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
