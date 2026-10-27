import json

import pytest

from esc_hotline.motor_ramp import cubic_bezier_ease
from esc_hotline.ramp_curve import MIN_ANCHOR_GAP, RampCurve
from esc_hotline.ramp_chart_math import profile_value_at_time, segment_value


def test_default_curve_matches_the_old_fixed_bezier():
    c = RampCurve.default()
    for i in range(0, 101):
        t = i / 100
        assert c.ease(t) == pytest.approx(cubic_bezier_ease(t), abs=1e-9)


def test_ease_is_clamped_and_hits_the_ends():
    c = RampCurve.default()
    assert c.ease(-1) == pytest.approx(0.0) and c.ease(2) == pytest.approx(1.0)
    assert c.ease(0.0) == pytest.approx(0.0) and c.ease(1.0) == pytest.approx(1.0)


def test_json_roundtrip_and_garbage_falls_back_to_default():
    c = RampCurve.default().move_anchor(1, (0.3, 0.7))
    back = RampCurve.from_json(c.to_json())
    assert all(a == pytest.approx(b, abs=1e-5) for p, q in zip(back.pts, c.pts) for a, b in zip(p, q))
    for bad in ("", "nope", "{}", '{"v":2,"p":[]}', '{"v":1,"p":[[0,0],[1,1]]}',
                json.dumps({"v": 1, "p": [[0, 0], [0.1, 2], [0.3, 0.2], [0.5, 0.5], [0.6, 0.7], [0.9, 1], [1, 1]]}),  # y out of range
                json.dumps({"v": 1, "p": [[0, 0], [0.9, 0], [0.3, 0.2], [0.5, 0.5], [0.6, 0.7], [0.9, 1], [1, 1]]})):  # handle x outside its segment
        assert RampCurve.from_json(bad) == RampCurve.default()


def test_move_anchor_stays_valid_and_monotonic():
    c = RampCurve.default().move_anchor(1, (5.0, -3.0))  # absurd drag
    assert c.pts[3][0] <= 1 - MIN_ANCHOR_GAP and 0.0 <= c.pts[3][1] <= 1.0
    assert RampCurve.from_points(c.pts) is not None  # passes the same validity check as loaded data
    ys = [c.ease(i / 200) for i in range(201)]
    assert all(0.0 <= y <= 1.0 for y in ys)


def test_handle_drag_is_clamped_to_its_segment_and_smooth_by_default():
    c = RampCurve.default()
    d = c.move_handle(2, (0.9, 0.9))  # in-handle of the middle anchor: x must stay <= anchor x
    assert d.pts[2][0] <= d.pts[3][0] + 1e-12
    # opposite handle turned to stay collinear with the dragged one
    ax, ay = d.pts[3]
    v_in = (d.pts[2][0] - ax, d.pts[2][1] - ay)
    v_out = (d.pts[4][0] - ax, d.pts[4][1] - ay)
    assert v_in[0] * v_out[1] - v_in[1] * v_out[0] == pytest.approx(0.0, abs=1e-9) or d.pts[4][0] in (ax, d.pts[5][0])
    independent = c.move_handle(2, (0.4, 0.1), smooth=False)
    assert independent.pts[4] == c.pts[4]


def test_curve_flows_through_the_shared_segment_function():
    steep = RampCurve.default().move_anchor(1, (0.2, 0.9))
    assert segment_value(True, 2.0, 10.0, 0, 100, curve=steep) == pytest.approx(0, abs=100) and \
        segment_value(True, 2.0, 10.0, 0, 100, curve=steep) > segment_value(True, 2.0, 10.0, 0, 100)
    assert segment_value(False, 2.0, 10.0, 0, 100, curve=steep) == pytest.approx(20.0)  # linear mode ignores curves
    v = profile_value_at_time(True, 5.0, 4.0, 0.0, 4.0, 0, 100, up_curve=steep, down_curve=None)
    assert 0 <= v <= 100


# ---- per-anchor handle type (smooth 180 degrees / independent), saved with the curve -----------------------------


def test_independent_handles_move_alone_and_smooth_ones_turn_together():
    from esc_hotline.ramp_curve import INDEPENDENT, SMOOTH

    base = RampCurve.default()
    smooth = base.move_handle(2, (0.3, 0.1), smooth=base.mode_of(1) == SMOOTH)
    assert smooth.pts[4] != base.pts[4]  # the opposite handle turned with it
    indep = base.with_mode(1, INDEPENDENT)
    moved = indep.move_handle(2, (0.3, 0.1), smooth=indep.mode_of(1) == SMOOTH)
    assert moved.pts[4] == base.pts[4] and moved.pts[2] != base.pts[2]  # only the dragged handle moved
    assert moved.modes == (INDEPENDENT,)


def test_switching_back_to_smooth_realigns_the_handles():
    from esc_hotline.ramp_curve import INDEPENDENT, SMOOTH

    c = RampCurve.default().with_mode(1, INDEPENDENT).move_handle(2, (0.3, 0.1), smooth=False)
    c = c.with_mode(1, SMOOTH)
    ax, ay = c.pts[3]
    (ix, iy), (ox, oy) = c.pts[2], c.pts[4]
    assert abs((ix - ax) * (oy - ay) - (iy - ay) * (ox - ax)) < 1e-9  # in-handle, anchor, out-handle on one line


def test_handle_types_survive_json_and_old_v1_still_loads():
    from esc_hotline.ramp_curve import INDEPENDENT, SMOOTH

    c = RampCurve.default().with_mode(1, INDEPENDENT)
    assert RampCurve.from_json(c.to_json()).modes == (INDEPENDENT,)
    old = '{"v":1,"p":' + RampCurve.default().to_json().split('"p":')[1].split(',"m"')[0] + "}"
    assert RampCurve.from_json(old).mode_of(1) == SMOOTH


def test_chart_recalculate_reports_a_good_profile_and_warns_about_a_too_steep_one():
    from PyQt6.QtWidgets import QApplication

    from esc_hotline.ramp_chart_widget import RampChartWidget

    app = QApplication.instance() or QApplication([])  # noqa: F841
    chart = RampChartWidget()
    chart.set_curve_mode("bezier")
    short, full = chart.recalculate(50)
    assert short.startswith("✓") and "81 точок" in full
    chart.set_durations(0.1, 0.0, 0.1)  # the whole range in two ticks: the slew guard would slow it down
    short, full = chart.recalculate(50)
    assert short.startswith("⚠") and "Увага" in full
