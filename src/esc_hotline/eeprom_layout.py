"""ESC settings-block (184-byte EEPROM) field layout and small decoders.

Offsets verified against am32-configurator/src/eeprom.ts (`EepromLayout`) and
src/mcu.ts (`parseBootLoaderPin`) - not guessed. Full field table included
for reference even though the UI currently only surfaces a handful of these
(firmware/bootloader identity, reversed/3D-mode flags); the rest is there
for when a full settings editor is ever built.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class EepromField:
    offset: int
    size: int
    min_eeprom_version: Optional[int] = None


EEPROM_LAYOUT: dict[str, EepromField] = {
    "BOOT_BYTE": EepromField(0x00, 1),
    "LAYOUT_REVISION": EepromField(0x01, 1),
    "BOOT_LOADER_REVISION": EepromField(0x02, 1),
    "MAIN_REVISION": EepromField(0x03, 1),
    "SUB_REVISION": EepromField(0x04, 1),
    "MAX_RAMP": EepromField(0x05, 1, 3),
    "MINIMUM_DUTY_CYCLE": EepromField(0x06, 1, 3),
    "DISABLE_STICK_CALIBRATION": EepromField(0x07, 1, 3),
    "ABSOLUTE_VOLTAGE_CUTOFF": EepromField(0x08, 1, 3),
    "CURRENT_P": EepromField(0x09, 1, 3),
    "CURRENT_I": EepromField(0x0A, 1, 3),
    "CURRENT_D": EepromField(0x0B, 1, 3),
    "ACTIVE_BRAKE_POWER": EepromField(0x0C, 1, 3),
    "MOTOR_DIRECTION": EepromField(0x11, 1),
    "BIDIRECTIONAL_MODE": EepromField(0x12, 1),
    "SINUSOIDAL_STARTUP": EepromField(0x13, 1),
    "COMPLEMENTARY_PWM": EepromField(0x14, 1),
    "VARIABLE_PWM_FREQUENCY": EepromField(0x15, 1),
    "STUCK_ROTOR_PROTECTION": EepromField(0x16, 1),
    "TIMING_ADVANCE": EepromField(0x17, 1),
    "PWM_FREQUENCY": EepromField(0x18, 1),
    "STARTUP_POWER": EepromField(0x19, 1),
    "MOTOR_KV": EepromField(0x1A, 1),
    "MOTOR_POLES": EepromField(0x1B, 1),
    "BRAKE_ON_STOP": EepromField(0x1C, 1),
    "STALL_PROTECTION": EepromField(0x1D, 1),
    "BEEP_VOLUME": EepromField(0x1E, 1),
    "INTERVAL_TELEMETRY": EepromField(0x1F, 1),
    "SERVO_LOW_THRESHOLD": EepromField(0x20, 1),
    "SERVO_HIGH_THRESHOLD": EepromField(0x21, 1),
    "SERVO_NEUTRAL": EepromField(0x22, 1),
    "SERVO_DEAD_BAND": EepromField(0x23, 1),
    "LOW_VOLTAGE_CUTOFF": EepromField(0x24, 1),
    "LOW_VOLTAGE_THRESHOLD": EepromField(0x25, 1),
    "RC_CAR_REVERSING": EepromField(0x26, 1),
    "USE_HALL_SENSORS": EepromField(0x27, 1),
    "SINE_MODE_RANGE": EepromField(0x28, 1),
    "BRAKE_STRENGTH": EepromField(0x29, 1),
    "RUNNING_BRAKE_LEVEL": EepromField(0x2A, 1),
    "TEMPERATURE_LIMIT": EepromField(0x2B, 1),
    "CURRENT_LIMIT": EepromField(0x2C, 1),
    "SINE_MODE_POWER": EepromField(0x2D, 1),
    "ESC_PROTOCOL": EepromField(0x2E, 1),
    "AUTO_ADVANCE": EepromField(0x2F, 1),
    "STARTUP_MELODY": EepromField(0x30, 128),
    "CAN_SETTINGS": EepromField(0xB0, 16),
}

# Read separately at (eeprom_offset - 32), 32 bytes, NUL-terminated ASCII,
# e.g. "ESCV_12S33mV" -> mcu_type "12S33mV" (text after the last '_').
FILENAME_BLOCK_SIZE = 32

_PORT_CHARACTERS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_NAME_RE = re.compile(r"[A-Za-z0-9_]+")


def field_value(settings: bytes, name: str) -> int:
    f = EEPROM_LAYOUT[name]
    return settings[f.offset]


def parse_boot_loader_pin(pin: int) -> Optional[str]:
    """0x14 -> port=1 (B), pin=4 -> 'PB4'. Returns None if out of range."""
    port = pin >> 4
    pin_number = pin & 0xF
    if 0 <= port < len(_PORT_CHARACTERS) and 0 <= pin_number <= 15:
        return f"P{_PORT_CHARACTERS[port]}{pin_number}"
    return None


def parse_name_block(raw: bytes) -> tuple[Optional[str], Optional[str]]:
    """Returns (file_name, mcu_type), or (None, None) if the block looks
    unprogrammed/garbage (e.g. all 0xFF on a blank ESC).
    """
    end = raw.find(0x00)
    text = raw[: end if end >= 0 else len(raw)].decode("ascii", errors="ignore").strip()
    if not text or not _NAME_RE.fullmatch(text):
        return None, None
    mcu_type = text.rsplit("_", 1)[1] if "_" in text else text
    return text, mcu_type
