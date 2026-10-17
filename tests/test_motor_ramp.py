"""Tests for motor_ramp.py - the rate-limiting math behind "Прогон мотором"
(no Qt/serial dependency). This is the actual safety mechanism for the
feature (never let commanded throttle jump by more than a bounded amount),
so its edge cases - never overshooting, never stalling with a slow rate,
landing exactly on the target - matter more than usual.
"""
from __future__ import annotations

from esc_hotline.motor_ramp import (
    MOTOR_PROTOCOLS,
    MSP_MAX,
    MSP_MIN,
    build_advanced_config_payload,
    count_active_motors,
    cubic_bezier_ease,
    msp_to_percent,
    next_value,
    next_value_bezier,
    percent_to_msp,
    protocol_name,
)


def test_percent_to_msp_endpoints():
    assert percent_to_msp(0) == MSP_MIN
    assert percent_to_msp(100) == MSP_MAX


def test_percent_to_msp_midpoint():
    assert percent_to_msp(50) == MSP_MIN + (MSP_MAX - MSP_MIN) // 2


def test_percent_to_msp_clamps_out_of_range():
    assert percent_to_msp(-10) == MSP_MIN
    assert percent_to_msp(150) == MSP_MAX


def test_msp_to_percent_endpoints():
    assert msp_to_percent(MSP_MIN) == 0.0
    assert msp_to_percent(MSP_MAX) == 100.0


def test_msp_to_percent_clamps_out_of_range():
    assert msp_to_percent(500) == 0.0
    assert msp_to_percent(3000) == 100.0


def test_percent_msp_round_trip_is_consistent():
    for pct in (0, 1, 25, 50, 75, 99, 100):
        assert round(msp_to_percent(percent_to_msp(pct))) == pct


# ---- next_value: the actual rate limiter ------------------------------------------------


def test_next_value_steps_up_towards_target():
    # rate=10%/s over a 0.1s tick should move by 10% of (MSP_MAX-MSP_MIN)*0.1/1.0 = 1% of range
    value = next_value(MSP_MIN, MSP_MAX, rate_percent_per_s=10.0, tick_s=0.1)
    assert MSP_MIN < value < MSP_MAX


def test_next_value_never_overshoots_target_going_up():
    # a huge rate/tick combo must clamp to target exactly, not step past it
    value = next_value(MSP_MIN, MSP_MAX, rate_percent_per_s=1000.0, tick_s=1.0)
    assert value == MSP_MAX


def test_next_value_never_overshoots_target_going_down():
    value = next_value(MSP_MAX, MSP_MIN, rate_percent_per_s=1000.0, tick_s=1.0)
    assert value == MSP_MIN


def test_next_value_reaches_target_exactly_eventually():
    current = MSP_MIN
    for _ in range(10_000):  # generous upper bound - must terminate well before this
        if current == MSP_MAX:
            break
        current = next_value(current, MSP_MAX, rate_percent_per_s=5.0, tick_s=0.1)
    assert current == MSP_MAX


def test_next_value_already_at_target_is_a_no_op():
    assert next_value(1500, 1500, rate_percent_per_s=10.0, tick_s=0.1) == 1500


def test_next_value_zero_rate_never_moves():
    assert next_value(MSP_MIN, MSP_MAX, rate_percent_per_s=0.0, tick_s=1.0) == MSP_MIN


def test_next_value_negative_rate_never_moves():
    assert next_value(MSP_MIN, MSP_MAX, rate_percent_per_s=-5.0, tick_s=1.0) == MSP_MIN


def test_next_value_tiny_tick_still_makes_progress():
    # even a very small tick_s must move at least 1 raw MSP unit, never stall
    value = next_value(MSP_MIN, MSP_MAX, rate_percent_per_s=1.0, tick_s=0.001)
    assert value == MSP_MIN + 1


def test_next_value_moves_down_towards_lower_target():
    value = next_value(MSP_MAX, MSP_MIN, rate_percent_per_s=10.0, tick_s=0.1)
    assert MSP_MIN < value < MSP_MAX


def test_next_value_higher_rate_moves_further_per_tick():
    slow = next_value(MSP_MIN, MSP_MAX, rate_percent_per_s=5.0, tick_s=0.1)
    fast = next_value(MSP_MIN, MSP_MAX, rate_percent_per_s=50.0, tick_s=0.1)
    assert fast > slow


# ---- cubic_bezier_ease / next_value_bezier ------------------------------------------------
# The alternative "Безе" ramp mode offered alongside (never replacing) the
# linear next_value() above - see motor_ramp.py's docstring for why 3t^2-2t^3
# genuinely is a cubic Bezier curve here, not just S-shaped-looking math.


def test_cubic_bezier_ease_endpoints():
    assert cubic_bezier_ease(0.0) == 0.0
    assert cubic_bezier_ease(1.0) == 1.0


def test_cubic_bezier_ease_exact_midpoint():
    # 3*(0.5)^2 - 2*(0.5)^3 = 0.75 - 0.25 = 0.5 exactly
    assert cubic_bezier_ease(0.5) == 0.5


def test_cubic_bezier_ease_clamps_out_of_range():
    assert cubic_bezier_ease(-1.0) == 0.0
    assert cubic_bezier_ease(2.0) == 1.0


