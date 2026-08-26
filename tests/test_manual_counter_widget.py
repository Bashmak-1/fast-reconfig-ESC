"""Unit tests for manual_counter_widget.py's pure clamp helper - the bound
enforcement behind the operator-editable "total reworked" counter shown on
both Хотспот tabs.
"""
from __future__ import annotations

from fast_reconfig_esc.manual_counter_widget import MAX_VALUE, MIN_VALUE, clamp_counter


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
