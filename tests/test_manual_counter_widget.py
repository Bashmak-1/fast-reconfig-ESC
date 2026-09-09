"""Unit tests for manual_counter_widget.py's pure clamp helper - the bound
enforcement behind the operator-editable "total reworked" counter shown on
both Хотспот tabs, and (with custom bounds) the "Хотспот (лінія)" tab's
poll-interval stepper.
"""
from __future__ import annotations

from esc_hotline.manual_counter_widget import MAX_VALUE, MIN_VALUE, clamp_counter


def test_in_range_value_passes_through():
    assert clamp_counter(0) == 0
    assert clamp_counter(42) == 42
    assert clamp_counter(MAX_VALUE) == MAX_VALUE


def test_negative_clamps_to_min():
    assert clamp_counter(-1) == MIN_VALUE
    assert clamp_counter(-100_000) == MIN_VALUE


def test_over_max_clamps_to_max():
    assert clamp_counter(MAX_VALUE + 1) == MAX_VALUE
    assert clamp_counter(10_000_000) == MAX_VALUE


def test_custom_bounds_clamp_low():
    assert clamp_counter(0, min_value=1000, max_value=10_000) == 1000
    assert clamp_counter(999, min_value=1000, max_value=10_000) == 1000


def test_custom_bounds_clamp_high():
    assert clamp_counter(999_999, min_value=1000, max_value=10_000) == 10_000


def test_custom_bounds_in_range_passes_through():
    assert clamp_counter(3000, min_value=1000, max_value=10_000) == 3000


def test_default_bounds_unchanged_by_custom_bounds_support():
    # Regression guard: adding min_value/max_value params must not disturb
    # the existing tally use case, which always calls clamp_counter(x) with
    # no bounds - a call site never expected to pass min_value/max_value.
    assert clamp_counter(-1) == MIN_VALUE
    assert clamp_counter(MAX_VALUE + 1) == MAX_VALUE