def test_cubic_bezier_ease_is_monotonic():
    assert cubic_bezier_ease(0.2) < cubic_bezier_ease(0.5) < cubic_bezier_ease(0.8)


def test_cubic_bezier_ease_is_symmetric_around_midpoint():
    assert round(cubic_bezier_ease(0.3) + cubic_bezier_ease(0.7), 9) == 1.0


def test_next_value_bezier_at_start_is_start_value():
    assert next_value_bezier(MSP_MIN, MSP_MAX, elapsed_s=0.0, duration_s=10.0) == MSP_MIN


def test_next_value_bezier_at_duration_is_target_exactly():
    assert next_value_bezier(MSP_MIN, MSP_MAX, elapsed_s=10.0, duration_s=10.0) == MSP_MAX


def test_next_value_bezier_at_half_duration_is_exact_midpoint():
    expected = MSP_MIN + (MSP_MAX - MSP_MIN) // 2
    assert next_value_bezier(MSP_MIN, MSP_MAX, elapsed_s=5.0, duration_s=10.0) == expected


def test_next_value_bezier_never_overshoots_past_duration():
    assert next_value_bezier(MSP_MIN, MSP_MAX, elapsed_s=999.0, duration_s=10.0) == MSP_MAX


def test_next_value_bezier_negative_elapsed_clamps_to_start():
    assert next_value_bezier(MSP_MIN, MSP_MAX, elapsed_s=-5.0, duration_s=10.0) == MSP_MIN


def test_next_value_bezier_zero_duration_is_safe_and_reaches_target():
    assert next_value_bezier(MSP_MIN, MSP_MAX, elapsed_s=0.0, duration_s=0.0) == MSP_MAX


def test_next_value_bezier_negative_duration_is_safe_and_reaches_target():
    assert next_value_bezier(MSP_MIN, MSP_MAX, elapsed_s=1.0, duration_s=-1.0) == MSP_MAX


def test_next_value_bezier_moves_down_towards_lower_target():
    value = next_value_bezier(MSP_MAX, MSP_MIN, elapsed_s=5.0, duration_s=10.0)
    assert MSP_MIN < value < MSP_MAX


# ---- count_active_motors ------------------------------------------------
# Real-hardware-confirmed convention (see motor_ramp.py docstring): INAV
# fills unconfigured MSP_MOTOR slots with exactly 0, real ones with >= MSP_MIN.


def test_count_active_motors_all_zero_is_none():
    assert count_active_motors([0] * 8) == 0


def test_count_active_motors_matches_real_capture():
    # the exact shape seen on real hardware: 2 configured (at MSP_MIN), 6 padding zeros
    raw = [1000, 1000, 0, 0, 0, 0, 0, 0]
    assert count_active_motors(raw) == 2


def test_count_active_motors_all_eight_configured():
    assert count_active_motors([1500] * 8) == 8


def test_count_active_motors_empty_list_is_zero():
    assert count_active_motors([]) == 0


def test_count_active_motors_ignores_gaps_counts_any_nonzero():
    # a real gap in the middle shouldn't happen per INAV's own packing, but
    # the function itself is a simple count, not position-dependent - pin
    # that down explicitly so a future change can't silently start assuming order.
    assert count_active_motors([1000, 0, 1000, 0, 0, 0, 0, 0]) == 2


# ---- MOTOR_PROTOCOLS / protocol_name ------------------------------------------------
# Table order verified against iNavFlight/inav master's src/main/fc/settings.yaml
# `tables:` entry for motor_pwm_protocol (the enforced list, not that file's stale
# `description:` text, which still mentions ONESHOT42/DSHOT1200 - both removed).


def test_motor_protocols_matches_inav_table_order():
    assert MOTOR_PROTOCOLS == ("STANDARD", "ONESHOT125", "MULTISHOT", "BRUSHED", "DSHOT150", "DSHOT300", "DSHOT600")


def test_protocol_name_known_indices():
    assert protocol_name(0) == "STANDARD"
    assert protocol_name(4) == "DSHOT150"
    assert protocol_name(6) == "DSHOT600"


def test_protocol_name_unknown_index_does_not_crash():
    # defensive against an index outside the known table (older/future firmware)
    assert protocol_name(99) == "Невідомий (99)"
    assert protocol_name(-1) == "Невідомий (-1)"


# ---- build_advanced_config_payload ------------------------------------------------
# Shared byte-packing used by both motor_test_manager.py's own set_protocol()
# and fc_flash_station.py's auto-provisioning protocol-write step - pinned
# down exactly so the two call sites can never silently drift apart.


def test_build_advanced_config_payload_exact_bytes():
    payload = build_advanced_config_payload(protocol_index=5, motor_pwm_rate=480, servo_pwm_rate=50)
    assert payload == bytes([1, 1, 1, 5]) + (480).to_bytes(2, "little") + (50).to_bytes(2, "little") + bytes([0])


def test_build_advanced_config_payload_is_9_bytes():
    assert len(build_advanced_config_payload(0, 0, 0)) == 9


def test_build_advanced_config_payload_echoes_rates_unchanged():
    payload = build_advanced_config_payload(protocol_index=0, motor_pwm_rate=1000, servo_pwm_rate=50)
    assert payload[4:6] == (1000).to_bytes(2, "little")
    assert payload[6:8] == (50).to_bytes(2, "little")
