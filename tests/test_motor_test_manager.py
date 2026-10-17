"""Tests for the configurable sweep min/max bounds added to
MotorTestManager (set_speed_bounds + start_sweep/stop_sweep/_on_tick
using _min_target/_max_target instead of hardcoded MSP_MIN/MSP_MAX) - the
manager-side half of the "Прогон мотором" ramp-chart feature's draggable
bound lines. No real hardware/serial transport - `_msp` is a bare stand-in
whose `.send()` never raises, same spirit as test_esc_manager.py's
FakeFourWay.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

from esc_hotline import motor_test_manager as motor_test_manager_module
from esc_hotline.motor_ramp import MSP_MAX, MSP_MIN, percent_to_msp
from esc_hotline.motor_test_manager import PHASE_DOWN, PHASE_IDLE, PHASE_UP, MotorTestManager


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _armed_manager(motor_count: int = 1) -> MotorTestManager:
    mgr = MotorTestManager()
    mgr._msp = SimpleNamespace(send=lambda *a, **k: None)
    mgr._motor_count = motor_count
    return mgr


# ---- set_speed_bounds ------------------------------------------------


def test_set_speed_bounds_stores_clamped_targets():
    mgr = _armed_manager()
    mgr.set_speed_bounds(20.0, 80.0)
    assert mgr._min_target == percent_to_msp(20.0)
    assert mgr._max_target == percent_to_msp(80.0)


def test_set_speed_bounds_clamps_out_of_range_and_gap():
    mgr = _armed_manager()
    mgr.set_speed_bounds(-10.0, 1.0)  # out of range AND too narrow a gap
    assert mgr._min_target == percent_to_msp(0.0)
    assert mgr._max_target == percent_to_msp(5.0)  # MIN_BOUND_GAP_PERCENT pushed up


def test_set_speed_bounds_ignored_mid_sweep():
    mgr = _armed_manager()
    mgr._phase = PHASE_UP
    warnings = []
    mgr.log_message.connect(lambda level, text: warnings.append((level, text)))
    mgr.set_speed_bounds(20.0, 80.0)
    assert mgr._min_target == MSP_MIN
    assert mgr._max_target == MSP_MAX
    assert any(level == "warning" for level, _text in warnings)


def test_default_bounds_reproduce_original_0_to_100_sweep():
    # Regression guard: a manager that never had set_speed_bounds() called
    # must behave exactly as before this feature existed.
    mgr = _armed_manager()
    mgr.start_sweep(10.0, 10.0)
    assert mgr._current == MSP_MIN
    assert mgr._target == MSP_MAX


# ---- start_sweep / stop_sweep / _on_tick using configured bounds ------------------------------------------------


def test_start_sweep_uses_configured_bounds():
    mgr = _armed_manager()
    mgr.set_speed_bounds(20.0, 80.0)
    mgr.start_sweep(10.0, 10.0)
    assert mgr._current == percent_to_msp(20.0)
    assert mgr._target == percent_to_msp(80.0)
    assert mgr._phase == PHASE_UP


def test_stop_sweep_targets_configured_floor_not_absolute_zero():
    mgr = _armed_manager()
    mgr.set_speed_bounds(20.0, 80.0)
    mgr._phase = PHASE_UP
    mgr._current = percent_to_msp(50.0)
    mgr.stop_sweep()
    assert mgr._phase == PHASE_DOWN
    assert mgr._target == mgr._min_target
    assert mgr._target != MSP_MIN  # the whole point: not plain zero anymore


def test_on_tick_up_to_down_transition_targets_configured_floor():
    mgr = _armed_manager()
    mgr.set_speed_bounds(20.0, 80.0)
    mgr._phase = PHASE_UP
    mgr._current = mgr._max_target  # simulate having just reached the ceiling
    mgr._target = mgr._max_target
    mgr._last_tick_time = motor_test_manager_module.time.monotonic()
    mgr._on_tick()
    assert mgr._phase == PHASE_DOWN
    assert mgr._target == mgr._min_target


def test_on_tick_down_to_idle_still_works_with_configured_floor():
    mgr = _armed_manager()
    mgr.set_speed_bounds(20.0, 80.0)
    mgr._phase = PHASE_DOWN
    mgr._current = mgr._min_target
    mgr._target = mgr._min_target
    mgr._last_tick_time = motor_test_manager_module.time.monotonic()
    mgr._on_tick()
    assert mgr._phase == PHASE_IDLE
