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
import sys
from datetime import date
from typing import Mapping, Optional

ENV_TOKEN = "ESC_HOTLINE_TOKEN"
ENV_PID = "ESC_HOTLINE_LAUNCHER_PID"
_SECRET = "esc-hotline-gate-v1"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def name_hash(name: str) -> str:
    return _sha(name.strip().casefold())


def load_users(text: str) -> set[str]:
    """One sha256 per line; blank lines and '#' comments are ignored."""
    return {ln.strip().lower() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")}


def check_login(name: str, password: str, users: set[str], today: Optional[str] = None) -> str:
    """'ok' | 'unknown_user' | 'bad_password'. The password is the name followed by today's ISO date."""
    if name_hash(name) not in users:
        return "unknown_user"
    expected = name.strip().casefold() + (today or date.today().isoformat())
    return "ok" if hmac.compare_digest(password.strip().casefold(), expected) else "bad_password"


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
