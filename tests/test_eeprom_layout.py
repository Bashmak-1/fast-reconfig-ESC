"""Tests for eeprom_layout.py's identity-byte helpers - the fix for a
real-hardware-confirmed false "readback mismatch after write": a config
file dumped from a different board/firmware build legitimately carries a
different BOOT_LOADER_REVISION etc. (hardware identity, not a
configurable setting), and the firmware silently refuses to let a host
overwrite its own identity bytes.
"""
from __future__ import annotations

from esc_hotline.eeprom_layout import EEPROM_LAYOUT, settings_equal_ignoring_identity, with_identity_from
from esc_hotline.mcu_profiles import LAYOUT_SIZE


def _bytes(fill: int) -> bytes:
    return bytes([fill]) * LAYOUT_SIZE


def test_with_identity_from_replaces_only_identity_bytes():
    target = _bytes(0x42)
    identity_source = _bytes(0x99)

    result = with_identity_from(target, identity_source)

    for name, field in EEPROM_LAYOUT.items():
        expected_source = identity_source if name in (
            "BOOT_BYTE", "LAYOUT_REVISION", "BOOT_LOADER_REVISION", "MAIN_REVISION", "SUB_REVISION"
        ) else target
        assert result[field.offset : field.offset + field.size] == expected_source[field.offset : field.offset + field.size]


def test_settings_equal_ignoring_identity_true_when_only_identity_differs():
    a = bytearray(_bytes(0x42))
    b = bytearray(a)
    b[0x01] = 4  # LAYOUT_REVISION
    b[0x02] = 7  # BOOT_LOADER_REVISION
    b[0x03] = 2  # MAIN_REVISION
    b[0x04] = 16  # SUB_REVISION

    assert settings_equal_ignoring_identity(bytes(a), bytes(b)) is True


def test_settings_equal_ignoring_identity_false_when_a_real_setting_differs():
    a = bytearray(_bytes(0x42))
    b = bytearray(a)
    b[EEPROM_LAYOUT["MOTOR_KV"].offset] = 0x01

    assert settings_equal_ignoring_identity(bytes(a), bytes(b)) is False
