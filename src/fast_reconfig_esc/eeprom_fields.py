"""Declarative spec for every editable ESC setting field: how to convert
between the raw EEPROM byte and the real-world value a person edits/reads
(degrees, KV, %, kHz, microseconds, etc).

Every scaling factor/offset/range/enum here is transcribed from
am32-configurator's pages/configurator.vue (`<SettingField ... />` props) -
the same source `eeprom_values.py`'s read-only decoder was originally built
from. This module is now the single source of truth for the math; both the
read-only log/summary decoder and the interactive editor call `decode_field`/
`encode_field` so they can never disagree about a formula.

Site formula, from components/SettingField.vue:
    value    = raw * display_factor + display_offset
    rendered = value * only_display_factor   (only TIMING_ADVANCE has a
               non-1 only_display_factor; every other numeric field has
               only_display_factor=1, so rendered == value there)
Inverting, for edits:
    raw = round((rendered / only_display_factor - display_offset) / display_factor)

`STARTUP_MELODY` (128-byte RTTTL tune) and `CAN_SETTINGS` (16 bytes) are
deliberately not covered here - not editable in this tool, left untouched
whenever a working buffer is built from a base buffer (see
config_editor_widget.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Union

from .eeprom_layout import EEPROM_LAYOUT

PROTOCOL_OPTIONS = {0: "Auto", 1: "DShot", 2: "Servo", 3: "Serial", 4: "EDT ARM"}
LOW_VOLTAGE_CUTOFF_OPTIONS = {0: "Off", 1: "Cell based", 2: "Absolute"}

FieldValue = Union[bool, int, float, None]


@dataclass(frozen=True)
class FieldSpec:
    key: str  # name in EEPROM_LAYOUT
    label_key: str  # i18n key for the field's own label
    group_key: str  # i18n key for the section header this field belongs to
    kind: str  # "bool" | "enum" | "number" | "number_or_disabled"
    min_display: float = 0
    max_display: float = 255
    decimals: int = 0
    display_factor: float = 1.0
    display_offset: float = 0.0
    only_display_factor: float = 1.0
    unit: str = ""
    options: Optional[dict] = None  # kind == "enum"
    disabled_at_raw: Optional[int] = None  # kind == "number_or_disabled": raw >= this means "off"
    min_eeprom_version: Optional[int] = None
    max_eeprom_version: Optional[int] = None
    visible_when: Optional[Callable[[bytes], bool]] = None
    bool_labels: tuple = ("On", "Off")

    @property
    def offset(self) -> int:
        return EEPROM_LAYOUT[self.key].offset


def eeprom_version_of(settings: bytes) -> int:
    return settings[EEPROM_LAYOUT["LAYOUT_REVISION"].offset]


def is_field_applicable(spec: FieldSpec, settings: bytes) -> bool:
    version = eeprom_version_of(settings)
    if spec.min_eeprom_version is not None and version < spec.min_eeprom_version:
        return False
    if spec.max_eeprom_version is not None and version > spec.max_eeprom_version:
        return False
    if spec.visible_when is not None and not spec.visible_when(settings):
        return False
    return True


def _round_clamp_byte(x: float) -> int:
    return max(0, min(255, round(x)))


def decode_field(spec: FieldSpec, settings: bytes) -> FieldValue:
    raw = settings[spec.offset]
    if spec.kind == "bool":
        return raw not in (0, 0xFF)
    if spec.kind == "enum":
        return raw
    if spec.kind == "number_or_disabled" and spec.disabled_at_raw is not None and raw >= spec.disabled_at_raw:
        return None
    value = raw * spec.display_factor + spec.display_offset
    return value * spec.only_display_factor


def encode_field(spec: FieldSpec, value: FieldValue) -> int:
    """value's type/domain matches what decode_field returns for this spec."""
    if spec.kind == "bool":
        return 1 if value else 0
    if spec.kind == "enum":
        return int(value)  # type: ignore[arg-type]
    if spec.kind == "number_or_disabled" and value is None:
        assert spec.disabled_at_raw is not None
        return spec.disabled_at_raw
    rendered = float(value)  # type: ignore[arg-type]
    raw = (rendered / spec.only_display_factor - spec.display_offset) / spec.display_factor
    return _round_clamp_byte(raw)


