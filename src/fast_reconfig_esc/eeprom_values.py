"""Decode raw settings bytes into the same real-world values
am32.ca/configurator's own UI displays (Timing advance in degrees, Motor KV,
PWM Frequency in kHz, Servo thresholds in microseconds, etc) - not just the
raw byte offsets in eeprom_layout.py.

The actual scaling formulas live in `eeprom_fields.py` (`decode_field`/
`encode_field`) - this module is now a thin formatter on top of that shared
spec table, used for the technical log file and any other plain-text summary.
The interactive config editor (`config_editor_widget.py`) reads/writes the
exact same specs, so the log display and the editor can never disagree about
a formula.

Cross-checked field by field against a real ESC capture (2026-08-22,
eeprom_version=1) where every single decoded value matched the site's own
display exactly: PWM 24kHz, Startup power 100%, Motor KV 380, Motor poles 14,
Beeper volume 5, Servo low/high/neutral/dead-band 1006/2006/1502/50, Low
voltage cutoff threshold 300, Current limit DISABLED, Temperature limit 90,
Sine mode range/power 15/6, Brake strength/running level 9/10, Timing advance
22.5°. See docs/PROTOCOL_NOTES.md for the full writeup.
"""
from __future__ import annotations

from dataclasses import dataclass

from .eeprom_fields import FieldSpec, applicable_specs, decode_field
from .eeprom_layout import EEPROM_LAYOUT

# i18n label_key -> plain-English label text used in the technical log file
# (the log stays English regardless of UI language, same as every other log
# line in this app). The editor UI uses the same label_key through i18n.py
# for translated labels instead.
_LOG_LABELS = {
    "field_protocol": "Protocol",
    "field_disable_stick_calibration": "Disable stick calibration",
    "field_timing_advance": "Timing advance",
    "field_startup_power": "Startup power",
    "field_motor_kv": "Motor KV",
    "field_motor_poles": "Motor poles",
    "field_beep_volume": "Beeper volume",
    "field_pwm_frequency": "PWM Frequency",
    "field_stuck_rotor_protection": "Stuck rotor protection",
    "field_stall_protection": "Stall protection",
    "field_use_hall_sensors": "Use hall sensors",
    "field_interval_telemetry": "30ms interval telemetry",
    "field_variable_pwm": "Variable PWM",
    "field_complementary_pwm": "Complementary PWM",
    "field_auto_advance": "Auto timing advance",
    "field_reversed": "Reversed",
    "field_3d_mode": "3D mode",
    "field_ramp_rate": "Ramp rate",
    "field_minimum_duty_cycle": "Minimum duty cycle",
    "field_temperature_limit": "Temperature limit",
    "field_current_limit": "Current limit",
    "field_low_voltage_cutoff": "Low voltage cutoff mode",
    "field_low_voltage_threshold": "Low voltage cut off threshold",
    "field_absolute_voltage_cutoff": "Absolute voltage cutoff",
    "field_current_p": "Current P",
    "field_current_i": "Current I",
    "field_current_d": "Current D",
    "field_sinusoidal_startup": "Sinusoidal startup",
    "field_sine_mode_range": "Sine Mode Range",
    "field_sine_mode_power": "Sine Mode Power",
    "field_brake_on_stop": "Brake on stop",
    "field_rc_car_reversing": "Car type reverse braking",
    "field_brake_strength": "Brake strength",
    "field_running_brake_level": "Running brake level",
    "field_active_brake_power": "Active brake power",
    "field_servo_low_threshold": "Servo low threshold",
    "field_servo_high_threshold": "Servo high threshold",
    "field_servo_neutral": "Servo neutral",
    "field_servo_dead_band": "Servo dead band",
}


@dataclass
class DecodedValue:
    label: str
    value: str


def _fmt(x: float) -> str:
    """Trim trailing zeros like JS's default Number-to-string (22.5, not
    22.50), while still handling whole numbers cleanly (100, not 100.0)."""
    if x == int(x):
        return str(int(x))
    return f"{x:g}"


def _format_value(spec: FieldSpec, value) -> str:
    if spec.kind == "bool":
        on_label, off_label = spec.bool_labels
        return on_label if value else off_label
    if spec.kind == "enum":
        return spec.options.get(value, f"unknown({value})") if spec.options else str(value)
    if value is None:  # number_or_disabled, at/above the disabled sentinel
        return "DISABLED"
    return f"{value:.{spec.decimals}f}" if spec.decimals else _fmt(value)


def decode_settings(settings: bytes) -> list[DecodedValue]:
    """Ordered list of (label, formatted value) for every field that applies
    to this buffer's eeprom_version, matching am32.ca's "Base" tab groupings
    and formatting. `settings` must be the full 184-byte block."""
    out: list[DecodedValue] = []

    for spec in applicable_specs(settings):
        label = _LOG_LABELS.get(spec.label_key, spec.label_key)
        value = decode_field(spec, settings)

        # Two fields have display text that isn't a plain "value+unit" -
        # kept as bespoke formatting here rather than adding editor-only
        # spec flags for cosmetics the interactive editor doesn't need.
        if spec.key == "ACTIVE_BRAKE_POWER":
            out.append(DecodedValue(label, "Off" if value == 0 else f"{_fmt(value)}% duty cycle"))
            continue
        if spec.key == "PWM_FREQUENCY":
            pwm_raw = settings[EEPROM_LAYOUT["PWM_FREQUENCY"].offset]
            variable_pwm = settings[EEPROM_LAYOUT["VARIABLE_PWM_FREQUENCY"].offset]
            text = f"{pwm_raw}kHz - {pwm_raw * 2}kHz" if variable_pwm == 1 else f"{pwm_raw}kHz"
            out.append(DecodedValue(label, text))
            continue

        text = _format_value(spec, value)
        if spec.unit and value is not None and spec.kind not in ("bool", "enum"):
            text = f"{text}{spec.unit}"
        out.append(DecodedValue(label, text))

    return out
