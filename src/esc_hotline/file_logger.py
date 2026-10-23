"""Persistent technical log file, independent of (and a superset of) the
GUI's log panel.

Every line shown in the GUI also lands here, plus a lot of low-level
protocol detail that would be too noisy for the GUI panel: raw MSP
responses, full hex dumps of each ESC's settings block before/after a
write, per-command ACK/timeout detail. Kept on disk specifically so
"something weird happened" can be diagnosed after the fact - see
`LOG_FILE` below.
"""
from __future__ import annotations

import logging
import platform
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional

from . import updater_core as core
from ._version import __version__

LOG_DIR = core.channel_dir(core.channel_of(__version__)) / "logs"
LOG_FILE = LOG_DIR / "esc_hotline.log"

_LEVEL_MAP = {
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "success": logging.INFO,
}

_logger: Optional[logging.Logger] = None


def get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("esc_hotline")
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
        logger.info("=" * 70)
        logger.info("Session started - log file: %s", LOG_FILE)
        logger.info(
            "App version: %s (%s channel, %s) | %s | Python %s",
            __version__, core.channel_of(__version__), "exe " + sys.executable if getattr(sys, "frozen", False) else "from source",
            platform.platform(), platform.python_version(),
        )

    _logger = logger
    return logger


def log_at(logger: logging.Logger, level: str, text: str) -> None:
    logger.log(_LEVEL_MAP.get(level, logging.INFO), text)


def hex_dump(data: bytes) -> str:
    return data.hex(" ")
