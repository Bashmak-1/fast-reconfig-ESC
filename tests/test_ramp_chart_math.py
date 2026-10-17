"""Tests for ramp_chart_math.py - the pure geometry/physics shared by both
the real motor control (motor_test_manager.py) and the ramp chart
(ramp_chart_widget.py). `segment_value`/`profile_value_at_time` are the
most important tests here: both call sites must compute the exact same
numbers, or the chart's live marker would visibly drift from the real
motor position.
"""
from __future__ import annotations

from esc_hotline.ramp_chart_math import (
    MAX_PHASE_DURATION_S,
    MIN_BOUND_GAP_PERCENT,
    MIN_PHASE_DURATION_S,
    clamp_bound_pair,
    clamp_duration_s,
    percent_to_pixel_y,
    pixel_x_to_time,
    pixel_y_to_percent,
    profile_value_at_time,
    segment_value,
    time_to_pixel_x,
)

# ---- segment_value ------------------------------------------------


def test_segment_value_linear_endpoints():
    assert segment_value(False, 0.0, 10.0, 20.0, 80.0) == 20.0
    assert segment_value(False, 10.0, 10.0, 20.0, 80.0) == 80.0


def test_segment_value_linear_midpoint():
    assert segment_value(False, 5.0, 10.0, 20.0, 80.0) == 50.0


def test_segment_value_bezier_endpoints():
    assert segment_value(True, 0.0, 10.0, 20.0, 80.0) == 20.0
    assert segment_value(True, 10.0, 10.0, 20.0, 80.0) == 80.0


def test_segment_value_bezier_exact_midpoint_matches_smoothstep():
    # cubic_bezier_ease(0.5) == 0.5 exactly (see motor_ramp.py) - so the
    # eased midpoint must land exactly on the arithmetic midpoint too.
    assert segment_value(True, 5.0, 10.0, 0.0, 100.0) == 50.0


def test_segment_value_bezier_differs_from_linear_off_midpoint():
    linear = segment_value(False, 2.5, 10.0, 0.0, 100.0)
    bezier = segment_value(True, 2.5, 10.0, 0.0, 100.0)
    assert bezier < linear  # eased curve starts slower than linear


def test_segment_value_clamps_past_duration():
    assert segment_value(False, 999.0, 10.0, 20.0, 80.0) == 80.0
    assert segment_value(True, 999.0, 10.0, 20.0, 80.0) == 80.0


def test_segment_value_clamps_negative_elapsed():
    assert segment_value(False, -5.0, 10.0, 20.0, 80.0) == 20.0


def test_segment_value_zero_duration_is_instantly_done():
    assert segment_value(False, 0.0, 0.0, 20.0, 80.0) == 80.0


def test_segment_value_moves_down_towards_lower_target():
    value = segment_value(False, 5.0, 10.0, 80.0, 20.0)
    assert 20.0 < value < 80.0


# ---- profile_value_at_time: the full розгін -> утримання -> спад timeline ------------------------------------------------


def test_profile_up_phase():
    # 4s up, 2s hold, 4s down, 0..100% - halfway through the up leg
    assert profile_value_at_time(False, 2.0, 4.0, 2.0, 4.0, 0.0, 100.0) == 50.0


def test_profile_hold_phase_stays_at_max():
    assert profile_value_at_time(False, 4.0, 4.0, 2.0, 4.0, 0.0, 100.0) == 100.0
    assert profile_value_at_time(False, 5.0, 4.0, 2.0, 4.0, 0.0, 100.0) == 100.0
    assert profile_value_at_time(False, 5.999, 4.0, 2.0, 4.0, 0.0, 100.0) == 100.0


def test_profile_down_phase():
    # down phase starts at t=6 (4+2), halfway through its own 4s span is t=8
    assert profile_value_at_time(False, 8.0, 4.0, 2.0, 4.0, 0.0, 100.0) == 50.0


def test_profile_end_of_down_is_min():
    assert profile_value_at_time(False, 10.0, 4.0, 2.0, 4.0, 0.0, 100.0) == 0.0


