"""Shared plain-data types used across the protocol and manager layers."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class EscState(str, Enum):
    IDLE = "idle"
    PROBING = "probing"
    OK = "ok"
    DIFF = "diff"  # has valid (non-blank) config, but it differs from the currently loaded config file
    BLANK = "blank"
    ERROR = "error"


@dataclass
class ProbeResult:
    ok: bool
    mcu_name: str = ""
    settings: Optional[bytes] = None
    error: str = ""
