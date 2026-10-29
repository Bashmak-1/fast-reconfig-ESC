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
import tkinter as tk
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
INTRO = "EscHotline — прошивка та налаштування FC і ESC.\nДля роботи необхідно авторизуватись."

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
    for path in (exe_dir / "users.sha256", BUNDLE / "users.sha256", HERE.parent / "private" / "users.sha256"):
        if path.is_file():
            return gate.load_users(path.read_text(encoding="utf-8"))
    return set()


def login(users: set[str]) -> Optional[str]:
    """Show the login window; returns the day it was accepted on, or None if the window was closed."""
    root = tk.Tk()
    root.title("EscHotline — вхід")
    root.resizable(False, False)
    result: list[str] = []
    password, error = tk.StringVar(), tk.StringVar()

    def submit(_event=None) -> None:
        today = date.today().isoformat()  # taken at the moment of the click, not at window creation
        status = gate.check_password(password.get(), users, today)
        log.info("login %s user=%s", status, gate.name_hash(password.get().strip().casefold()[:-4])[:8])
        if status == "ok":
            result.append(today)
            root.destroy()
        else:
            error.set("Невірний пароль")

    tk.Label(root, text=INTRO, justify="left", padx=16, pady=12).grid(row=0, column=0, columnspan=2)
    tk.Label(root, text="Пароль").grid(row=1, column=0, sticky="e", padx=8, pady=4)
    entry = tk.Entry(root, textvariable=password, show="*", width=28)
    entry.grid(row=1, column=1, padx=(0, 16))
    tk.Label(root, textvariable=error, fg="#c00000").grid(row=2, column=0, columnspan=2)
    tk.Button(root, text="Увійти", width=14, command=submit).grid(row=3, column=0, columnspan=2, pady=12)
    root.bind("<Return>", submit)
    entry.focus_set()
    root.mainloop()
    return result[0] if result else None


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
