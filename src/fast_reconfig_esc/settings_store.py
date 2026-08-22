"""Small persistent app settings (currently just the last-used config file
path), stored as a real .ini file in the user's home directory - not the
Windows registry - so it survives restarts and is easy to find/inspect.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QSettings

SETTINGS_DIR = Path.home() / ".fast_reconfig_esc"
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
