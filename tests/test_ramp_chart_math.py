"""Tests for ramp_chart_math.py - the pure geometry/physics behind the
ramp-chart widget (no Qt dependency). The `leg_progress_fraction`
duration formulas are the most important tests here: they must stay
bit-for-bit consistent with motor_test_manager.py's own `_on_tick` math,
or the chart's live marker would visibly drift from the real motor
position - see the function's own docstring for why.
"""
from __future__ import annotations

from esc_hotline.ramp_chart_math import (
    MIN_BOUND_GAP_PERCENT,
    clamp_bound_pair,
    curve_fraction_to_percent,
    leg_progress_fraction,
    percent_to_pixel_y,
    pixel_y_to_percent,
)

# ---- curve_fraction_to_percent ------------------------------------------------


def test_curve_fraction_linear_endpoints():
    assert curve_fraction_to_percent(False, 0.0, 20.0, 80.0) == 20.0
    assert curve_fraction_to_percent(False, 1.0, 20.0, 80.0) == 80.0


def test_curve_fraction_linear_midpoint():
    assert curve_fraction_to_percent(False, 0.5, 20.0, 80.0) == 50.0


def test_curve_fraction_bezier_endpoints():
    assert curve_fraction_to_percent(True, 0.0, 20.0, 80.0) == 20.0
    assert curve_fraction_to_percent(True, 1.0, 20.0, 80.0) == 80.0


def test_curve_fraction_bezier_exact_midpoint_matches_smoothstep():
    # cubic_bezier_ease(0.5) == 0.5 exactly (see motor_ramp.py) - so the
    # eased midpoint must land exactly on the arithmetic midpoint too.
    assert curve_fraction_to_percent(True, 0.5, 0.0, 100.0) == 50.0


def test_curve_fraction_bezier_differs_from_linear_off_midpoint():
    linear = curve_fraction_to_percent(False, 0.25, 0.0, 100.0)
    bezier = curve_fraction_to_percent(True, 0.25, 0.0, 100.0)
    assert bezier < linear  # eased curve starts slower than linear


def test_curve_fraction_clamps_out_of_range_fraction():
    assert curve_fraction_to_percent(False, -1.0, 20.0, 80.0) == 20.0
    assert curve_fraction_to_percent(False, 2.0, 20.0, 80.0) == 80.0


# ---- leg_progress_fraction ------------------------------------------------


def test_leg_progress_linear_scales_with_bound_width():
    # 60-point leg (20..80) at 10%/s must take 6s, not the same time as a
    # full 0..100 leg would at the same rate.
    assert leg_progress_fraction(False, elapsed_s=3.0, rate_percent_per_s=10.0, min_percent=20.0, max_percent=80.0) == 0.5
    assert leg_progress_fraction(False, elapsed_s=6.0, rate_percent_per_s=10.0, min_percent=20.0, max_percent=80.0) == 1.0


def test_leg_progress_linear_full_range_matches_simple_rate_math():
    assert leg_progress_fraction(False, elapsed_s=5.0, rate_percent_per_s=10.0, min_percent=0.0, max_percent=100.0) == 0.5


def test_leg_progress_bezier_duration_is_always_100_over_rate():
    # Bezier duration is rate-only (100/rate), deliberately NOT scaled by
    # bound width - must match motor_test_manager._on_tick's own formula
    # bit-for-bit, even for a narrow 20..80 window.
    assert leg_progress_fraction(True, elapsed_s=10.0, rate_percent_per_s=10.0, min_percent=20.0, max_percent=80.0) == 1.0
    assert leg_progress_fraction(True, elapsed_s=5.0, rate_percent_per_s=10.0, min_percent=20.0, max_percent=80.0) == 0.5


def test_leg_progress_clamps_to_one_past_duration():
    assert leg_progress_fraction(False, elapsed_s=999.0, rate_percent_per_s=10.0, min_percent=0.0, max_percent=100.0) == 1.0
    assert leg_progress_fraction(True, elapsed_s=999.0, rate_percent_per_s=10.0, min_percent=0.0, max_percent=100.0) == 1.0


def test_leg_progress_zero_or_negative_rate_is_zero_progress():
    assert leg_progress_fraction(False, elapsed_s=5.0, rate_percent_per_s=0.0, min_percent=0.0, max_percent=100.0) == 0.0
    assert leg_progress_fraction(True, elapsed_s=5.0, rate_percent_per_s=-1.0, min_percent=0.0, max_percent=100.0) == 0.0


def test_leg_progress_negative_elapsed_clamps_to_zero():
    assert leg_progress_fraction(False, elapsed_s=-5.0, rate_percent_per_s=10.0, min_percent=0.0, max_percent=100.0) == 0.0


def test_leg_progress_zero_width_bounds_is_instantly_done():
    # degenerate: min==max, nothing to travel - must not divide by zero.
    assert leg_progress_fraction(False, elapsed_s=1.0, rate_percent_per_s=10.0, min_percent=50.0, max_percent=50.0) == 1.0


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
