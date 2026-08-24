"""Round-trip tests for eeprom_fields.py's decode_field/encode_field - the
math the interactive config editor relies on to turn a raw byte into an
editable real-world value and back, without drifting from what the read-only
decoder (eeprom_values.decode_settings) shows for the exact same byte.
"""
from __future__ import annotations

import pytest

from fast_reconfig_esc.eeprom_fields import (
    FIELD_SPECS,
    applicable_specs,
    decode_field,
    encode_field,
    eeprom_version_of,
    is_field_applicable,
)

# Same real capture used in test_eeprom_values.py (ARM64K, 12S33mV, eeprom v1).
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


EEPROM_LOW_VOLTAGE_CUTOFF_OFFSET = 0x24


def _real_settings() -> bytes:
    return bytes.fromhex(REAL_SETTINGS_HEX.replace(" ", ""))


def test_eeprom_version_of_real_capture():
    assert eeprom_version_of(_real_settings()) == 1


@pytest.mark.parametrize("spec", FIELD_SPECS, ids=lambda s: f"{s.key}:{s.kind}")
def test_decode_then_encode_round_trips_for_every_raw_byte_value(spec):
    """For every possible raw byte 0-255, decoding then re-encoding must
    reproduce a raw value that decodes to the SAME real-world value - the
    property that actually matters (encode need not reproduce the exact
    original byte when the mapping isn't 1:1, e.g. a "disabled" sentinel
    range, but it must never silently change what gets written)."""
    for raw in range(256):
        settings = bytearray(184)
        settings[spec.offset] = raw
        value = decode_field(spec, bytes(settings))
        re_raw = encode_field(spec, value)
        assert 0 <= re_raw <= 255
        settings[spec.offset] = re_raw
        re_value = decode_field(spec, bytes(settings))
        assert re_value == value, f"{spec.key} raw={raw} -> value={value!r} -> raw={re_raw} -> value={re_value!r}"


def test_encode_field_bool_true_false():
    spec = next(s for s in FIELD_SPECS if s.key == "STUCK_ROTOR_PROTECTION")
    assert encode_field(spec, True) == 1
    assert encode_field(spec, False) == 0


def test_encode_field_number_or_disabled_sentinel_round_trips():
    temp_spec = next(s for s in FIELD_SPECS if s.key == "TEMPERATURE_LIMIT")
    assert encode_field(temp_spec, None) == 141
    settings = bytearray(184)
    settings[temp_spec.offset] = 141
    assert decode_field(temp_spec, bytes(settings)) is None

    assert encode_field(temp_spec, 90.0) == 90
    settings[temp_spec.offset] = 90
    assert decode_field(temp_spec, bytes(settings)) == 90.0


def test_encode_field_clamps_out_of_range_to_valid_byte():
    kv_spec = next(s for s in FIELD_SPECS if s.key == "MOTOR_KV")
    assert encode_field(kv_spec, 999999) == 255  # way above max_display -> clamps to top byte
    assert encode_field(kv_spec, -999999) == 0  # way below min_display -> clamps to bottom byte


def test_timing_advance_encode_matches_real_capture_both_versions():
    v1_spec = next(s for s in FIELD_SPECS if s.key == "TIMING_ADVANCE" and s.max_eeprom_version == 2)
    v3_spec = next(s for s in FIELD_SPECS if s.key == "TIMING_ADVANCE" and s.min_eeprom_version == 3)

    settings = bytearray(_real_settings())
    assert is_field_applicable(v1_spec, bytes(settings))
    assert not is_field_applicable(v3_spec, bytes(settings))
    assert decode_field(v1_spec, bytes(settings)) == 22.5  # raw=3 -> 3*7.5

    settings[0x01] = 3  # bump eeprom_version to 3
    settings[0x17] = 20  # raw TIMING_ADVANCE
    assert not is_field_applicable(v1_spec, bytes(settings))
    assert is_field_applicable(v3_spec, bytes(settings))
    assert decode_field(v3_spec, bytes(settings)) == pytest.approx((20 - 10) * 0.9375)
    assert encode_field(v3_spec, (20 - 10) * 0.9375) == 20


def test_applicable_specs_excludes_out_of_version_variants():
    specs = applicable_specs(_real_settings())  # eeprom_version=1
    timing_specs = [s for s in specs if s.key == "TIMING_ADVANCE"]
    assert len(timing_specs) == 1
    assert timing_specs[0].max_eeprom_version == 2

    pwm_specs = [s for s in specs if s.key == "PWM_FREQUENCY"]
    assert len(pwm_specs) == 1
    assert pwm_specs[0].max_eeprom_version == 2

    # eeprom v1 -> the v3+-only groups must be entirely absent
    assert not any(s.key in ("MAX_RAMP", "MINIMUM_DUTY_CYCLE", "CURRENT_P", "ACTIVE_BRAKE_POWER") for s in specs)


def test_low_voltage_threshold_visibility_follows_cutoff_mode():
    lvt_spec = next(s for s in FIELD_SPECS if s.key == "LOW_VOLTAGE_THRESHOLD")
    settings = bytearray(184)

    settings[EEPROM_LOW_VOLTAGE_CUTOFF_OFFSET] = 0
    assert is_field_applicable(lvt_spec, bytes(settings))

    settings[EEPROM_LOW_VOLTAGE_CUTOFF_OFFSET] = 2
    assert not is_field_applicable(lvt_spec, bytes(settings))
