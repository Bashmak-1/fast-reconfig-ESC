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


def _manual_manager(motor_count=2):
    from PyQt6.QtWidgets import QApplication

    from esc_hotline import motor_test_manager as mtm

    QApplication.instance() or QApplication([])
    sent = []

    class _Msp:
        def send(self, cmd, payload=b"", **k):
            sent.append([int.from_bytes(payload[i:i + 2], "little") for i in range(0, 2 * motor_count, 2)])

    m = mtm.MotorTestManager()
    m._msp, m._motor_count = _Msp(), motor_count
    m._read_disarmed_state = lambda: (True, "")
    return m, sent, mtm


def test_manual_drive_slews_up_and_holds_where_the_fader_is_left():
    m, sent, mtm = _manual_manager()
    m.manual_set_target(1, 1500)
    m._on_manual_tick()
    assert sent[-1] == [mtm.MSP_MIN, mtm.MSP_MIN + mtm.MANUAL_STEP]  # only motor 2, one slew step
    for _ in range(40):
        m._on_manual_tick()
    assert sent[-1] == [mtm.MSP_MIN, 1500]
    for _ in range(100):  # the fader is released and nothing else happens: the motor keeps spinning, forever
        m._on_manual_tick()
    assert sent[-1] == [mtm.MSP_MIN, 1500] and m._manual_timer.isActive()


def test_manual_drive_goes_down_along_the_curve_not_in_one_jump():
    m, sent, mtm = _manual_manager()
    m.set_ramp_mode("bezier")
    m.manual_set_target(0, 1800)
    for _ in range(60):
        m._on_manual_tick()
    assert sent[-1][0] == 1800
    m.manual_set_target(0, 1200)
    seen = []
    for _ in range(200):
        m._on_manual_tick()
        seen.append(sent[-1][0])
    steps = [a - b for a, b in zip(seen, seen[1:])]
    assert max(steps) <= mtm.MAX_SLEW_PER_S * mtm.MANUAL_TICK_MS / 1000  # never an abrupt drop
    assert seen[0] > 1700 and seen[-1] == 1200 and seen == sorted(seen, reverse=True)  # eased, ends on the target


def test_manual_motors_are_independent_and_stop_spins_all_down_to_idle():
    m, sent, mtm = _manual_manager()
    m.manual_set_target(0, 1400)
    m.manual_set_target(1, 1600)
    for _ in range(60):
        m._on_manual_tick()
    assert sent[-1] == [1400, 1600]
    m.stop_sweep()  # the Стоп button / panic key while idle
    for _ in range(400):
        m._on_manual_tick()
    assert sent[-1] == [mtm.MSP_MIN, mtm.MSP_MIN] and not m._man_value and not m._manual_timer.isActive()


def test_manual_drive_refused_on_an_armed_fc():
    m, sent, mtm = _manual_manager()
    m._read_disarmed_state = lambda: (False, "ARMED")
    m.manual_set_target(0, 1500)
    assert not m._man_value and not sent


# ---- pre-solved leg table + slew guard --------------------------------------------------------------------------


def test_leg_table_is_solved_once_and_reused():
    mgr = _armed_manager()
    mgr.set_ramp_mode("bezier")
    mgr.start_sweep(2.0, 0.0, 2.0)
    first = mgr._leg_table
    assert len(first) == 41 and first[0] == MSP_MIN and first[-1] == MSP_MAX  # 2 s / 50 ms steps + the end point
    mgr.stop_sweep()
    mgr._phase = PHASE_IDLE
    mgr._current = MSP_MIN
    mgr.start_sweep(2.0, 0.0, 2.0)
    assert mgr._leg_table is first  # same settings -> served from the cache, nothing re-solved


def test_sent_value_never_jumps_faster_than_the_slew_limit():
    from esc_hotline import motor_test_manager as m

    mgr = _armed_manager()
    mgr.start_sweep(0.1, 0.0, 0.1)  # a near-instant ramp: the table itself jumps from min to max within 2 ticks
    sent = []
    mgr._msp = SimpleNamespace(send=lambda cmd, payload, **k: sent.append(int.from_bytes(payload[:2], "little")))
    mgr._last_sent_at = m.time.monotonic()
    for _ in range(40):
        mgr._phase_start_time -= 0.05
        mgr._last_sent_at = m.time.monotonic() - 0.05
        mgr._on_tick()
    steps = [abs(b - a) for a, b in zip(sent, sent[1:])]
    assert sent and max(steps) <= round(m.MAX_SLEW_PER_S * 0.05) + 1


def test_chart_table_points_are_exactly_the_table_the_motor_is_driven_from():
    from esc_hotline.motor_ramp import msp_to_percent
    from esc_hotline.ramp_chart_widget import RampChartWidget

    mgr = _armed_manager()
    mgr.set_ramp_mode("bezier")
    mgr.start_sweep(2.0, 0.0, 2.0)
    chart = RampChartWidget()
    chart.set_curve_mode("bezier")
    chart.set_durations(2.0, 0.0, 2.0)
    up, _down = chart._table_points()
    assert [round(p, 6) for _t, p in up] == [round(msp_to_percent(v), 6) for v in mgr._leg_table]
