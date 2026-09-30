"""Pure ramp-stepping math for the "Прогон мотором" motor-spin-test tab -
no Qt/serial dependency, so the rate-limiting logic (the whole point of this
feature: never let commanded throttle jump by more than a bounded amount per
tick, since an abrupt drop already burned two FC controllers) is directly
unit-testable without a QTimer or real hardware.

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


def next_value(current: int, target: int, rate_percent_per_s: float, tick_s: float) -> int:
    """One ramp step from `current` towards `target`, moving at most
    `rate_percent_per_s` percent of the full 1000-2000 range per second of
    wall-clock time (`tick_s` since the last step). Never overshoots target
    - the final step lands exactly on it rather than stepping past.

    Pure integer MSP units in, pure integer MSP units out, so the caller
    (MotorTestManager) can feed this straight into MSP_SET_MOTOR without
    further rounding surprises.
    """
    if rate_percent_per_s <= 0:
        return current
    max_step = round(rate_percent_per_s / 100.0 * MSP_RANGE * tick_s)
    max_step = max(max_step, 1)  # a nonzero rate must always make *some* progress
    if current < target:
        return min(current + max_step, target)
    if current > target:
        return max(current - max_step, target)
    return current
