"""Small persistent app settings (last-used config file path, and each
Хотспот tab's own remembered port/ESC-slot/config), stored as a real .ini
file in the user's home directory - not the Windows registry - so it
survives restarts and is easy to find/inspect.
"""
from __future__ import annotations

import shutil

from PyQt6.QtCore import QSettings

from . import updater_core as core
from ._version import __version__

# Stable and dev builds each keep their own settings (a dev experiment must not corrupt the
# stable one); only the few values in shared.ini are common to both.
CHANNEL = core.channel_of(__version__)
SETTINGS_DIR = core.channel_dir(CHANNEL)
SETTINGS_FILE = SETTINGS_DIR / "settings.ini"
SHARED_FILE = core.ROOT_DIR / "shared.ini"
_LEGACY_FILE = core.ROOT_DIR / "settings.ini"  # pre-channel layout: everything lived directly in ~/.esc_hotline

_LAST_CONFIG_PATH_KEY = "last_config_path"
_LANGUAGE_KEY = "language"


def _open() -> QSettings:
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    if not SETTINGS_FILE.exists():
        # One-time seed of a fresh channel folder: the old flat layout's settings (stable), and for
        # dev also the stable ones, so a new dev install doesn't start with empty ports/CLI paths.
        # Copy only, never write back - a dev build can't touch stable's file.
        sources = [core.channel_dir("stable") / "settings.ini"] if CHANNEL == "dev" else []
        for src in sources + [_LEGACY_FILE]:
            if src.exists():
                shutil.copy2(src, SETTINGS_FILE)
                break
    return QSettings(str(SETTINGS_FILE), QSettings.Format.IniFormat)


def _open_shared() -> QSettings:
    core.ROOT_DIR.mkdir(parents=True, exist_ok=True)
    return QSettings(str(SHARED_FILE), QSettings.Format.IniFormat)


def load_shared_str(key: str, default: str = "") -> str:
    return str(_open_shared().value(key, default))


def save_shared_str(key: str, value: str) -> None:
    settings = _open_shared()
    settings.setValue(key, value)
    settings.sync()


def load_language() -> str:
    return load_shared_str(_LANGUAGE_KEY)


def save_language(code: str) -> None:
    save_shared_str(_LANGUAGE_KEY, code)


def load_last_config_path() -> str:
    return load_shared_str(_LAST_CONFIG_PATH_KEY) or str(_open().value(_LAST_CONFIG_PATH_KEY, ""))


def save_last_config_path(path: str) -> None:
    save_shared_str(_LAST_CONFIG_PATH_KEY, path)


def load_str(key: str, default: str = "") -> str:
    """Generic string setting reader - for per-tab values (remembered port,
    a second independent config path, ...) that don't each need their own
    named load/save function."""
    return str(_open().value(key, default))


def save_str(key: str, value: str) -> None:
    settings = _open()
    settings.setValue(key, value)
    settings.sync()


def load_int(key: str, default: int = 0) -> int:
    try:
        return int(_open().value(key, default))
    except (TypeError, ValueError):
        return default


def save_int(key: str, value: int) -> None:
    settings = _open()
    settings.setValue(key, value)
    settings.sync()


def load_float(key: str, default: float = 0.0) -> float:
    """Same as load_int(), but for values needing sub-integer precision
    (e.g. ramp durations in tenths of a second) - reads an older plain
    int ("4") written by a previous version just as well as a float
    ("4.2"), since both parse fine via float()."""
    try:
        return float(_open().value(key, default))
    except (TypeError, ValueError):
        return default


def save_float(key: str, value: float) -> None:
    settings = _open()
    settings.setValue(key, value)
    settings.sync()