def test_profile_zero_hold_goes_straight_from_up_to_down():
    # no plateau at all - up ends exactly where down begins
    assert profile_value_at_time(False, 4.0, 4.0, 0.0, 4.0, 0.0, 100.0) == 100.0
    assert profile_value_at_time(False, 6.0, 4.0, 0.0, 4.0, 0.0, 100.0) == 50.0


def test_profile_respects_custom_bounds():
    assert profile_value_at_time(False, 0.0, 4.0, 2.0, 4.0, 20.0, 80.0) == 20.0
    assert profile_value_at_time(False, 4.0, 4.0, 2.0, 4.0, 20.0, 80.0) == 80.0
    assert profile_value_at_time(False, 10.0, 4.0, 2.0, 4.0, 20.0, 80.0) == 20.0


# ---- clamp_bound_pair ------------------------------------------------


def test_clamp_bound_pair_in_range_no_op():
    assert clamp_bound_pair(20.0, 80.0) == (20.0, 80.0)


def test_clamp_bound_pair_clamps_to_0_100():
    assert clamp_bound_pair(-10.0, 50.0) == (0.0, 50.0)
    assert clamp_bound_pair(50.0, 150.0) == (50.0, 100.0)


def test_clamp_bound_pair_enforces_minimum_gap_pushing_max_by_default():
    min_p, max_p = clamp_bound_pair(50.0, 51.0, dragging="min")
    assert min_p == 50.0
    assert max_p == 50.0 + MIN_BOUND_GAP_PERCENT


def test_clamp_bound_pair_enforces_minimum_gap_pushing_min_when_dragging_max():
    min_p, max_p = clamp_bound_pair(50.0, 51.0, dragging="max")
    assert max_p == 51.0
    assert min_p == 51.0 - MIN_BOUND_GAP_PERCENT


def test_clamp_bound_pair_degenerate_equal_inputs():
    min_p, max_p = clamp_bound_pair(50.0, 50.0, dragging="min")
    assert max_p - min_p == MIN_BOUND_GAP_PERCENT


# ---- clamp_duration_s ------------------------------------------------


def test_clamp_duration_s_in_range_no_op():
    assert clamp_duration_s(5.0) == 5.0


def test_clamp_duration_s_clamps_too_small():
    assert clamp_duration_s(0.0) == MIN_PHASE_DURATION_S
    assert clamp_duration_s(-5.0) == MIN_PHASE_DURATION_S


def test_clamp_duration_s_clamps_too_large():
    assert clamp_duration_s(999.0) == MAX_PHASE_DURATION_S


# ---- pixel <-> percent round trip ------------------------------------------------


def test_pixel_percent_round_trip():
    for pct in (0, 25, 50, 75, 100):
        y = percent_to_pixel_y(pct, height=200, top_margin=20, bottom_margin=10)
        assert round(pixel_y_to_percent(y, height=200, top_margin=20, bottom_margin=10)) == pct


def test_percent_to_pixel_y_top_is_100_percent():
    # 0 at the TOP in pixel space (Qt convention) must correspond to 100%.
    y_top = percent_to_pixel_y(100, height=200, top_margin=20, bottom_margin=10)
    y_bottom = percent_to_pixel_y(0, height=200, top_margin=20, bottom_margin=10)
    assert y_top < y_bottom


# ---- pixel <-> time round trip ------------------------------------------------


def test_pixel_time_round_trip():
    for t in (0.0, 2.5, 5.0, 7.5, 10.0):
        x = time_to_pixel_x(t, total_duration_s=10.0, left=30, width=300)
        assert round(pixel_x_to_time(x, total_duration_s=10.0, left=30, width=300), 3) == t


def test_time_to_pixel_x_zero_total_duration_is_safe():
    assert time_to_pixel_x(5.0, total_duration_s=0.0, left=30, width=300) == 30


def test_pixel_x_to_time_zero_width_is_safe():
    assert pixel_x_to_time(100, total_duration_s=10.0, left=30, width=0) == 0.0
