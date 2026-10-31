from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from esc_hotline.ramp_curve import INDEPENDENT, SMOOTH, RampCurve


_APP = None


def _chart():
    global _APP
    from esc_hotline.motor_test_manager import RAMP_BEZIER
    from esc_hotline.ramp_chart_widget import RampChartWidget

    _APP = QApplication.instance() or QApplication([])  # kept alive: a collected QApplication kills the interpreter
    w = RampChartWidget()
    w.resize(800, 300)
    w.set_curve_mode(RAMP_BEZIER)
    w.show()
    changed = []
    w.curves_changed.connect(lambda u, d: changed.append((u, d)))
    return w, changed


def test_right_click_menu_actions_add_change_and_delete_points():
    w, changed = _chart()
    up = w._up_curve
    assert up.segments == 2  # the default curve: one interior anchor per leg

    spot = QPointF(w._point_pixel("A").x() * 0.3 + 40, 100)  # an empty place over the ramp-up leg
    assert w.add_anchor_at(spot) and w._up_curve.segments == 3 and w._selected == ("U", 1)
    assert changed, "curves_changed must fire so the motor and the settings get the new curve"

    down_spot = QPointF((w._point_pixel("B").x() + w._point_pixel("C").x()) / 2 + 30, 120)
    assert w.add_anchor_at(down_spot) and w._down_curve.segments == 3

    w.set_anchor_mode("U", 1, INDEPENDENT)
    assert w._up_curve.mode_of(1) == INDEPENDENT
    w.set_anchor_mode("U", 1, SMOOTH)
    assert w._up_curve.mode_of(1) == SMOOTH

    for _ in range(3):  # delete every interior point of both legs: the minimum is A, B, C (+ the start)
        w.remove_anchor("U", 1) or w.remove_anchor("D", 1)
    w.remove_anchor("D", 1)
    w.remove_anchor("D", 1)
    assert w._up_curve.segments == 1 and w._down_curve.segments == 1
    assert w.remove_anchor("U", 1) is False  # nothing interior left


def test_a_click_on_the_plateau_adds_nothing_and_points_a_b_c_cannot_be_deleted():
    w, _ = _chart()
    w.set_durations(4.0, 3.0, 4.0)
    a, b = w._point_pixel("A"), w._point_pixel("B")
    assert w.add_anchor_at(QPointF((a.x() + b.x()) / 2, 100)) is False  # between A and B is the hold plateau
    assert w._hit_test(a) == ("A",) and w._hit_test(b) == ("B",)
    assert w.remove_anchor("U", 0) is False and w.remove_anchor("D", 5) is False


def test_dragging_an_added_point_with_the_mouse_moves_it_and_reports_once():
    w, changed = _chart()
    w.add_anchor_at(QPointF(w._point_pixel("A").x() * 0.3 + 40, 100))
    target = w._to_pixel("U", w._up_curve.pts[3])
    QTest.mousePress(w, Qt.MouseButton.LeftButton, pos=QPoint(round(target.x()), round(target.y())))
    assert w._dragging == ("a", "U", 1)
    before = changed.copy()
    QTest.mouseMove(w, QPoint(round(target.x()) + 25, round(target.y()) - 20))
    moved = w._up_curve.pts[3]
    QTest.mouseRelease(w, Qt.MouseButton.LeftButton, pos=QPoint(round(target.x()) + 25, round(target.y()) - 20))
    assert moved != RampCurve.default().pts[3] and len(changed) == len(before) + 1