def _lvc_below_2(settings: bytes) -> bool:
    return settings[EEPROM_LAYOUT["LOW_VOLTAGE_CUTOFF"].offset] < 2


def _lvc_is_absolute(settings: bytes) -> bool:
    return settings[EEPROM_LAYOUT["LOW_VOLTAGE_CUTOFF"].offset] == 2


FIELD_SPECS: list[FieldSpec] = [
    # Essentials
    FieldSpec("ESC_PROTOCOL", "field_protocol", "group_essentials", "enum", options=PROTOCOL_OPTIONS),
    FieldSpec(
        "DISABLE_STICK_CALIBRATION", "field_disable_stick_calibration", "group_essentials", "bool",
        min_eeprom_version=3,
    ),
    # Motor
    FieldSpec(
        "TIMING_ADVANCE", "field_timing_advance", "group_motor", "number",
        min_display=0, max_display=32, display_factor=1, display_offset=-10,
        only_display_factor=0.9375, unit="°", min_eeprom_version=3,
    ),
    FieldSpec(
        "TIMING_ADVANCE", "field_timing_advance", "group_motor", "number",
        min_display=0, max_display=22.5, display_factor=7.5,
        unit="°", max_eeprom_version=2,
    ),
    FieldSpec("STARTUP_POWER", "field_startup_power", "group_motor", "number", min_display=50, max_display=150, unit="%"),
    FieldSpec(
        "MOTOR_KV", "field_motor_kv", "group_motor", "number",
        min_display=20, max_display=10220, display_factor=40, display_offset=20,
    ),
    FieldSpec("MOTOR_POLES", "field_motor_poles", "group_motor", "number", min_display=2, max_display=36),
    FieldSpec("BEEP_VOLUME", "field_beep_volume", "group_motor", "number", min_display=0, max_display=11),
    FieldSpec(
        "PWM_FREQUENCY", "field_pwm_frequency", "group_motor", "number",
        min_display=8, max_display=144, unit="kHz", min_eeprom_version=3,
    ),
    FieldSpec(
        "PWM_FREQUENCY", "field_pwm_frequency", "group_motor", "number",
        min_display=8, max_display=48, unit="kHz", max_eeprom_version=2,
    ),
    FieldSpec("STUCK_ROTOR_PROTECTION", "field_stuck_rotor_protection", "group_motor", "bool"),
    FieldSpec("STALL_PROTECTION", "field_stall_protection", "group_motor", "bool"),
    FieldSpec("USE_HALL_SENSORS", "field_use_hall_sensors", "group_motor", "bool"),
    FieldSpec("INTERVAL_TELEMETRY", "field_interval_telemetry", "group_motor", "bool"),
    FieldSpec("VARIABLE_PWM_FREQUENCY", "field_variable_pwm", "group_motor", "bool"),
    FieldSpec("COMPLEMENTARY_PWM", "field_complementary_pwm", "group_motor", "bool"),
    FieldSpec("AUTO_ADVANCE", "field_auto_advance", "group_motor", "bool"),
    FieldSpec("MOTOR_DIRECTION", "field_reversed", "group_motor", "bool", bool_labels=("Yes", "No")),
    FieldSpec("BIDIRECTIONAL_MODE", "field_3d_mode", "group_motor", "bool", bool_labels=("Yes", "No")),
    # Extended settings (eeprom v3+)
    FieldSpec(
        "MAX_RAMP", "field_ramp_rate", "group_extended", "number",
        min_display=0.1, max_display=20, decimals=1, display_factor=0.1,
        unit="% duty cycle per ms", min_eeprom_version=3,
    ),
    FieldSpec(
        "MINIMUM_DUTY_CYCLE", "field_minimum_duty_cycle", "group_extended", "number",
        min_display=0, max_display=25, decimals=1, display_factor=0.5,
        unit="%", min_eeprom_version=3,
    ),
    # Limits
    FieldSpec(
        "TEMPERATURE_LIMIT", "field_temperature_limit", "group_limits", "number_or_disabled",
        min_display=70, max_display=140, disabled_at_raw=141,
    ),
    FieldSpec(
        "CURRENT_LIMIT", "field_current_limit", "group_limits", "number_or_disabled",
        min_display=0, max_display=200, display_factor=2, disabled_at_raw=101,
    ),
    FieldSpec("LOW_VOLTAGE_CUTOFF", "field_low_voltage_cutoff", "group_limits", "enum", options=LOW_VOLTAGE_CUTOFF_OPTIONS),
    FieldSpec(
        "LOW_VOLTAGE_THRESHOLD", "field_low_voltage_threshold", "group_limits", "number",
        min_display=250, max_display=350, display_offset=250, visible_when=_lvc_below_2,
    ),
    FieldSpec(
        "ABSOLUTE_VOLTAGE_CUTOFF", "field_absolute_voltage_cutoff", "group_limits", "number",
        min_display=0.5, max_display=50, decimals=1, display_factor=0.5, unit="V",
        min_eeprom_version=3, visible_when=_lvc_is_absolute,
    ),
    # Current control (eeprom v3+)
    FieldSpec("CURRENT_P", "field_current_p", "group_current_control", "number", min_eeprom_version=3),
    FieldSpec("CURRENT_I", "field_current_i", "group_current_control", "number", min_eeprom_version=3),
    FieldSpec("CURRENT_D", "field_current_d", "group_current_control", "number", min_eeprom_version=3),
    # Sinusoidal Startup
    FieldSpec("SINUSOIDAL_STARTUP", "field_sinusoidal_startup", "group_sinusoidal", "bool"),
    FieldSpec("SINE_MODE_RANGE", "field_sine_mode_range", "group_sinusoidal", "number", min_display=5, max_display=25),
    FieldSpec("SINE_MODE_POWER", "field_sine_mode_power", "group_sinusoidal", "number", min_display=1, max_display=10),
    # Brake
    FieldSpec("BRAKE_ON_STOP", "field_brake_on_stop", "group_brake", "bool"),
    FieldSpec("RC_CAR_REVERSING", "field_rc_car_reversing", "group_brake", "bool"),
    FieldSpec("BRAKE_STRENGTH", "field_brake_strength", "group_brake", "number", min_display=1, max_display=10),
    FieldSpec("RUNNING_BRAKE_LEVEL", "field_running_brake_level", "group_brake", "number", min_display=1, max_display=10),
    FieldSpec(
        "ACTIVE_BRAKE_POWER", "field_active_brake_power", "group_brake", "number",
        min_display=0, max_display=5, unit="% duty cycle", min_eeprom_version=3,
    ),
    # Servo settings
    FieldSpec(
        "SERVO_LOW_THRESHOLD", "field_servo_low_threshold", "group_servo", "number",
        min_display=750, max_display=1250, display_factor=2, display_offset=750,
    ),
    FieldSpec(
        "SERVO_HIGH_THRESHOLD", "field_servo_high_threshold", "group_servo", "number",
        min_display=1750, max_display=2250, display_factor=2, display_offset=1750,
    ),
    FieldSpec(
        "SERVO_NEUTRAL", "field_servo_neutral", "group_servo", "number",
        min_display=1374, max_display=1630, display_offset=1374,
    ),
    FieldSpec("SERVO_DEAD_BAND", "field_servo_dead_band", "group_servo", "number", min_display=0, max_display=100),
]

GROUP_ORDER = [
    "group_essentials",
    "group_motor",
    "group_extended",
    "group_limits",
    "group_current_control",
    "group_sinusoidal",
    "group_brake",
    "group_servo",
]


def applicable_specs(settings: bytes) -> list[FieldSpec]:
    """FIELD_SPECS filtered/deduped for one settings buffer's eeprom_version -
    resolves the two TIMING_ADVANCE/PWM_FREQUENCY version-variants down to
    the single one that actually applies."""
    return [spec for spec in FIELD_SPECS if is_field_applicable(spec, settings)]
