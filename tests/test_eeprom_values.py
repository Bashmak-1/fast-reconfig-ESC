"""Verifies decode_settings() against a real ESC capture (2026-08-22,
eeprom_version=1) whose values were independently confirmed to match
am32.ca/configurator's own displayed numbers exactly - see
docs/PROTOCOL_NOTES.md and eeprom_values.py's module docstring.
"""
from __future__ import annotations

from esc_hotline.eeprom_values import decode_settings

# Real bytes read back from a live ESC (ARM64K, 12S33mV, fw 2.16, eeprom v1).
REAL_SETTINGS_HEX = (
    "01 01 07 02 10 ff ff ff ff ff ff ff ff ff ff ff ff 00 00 01 01 00 01 03 "
    "18 64 09 0e 00 01 05 01 80 80 80 32 00 32 00 00 0f 09 0a 5a 65 06 01 00 "
    "ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff "
    "ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff "
    "ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff "
    "ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff "
    "ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff "
    "ff ff ff ff ff ff ff ff fd fd fd fd fd fd fd fd "
)


def _decoded_dict(settings: bytes) -> dict[str, str]:
    return {d.label: d.value for d in decode_settings(settings)}


def test_decode_settings_matches_real_am32ca_display():
    settings = bytes.fromhex(REAL_SETTINGS_HEX.replace(" ", ""))
    assert len(settings) == 184

    values = _decoded_dict(settings)

    # These are the exact values the user captured from am32.ca/configurator's
    # own UI for this same ESC - not invented.
    assert values["Protocol"] == "DShot"
    assert values["Timing advance"] == "22.5°"
    assert values["Startup power"] == "100%"
    assert values["Motor KV"] == "380"
    assert values["Motor poles"] == "14"
    assert values["Beeper volume"] == "5"
    assert values["PWM Frequency"] == "24kHz"
    assert values["Temperature limit"] == "90"
    assert values["Current limit"] == "DISABLED"
    assert values["Low voltage cut off threshold"] == "300"
    assert values["Sine Mode Range"] == "15"
    assert values["Sine Mode Power"] == "6"
    assert values["Brake strength"] == "9"
    assert values["Running brake level"] == "10"
    assert values["Servo low threshold"] == "1006"
    assert values["Servo high threshold"] == "2006"
    assert values["Servo neutral"] == "1502"
    assert values["Servo dead band"] == "50"
    assert values["Reversed"] == "No"
    assert values["3D mode"] == "No"


def test_decode_settings_current_limit_not_disabled_when_below_threshold():
    settings = bytearray(bytes.fromhex(REAL_SETTINGS_HEX.replace(" ", "")))
    settings[0x2C] = 50  # CURRENT_LIMIT raw -> 50*2=100A, well under the 202 "disabled" sentinel
    values = _decoded_dict(bytes(settings))
    assert values["Current limit"] == "100"


def test_decode_settings_timing_advance_v3_formula_differs_from_v1():
    settings = bytearray(bytes.fromhex(REAL_SETTINGS_HEX.replace(" ", "")))
    settings[0x01] = 3  # LAYOUT_REVISION -> eeprom_version 3, switches TIMING_ADVANCE formula
    settings[0x17] = 20  # raw TIMING_ADVANCE
    values = _decoded_dict(bytes(settings))
    # v>=3 formula: (raw - 10) * 0.9375
    assert values["Timing advance"] == f"{(20 - 10) * 0.9375:g}°"
