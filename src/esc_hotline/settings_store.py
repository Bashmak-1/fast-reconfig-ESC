"""Small persistent app settings (last-used config file path, and each
Хотспот tab's own remembered port/ESC-slot/config), stored as a real .ini
file in the user's home directory - not the Windows registry - so it
survives restarts and is easy to find/inspect.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QSettings

SETTINGS_DIR = Path.home() / ".esc_hotline"
SETTINGS_FILE = SETTINGS_DIR / "settings.ini"

_LAST_CONFIG_PATH_KEY = "last_config_path"


def _open() -> QSettings:
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    return QSettings(str(SETTINGS_FILE), QSettings.Format.IniFormat)


def load_last_config_path() -> str:
    return str(_open().value(_LAST_CONFIG_PATH_KEY, ""))


def save_last_config_path(path: str) -> None:
    settings = _open()
    settings.setValue(_LAST_CONFIG_PATH_KEY, path)
    settings.sync()


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
