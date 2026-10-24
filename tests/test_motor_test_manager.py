"""Tests for MotorTestManager: configurable sweep min/max bounds
(set_speed_bounds), the three-phase розгін->утримання->спад timeline
(start_sweep/stop_sweep/_on_tick using duration-in-seconds, not %/s
rates), and TEST_MODE_SEQUENTIAL (one motor at a time, advancing
automatically through all of them once then stopping) - added because a
bench lab power supply often can't source enough current for two motors
at once. No real hardware/serial transport - `_msp` is a bare stand-in
whose `.send()` never raises, same spirit as test_esc_manager.py's
FakeFourWay.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

from esc_hotline.motor_ramp import MSP_MAX, MSP_MIN, percent_to_msp
from esc_hotline.motor_test_manager import (
    PHASE_DOWN,
    PHASE_HOLD,
    PHASE_IDLE,
    PHASE_UP,
    TEST_MODE_ALL,
    TEST_MODE_SEQUENTIAL,
    MotorTestManager,
)


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
    mgr.start_sweep(4.0, 0.0, 4.0)
    assert mgr._current == MSP_MIN
    assert mgr._target == MSP_MAX


# ---- start_sweep / stop_sweep / _on_tick using configured bounds ------------------------------------------------


def test_start_sweep_uses_configured_bounds():
    mgr = _armed_manager()
    mgr.set_speed_bounds(20.0, 80.0)
    mgr.start_sweep(4.0, 0.0, 4.0)
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


def test_on_tick_up_to_down_transition_with_no_hold():
    mgr = _armed_manager()
    mgr.set_speed_bounds(20.0, 80.0)
    mgr.start_sweep(4.0, 0.0, 4.0)  # hold=0 -> straight to DOWN, no plateau
    mgr._phase_start_time -= 999.0  # simulate having just reached the ceiling (elapsed >> up_duration_s)
    mgr._on_tick()
    assert mgr._phase == PHASE_DOWN
    assert mgr._target == mgr._min_target


def test_on_tick_down_to_idle_still_works_with_configured_floor():
    mgr = _armed_manager()
    mgr.set_speed_bounds(20.0, 80.0)
    mgr.start_sweep(4.0, 0.0, 4.0)
    mgr._phase = PHASE_DOWN
    mgr._current = mgr._min_target
    mgr._target = mgr._min_target
    mgr._on_tick()
    assert mgr._phase == PHASE_IDLE


# ---- hold phase ------------------------------------------------


def test_up_phase_enters_hold_when_hold_duration_positive():
    mgr = _armed_manager()
    mgr.start_sweep(4.0, 2.0, 4.0)
    mgr._phase_start_time -= 999.0  # simulate having just reached the ceiling
    mgr._on_tick()
    assert mgr._phase == PHASE_HOLD


def test_hold_phase_holds_at_ceiling_until_duration_elapses():
    mgr = _armed_manager()
    mgr.start_sweep(4.0, 2.0, 4.0)
    mgr._phase = PHASE_HOLD
    mgr._phase_start_time -= 1.0  # pretend 1s of the 2s hold has already elapsed
    mgr._on_tick()
    assert mgr._phase == PHASE_HOLD
    assert mgr._current == mgr._max_target


def test_hold_phase_moves_to_down_once_duration_elapses():
    mgr = _armed_manager()
    mgr.start_sweep(4.0, 2.0, 4.0)
    mgr._phase = PHASE_HOLD
    mgr._phase_start_time -= 3.0  # pretend the whole 2s hold has already elapsed
    mgr._on_tick()
    assert mgr._phase == PHASE_DOWN
    assert mgr._target == mgr._min_target


def test_zero_hold_duration_is_clamped_to_zero_not_min_phase_duration():
    # hold=0 must mean "no plateau at all", never floored up like up/down durations are.
    mgr = _armed_manager()
    mgr.start_sweep(4.0, 0.0, 4.0)
    assert mgr._hold_duration_s == 0.0


# ---- set_test_mode / TEST_MODE_SEQUENTIAL ------------------------------------------------


def test_set_test_mode_defaults_to_all():
    mgr = _armed_manager()
    assert mgr._test_mode == TEST_MODE_ALL


def test_set_test_mode_invalid_value_falls_back_to_all():
    mgr = _armed_manager()
    mgr.set_test_mode("bogus")
    assert mgr._test_mode == TEST_MODE_ALL


def test_sequential_start_sweep_targets_only_motor_zero():
    mgr = _armed_manager(motor_count=3)
    mgr.set_test_mode(TEST_MODE_SEQUENTIAL)
    sent = []
    mgr.value_changed.connect(lambda values: sent.append(values))
    mgr.start_sweep(4.0, 0.0, 4.0)
    assert mgr._active_motor_index == 0
    mgr._phase_start_time -= 2.0  # partway through the 4s up-ramp, so motor 0's value has moved off MSP_MIN
    mgr._on_tick()
    assert sent[-1][0] != MSP_MIN  # motor 0 is moving
    assert sent[-1][1] == MSP_MIN and sent[-1][2] == MSP_MIN  # the other two stay stopped


def test_sequential_advances_to_next_motor_after_down_leg_completes():
    mgr = _armed_manager(motor_count=2)
    mgr.set_test_mode(TEST_MODE_SEQUENTIAL)
    mgr.start_sweep(4.0, 0.0, 4.0)
    mgr._phase = PHASE_DOWN
    mgr._current = mgr._min_target
    mgr._target = mgr._min_target
    mgr._on_tick()
    assert mgr._active_motor_index == 1
    assert mgr._phase == PHASE_UP


def test_sequential_stops_after_last_motor():
    mgr = _armed_manager(motor_count=2)
    mgr.set_test_mode(TEST_MODE_SEQUENTIAL)
    mgr.start_sweep(4.0, 0.0, 4.0)
    mgr._active_motor_index = 1  # already on the last motor
    mgr._phase = PHASE_DOWN
    mgr._current = mgr._min_target
    mgr._target = mgr._min_target
    mgr._on_tick()
    assert mgr._phase == PHASE_IDLE
    assert mgr._active_motor_index is None


def test_manual_stop_during_sequential_does_not_advance():
    mgr = _armed_manager(motor_count=3)
    mgr.set_test_mode(TEST_MODE_SEQUENTIAL)
    mgr.start_sweep(4.0, 0.0, 4.0)
    mgr.stop_sweep()  # manual stop while on motor 0 of 3
    mgr._phase = PHASE_DOWN
    mgr._current = mgr._min_target
    mgr._target = mgr._min_target
    mgr._on_tick()
    assert mgr._phase == PHASE_IDLE  # stopped entirely, did NOT advance to motor 1


def test_all_mode_moves_every_motor_together():
    mgr = _armed_manager(motor_count=3)
    sent = []
    mgr.value_changed.connect(lambda values: sent.append(values))
    mgr.start_sweep(4.0, 0.0, 4.0)
    mgr._on_tick()
    assert len(set(sent[-1])) == 1  # every motor got the same value


def test_manual_drive_slews_one_motor_and_spins_down():
    from PyQt6.QtWidgets import QApplication

    from esc_hotline import motor_test_manager as mtm

    app = QApplication.instance() or QApplication([])  # noqa: F841
    sent = []

    class _Msp:
        def send(self, cmd, payload=b"", **k):
            sent.append(payload)

    m = mtm.MotorTestManager()
    m._msp, m._motor_count = _Msp(), 2
    m._read_disarmed_state = lambda: (True, "")
    m.manual_set_target(1, 1500)
    m._on_manual_tick()
    first = [int.from_bytes(sent[-1][i:i + 2], "little") for i in (0, 2)]
    assert first == [mtm.MSP_MIN, mtm.MSP_MIN + mtm.MANUAL_STEP]  # only motor 2, one slew step
    m.manual_set_target(1, mtm.MSP_MIN)  # slider released
    for _ in range(5):
        m._on_manual_tick()
    assert m._manual_index is None  # spun down gradually, then idle
    m.manual_set_target(0, 1500)
    m._read_disarmed_state = lambda: (False, "ARMED")
    m._manual_index = None
    m.manual_set_target(0, 1500)
    assert m._manual_index is None  # armed FC: refused
