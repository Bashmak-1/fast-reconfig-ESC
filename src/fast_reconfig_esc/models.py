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
class EscInfo:
    """Identity/firmware info decoded from an ESC's name block + settings,
    for display only - never used for write-safety decisions."""

    mcu_family: str  # e.g. "STM32F051", from the signature table
    mcu_type: Optional[str] = None  # e.g. "12S33mV", parsed from the name block
    firmware_name: Optional[str] = None  # e.g. "ESCV_12S33mV"
    firmware_version: Optional[str] = None  # e.g. "2.16"
    boot_pin: Optional[str] = None  # e.g. "PB4"
    boot_version: Optional[int] = None
    eeprom_version: Optional[int] = None
    reversed: bool = False
    bidirectional: bool = False
    settings: Optional[bytes] = None  # the 184-byte block this info was decoded from, for "Read from ESC" in the editor


@dataclass
class ProbeResult:
    ok: bool
    mcu_name: str = ""
    settings: Optional[bytes] = None
    error: str = ""
    esc_info: Optional["EscInfo"] = None
