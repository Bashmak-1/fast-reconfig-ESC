"""Loading/validating the ESC EEPROM/config .bin file.

The 184-byte settings block has no embedded checksum of its own (confirmed
against am32-configurator's settings (de)serializers), so it is treated as an
opaque blob here: load it, check the length, write it back byte-for-byte.
"""
from __future__ import annotations

from pathlib import Path

from .mcu_profiles import LAYOUT_SIZE


class ConfigFileError(Exception):
    pass


def load_config_file(path: str | Path) -> bytes:
    p = Path(path)
    if not p.is_file():
        raise ConfigFileError(f"config file not found: {p}")
    data = p.read_bytes()
    if len(data) != LAYOUT_SIZE:
        raise ConfigFileError(f"config file must be exactly {LAYOUT_SIZE} bytes, got {len(data)} ({p})")
    return data
