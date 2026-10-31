"""Login gate shared by the launcher (scripts/launcher.py) and the app itself.

NOTE FOR DEVELOPERS: this is deliberately a trivial barrier, not real security.
The author wrote this program on their own time and money and was not paid for
it, so it is kept for the author's own people only. Anyone who can read Python
bytecode can strip it; it only keeps casual users out. Do not rely on it.

How it works: the launcher asks for a name and a password (name + today's ISO
date), checks sha256(name) against a list of hashes, unpacks the real exe into
a temp folder and starts it with a token that is only valid for today. The app
refuses to start without that token, so a copy lifted from the temp folder is
useless on its own (and useless tomorrow). Stdlib only: the launcher imports it.
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import os
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path
from typing import Callable, Mapping, Optional

ENV_TOKEN = "ESC_HOTLINE_TOKEN"
ENV_PID = "ESC_HOTLINE_LAUNCHER_PID"
ENV_LAUNCHER_EXE = "ESC_HOTLINE_LAUNCHER_EXE"  # path of the launcher exe that started this session
_SECRET = "esc-hotline-gate-v1"
# deliberately one message for every failure: do not tell what is wrong or how a password is built
DENIED = "Невірний пароль"
INTRO = "EscHotline — прошивка та налаштування FC і ESC.\nДля роботи необхідно авторизуватись."


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _norm(text: str) -> str:
    """Casefolded, NFC, without zero-width/BOM characters and with NBSP as a plain space (pasted names carry those)."""
    text = unicodedata.normalize("NFC", text).replace(" ", " ")
    return "".join(c for c in text if unicodedata.category(c) != "Cf").strip().casefold()


def is_valid_name(name: str) -> bool:
    """Whitelist: Latin letters and digits only (no spaces, no Cyrillic, no symbols)."""
    return re.fullmatch(r"[a-z0-9]+", _norm(name), re.ASCII) is not None


def name_hash(name: str) -> str:
    return _sha(_norm(name))


def load_users(text: str) -> set[str]:
    """One sha256 per line; blank lines and '#' comments are ignored."""
    return {ln.strip().lower() for ln in text.lstrip("﻿").splitlines() if ln.strip() and not ln.lstrip().startswith("#")}


def password_for(name: str, today: Optional[str] = None) -> str:
    """Today's password for a user: the name followed by MMDD."""
    iso = today or date.today().isoformat()
    return _norm(name) + iso[5:7] + iso[8:10]


def check_password(password: str, users: set[str], today: Optional[str] = None) -> str:
    """'ok' | 'bad_chars' | 'unknown_user' | 'bad_password'. The name is the password minus its trailing MMDD,
    so the login needs the password only and the user list stays hashes."""
    pw = _norm(password)
    name = pw[:-4]
    if not is_valid_name(pw):
        return "bad_chars"
    if len(pw) <= 4 or name_hash(name) not in users:
        return "unknown_user"
    return "ok" if hmac.compare_digest(pw, password_for(name, today)) else "bad_password"


def make_token(pid: int, today: Optional[str] = None) -> str:
    return _sha(f"{_SECRET}|{today or date.today().isoformat()}|{pid}")


def session_env(pid: int) -> dict[str, str]:
    return {ENV_TOKEN: make_token(pid), ENV_PID: str(pid)}


def in_session(env: Mapping[str, str] = os.environ) -> bool:
    return ENV_TOKEN in env


def token_ok(env: Mapping[str, str] = os.environ) -> bool:
    pid = env.get(ENV_PID, "")
    return pid.isdigit() and hmac.compare_digest(env.get(ENV_TOKEN, ""), make_token(int(pid)))


def require_session() -> bool:
    """Source runs are free; a frozen exe must have been started by the launcher today."""
    if not getattr(sys, "frozen", False) or token_ok():
        return True
    ctypes.windll.user32.MessageBoxW(
        0, "Запустіть програму через EscHotlineLauncher.exe і авторизуйтесь.", "EscHotline", 0x10
    )
    return False


def find_users(*dirs: Path) -> set[str]:
    """The user list from the first folder that has a users.sha256 (empty set = nobody gets in)."""
    for d in dirs:
        f = Path(d) / "users.sha256"
        if f.is_file():
            return load_users(f.read_text(encoding="utf-8-sig"))
    return set()


def login_dialog(
    users: "set[str] | Callable[[], set[str]]", on_attempt: Optional[Callable[[str, str], None]] = None
) -> Optional[str]:
    """Password window. Returns the day it was accepted on, or None if it was closed.
    on_attempt(status, password) is called after every try (the launcher logs it)."""
    import tkinter as tk  # lazy: the app itself never needs Tk

    root = tk.Tk()
    root.title("EscHotline — вхід")
    root.resizable(False, False)
    result: list[str] = []
    password, error = tk.StringVar(), tk.StringVar()

    def submit(_event=None) -> None:
        today = date.today().isoformat()  # taken at the moment of the click, not at window creation
        current = users() if callable(users) else users  # re-read the list on every try: it may have been replaced meanwhile
        status = check_password(password.get(), current, today)
        if on_attempt:
            on_attempt(status, password.get())
        if status == "ok":
            result.append(today)
            root.destroy()
        else:
            error.set(DENIED)

    tk.Label(root, text=INTRO, justify="left", padx=16, pady=12).grid(row=0, column=0, columnspan=2)
    tk.Label(root, text="Пароль").grid(row=1, column=0, sticky="e", padx=8, pady=4)
    # whitelist while typing/pasting: anything outside A-Z a-z 0-9 is simply not accepted
    only_ascii = root.register(lambda proposed: re.fullmatch(r"[A-Za-z0-9]*", proposed) is not None)
    entry = tk.Entry(root, textvariable=password, show="*", width=28, validate="key", validatecommand=(only_ascii, "%P"))
    entry.grid(row=1, column=1, padx=(0, 16))
    tk.Label(root, textvariable=error, fg="#c00000").grid(row=2, column=0, columnspan=2)
    tk.Button(root, text="Увійти", width=14, command=submit).grid(row=3, column=0, columnspan=2, pady=12)
    root.bind("<Return>", submit)
    entry.focus_set()
    root.mainloop()
    return result[0] if result else None


def require_login(*users_dirs: Path) -> bool:
    """For the standalone updater: inside a launcher session the token is inherited; otherwise ask for the password first."""
    if not getattr(sys, "frozen", False) or token_ok():
        return True
    return login_dialog(lambda: find_users(*users_dirs)) is not None
