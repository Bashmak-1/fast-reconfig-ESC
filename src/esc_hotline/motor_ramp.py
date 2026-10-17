"""Generic MSP-value/protocol helpers for the "Прогон мотором" motor-spin-
test tab - no Qt/serial dependency, directly unit-testable without a
QTimer or real hardware. The actual ramp-shape math (never let commanded
throttle jump abruptly - an abrupt drop already burned two FC controllers)
lives in ramp_chart_math.py, the shared source of truth between the real
motor control (motor_test_manager.py) and the ramp chart
(ramp_chart_widget.py); this file stays scoped to value conversion
(percent<->MSP) and the motor-protocol byte format.

Value range (1000-2000) matches the MSP wire protocol's own convention for
MSP_SET_MOTOR, confirmed against a real MSP_MOTOR dump earlier in this
project's development (raw e8 03 == 1000) and iNavFlight/inav's mixer.c/
fc_msp.c source - not guessed.
"""
from __future__ import annotations

MSP_MIN = 1000  # 0% - motor stop/idle command
MSP_MAX = 2000  # 100%
MSP_RANGE = MSP_MAX - MSP_MIN

# Index order matches INAV's own `motor_pwm_protocol` settings table exactly
# - verified 2026-09-30 against iNavFlight/inav master's src/main/fc/
# settings.yaml `tables:` entry (the actually-enforced list the firmware
# validates against), not that same file's `description:` text for the
# setting, which is stale and still mentions ONESHOT42/DSHOT1200 - both
# removed from the real table. This is also the index MSP_ADVANCED_CONFIG/
# MSP_SET_ADVANCED_CONFIG send/expect at payload byte offset 3.
MOTOR_PROTOCOLS = ("STANDARD", "ONESHOT125", "MULTISHOT", "BRUSHED", "DSHOT150", "DSHOT300", "DSHOT600")


def protocol_name(index: int) -> str:
    """Defensive against an index outside the known table (older/future
    firmware) - never crash on an unexpected byte, just surface it as-is."""
    if 0 <= index < len(MOTOR_PROTOCOLS):
        return MOTOR_PROTOCOLS[index]
    return f"Невідомий ({index})"


def percent_to_msp(percent: float) -> int:
    """0..100 -> 1000..2000, clamped."""
    clamped = max(0.0, min(100.0, percent))
    return round(MSP_MIN + clamped / 100.0 * MSP_RANGE)


def msp_to_percent(value: int) -> float:
    """1000..2000 -> 0..100, clamped."""
    clamped = max(MSP_MIN, min(MSP_MAX, value))
    return (clamped - MSP_MIN) / MSP_RANGE * 100.0


def build_advanced_config_payload(protocol_index: int, motor_pwm_rate: int, servo_pwm_rate: int) -> bytes:
    """Exact 9-byte MSP_SET_ADVANCED_CONFIG payload for changing the motor
    protocol while echoing motor_pwm_rate/servo_pwm_rate back unchanged -
    the FC silently overwrites them with whatever is sent otherwise (see
    motor_test_manager.py's module docstring). Bytes 0-2 and 8 are
    read-and-discarded placeholders on the FC/INAV versions this project
    targets.

    Pure wire-format packing, not a state machine, so it's shared as-is
    between motor_test_manager.py (the live spin-test tab's own protocol
    change) and fc_flash_station.py (the auto-provisioning tab's final
    protocol-write step) - unlike the state machines around each call
    site, which deliberately stay separate, a byte-packing routine has no
    behavioural coupling to share, only a single source of truth for the
    offsets/order worth protecting from ever drifting apart.
    """
    return (
        bytes([1, 1, 1, protocol_index])
        + motor_pwm_rate.to_bytes(2, "little")
        + servo_pwm_rate.to_bytes(2, "little")
        + bytes([0])
    )


def count_active_motors(raw_motor_values: list[int]) -> int:
    """How many of MSP_MOTOR's (always 8) slots are actually configured
    outputs, vs. padding. INAV's fc_msp.c fills unconfigured slots with
    exactly 0 (`i < MAX_SUPPORTED_MOTORS ? motor[i] : 0`), while a real,
    configured-but-idle motor reads its stop command (MSP_MIN, never 0) -
    confirmed on real hardware earlier in this project: the same FC
    reported "2 ESC output(s)" via the separate FourWay/MSP_SET_PASSTHROUGH
    world AND showed exactly 2 non-zero MSP_MOTOR slots, independently.
    Pure/no-Qt so it's unit-testable without a real MSP_MOTOR response.
    """
    return sum(1 for v in raw_motor_values if v != 0)


def cubic_bezier_ease(t: float) -> float:
    """Ease-in-out progress curve for the "Безе" ramp mode, offered as an
    alternative to a constant-speed linear ramp (never a replacement for
    it - see ramp_chart_math.segment_value(), the shared function both
    motor_test_manager.py's real motor control and the ramp chart use).

    This is a genuine cubic Bezier curve, not just a vaguely S-shaped
    approximation: with control points P0=0, P1=0, P2=1, P3=1, the
    standard Bezier formula B(t) = (1-t)^3*P0 + 3(1-t)^2*t*P1 +
    3(1-t)*t^2*P2 + t^3*P3 reduces algebraically to 3t^2 - 2t^3 - the
    well-known "smoothstep" curve. Starts and ends with zero slope (no
    abrupt jerk at either end, unlike the linear ramp's constant rate,
    which is exactly the "gentler" shape requested), monotonic over
    [0, 1], and symmetric around t=0.5.

    `t` is clamped to [0, 1] first, so the result is always in [0, 1] too
    - callers never need to clamp the output themselves.
    """
    clamped = max(0.0, min(1.0, t))
    return 3.0 * clamped**2 - 2.0 * clamped**3
