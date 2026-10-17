"""Visual ramp-profile chart for the "Прогон мотором" tab
(motor_test_widget.py): draws the full розгін (up) -> утримання (hold) ->
спад (down) timeline (linear or "Безе"), a live marker showing where the
real throttle value currently sits on it, a shaded fill showing how far
the current cycle has progressed, and three draggable points letting the
operator reshape the whole profile directly on the chart - matching a
reference app the operator pointed to (AeroBench): dragging a point
RIGHT/LEFT changes its TIME (phase duration), dragging it UP/DOWN changes
its throttle VALUE (the ceiling or floor) - confirmed by the operator.
Dragging is only possible while the sweep is stopped (PHASE_IDLE); while
running, the points are read-only.

Point A = end of ramp-up (time=up_duration_s, value=max_percent) - drag
  changes up_duration_s (X) and max_percent (Y, the ceiling).
Point B = end of hold (time=up_duration_s+hold_duration_s, value=max_percent)
  - drag only changes hold_duration_s (X); Y stays locked to point A's
  height (the plateau is always flat).
Point C = end of ramp-down (time=total_duration_s, value=min_percent) -
  drag changes down_duration_s (X) and min_percent (Y, the floor).

First hand-rolled, live-repainting QWidget in this codebase (every other
QPainter use, flag_icons.py, only ever paints a static QPixmap once for a
QIcon) - there is no charting library in requirements.txt and this
deliberately doesn't add one (PyQt6 + pyserial stays the whole dependency
list), so this is plain QPainter in an overridden paintEvent.

The live marker needs no new signal from MotorTestManager: this widget
mirrors MotorTestManager._begin_leg()'s own logic locally - every
set_phase() call that starts a NEW cycle (phase becomes PHASE_UP from
something else - the very first start, or TEST_MODE_SEQUENTIAL advancing
to the next motor) records a fresh cycle_start_time baseline, and every
set_current_value() call (10Hz, matching the manager's own TICK_MS=100
value_changed cadence) recomputes elapsed time since that baseline. See
ramp_chart_math.profile_value_at_time()'s docstring for why its math must
stay bit-for-bit identical to motor_test_manager.py's own, or this marker
would visibly drift from the real motor position.
"""
from __future__ import annotations

import time
from typing import Optional

from PyQt6.QtCore import QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen
from PyQt6.QtWidgets import QWidget

from .motor_ramp import msp_to_percent
from .motor_test_manager import PHASE_DOWN, PHASE_HOLD, PHASE_IDLE, PHASE_UP, RAMP_BEZIER
from .ramp_chart_math import (
    MAX_PHASE_DURATION_S,
    clamp_bound_pair,
    clamp_duration_s,
    percent_to_pixel_y,
    pixel_x_to_time,
    pixel_y_to_percent,
    profile_value_at_time,
    time_to_pixel_x,
)
from .theme import ACCENT_AMBER, ACCENT_GREEN, BG_MAIN, BG_PANEL, BORDER, TEXT_MAIN, TEXT_MUTED

_TOP_MARGIN = 40
_BOTTOM_MARGIN = 18
_LEFT_MARGIN = 34
_RIGHT_MARGIN = 14
_HANDLE_HIT_TOLERANCE_PX = 10
_CURVE_SAMPLES = 60

_PHASE_TEXT = {PHASE_IDLE: "Очікування", PHASE_UP: "Розгін", PHASE_HOLD: "Утримання", PHASE_DOWN: "Спад"}


class RampChartWidget(QWidget):
    # up_duration_s, hold_duration_s, down_duration_s, min_percent,
    # max_percent - emitted exactly once, on mouse release, never on every
    # pixel of mouse movement (that would spam settings saves and a
    # *_requested signal to the manager on every drag frame).
    profile_changed = pyqtSignal(float, float, float, float, float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(190)
        self._use_bezier = False
        self._up_duration_s = 4.0
        self._hold_duration_s = 0.0
        self._down_duration_s = 4.0
        self._min_percent = 0.0
        self._max_percent = 100.0
        self._phase = PHASE_IDLE
        self._interactive = True
        self._live_percent = 0.0
        self._elapsed_s = 0.0
        self._cycle_start_time: Optional[float] = None
        self._dragging: Optional[str] = None  # "A" / "B" / "C" / None

    # ---- inward setters - never emit profile_changed ------------------------------------------------

    def set_curve_mode(self, mode: str) -> None:
        self._use_bezier = mode == RAMP_BEZIER
        self.update()

    def set_durations(self, up_s: float, hold_s: float, down_s: float) -> None:
        self._up_duration_s = clamp_duration_s(up_s)
        self._hold_duration_s = max(0.0, hold_s)  # 0 is valid - no plateau, not floored like up/down
        self._down_duration_s = clamp_duration_s(down_s)
        self.update()

    def set_bounds(self, min_percent: float, max_percent: float) -> None:
        """Programmatic update (e.g. from _restore_settings()) - clamps
        the same way a drag would, but deliberately does NOT emit
        profile_changed, so restoring saved settings can never trigger a
        save-loop back into settings_store."""
        self._min_percent, self._max_percent = clamp_bound_pair(min_percent, max_percent)
        self.update()

    def set_phase(self, phase: str) -> None:
        starting_new_cycle = phase == PHASE_UP and phase != self._phase
        self._phase = phase
        self._interactive = phase == PHASE_IDLE
        if self._interactive:
            self._dragging = None  # defensive: a sweep starting mid-drag (shouldn't happen) must not leave a stuck drag
        if starting_new_cycle:
            self._cycle_start_time = time.monotonic()
            self._elapsed_s = 0.0
        self.update()

    def set_current_value(self, msp: int) -> None:
        self._live_percent = msp_to_percent(msp)
        if self._phase != PHASE_IDLE and self._cycle_start_time is not None:
            self._elapsed_s = time.monotonic() - self._cycle_start_time
        else:
            self._elapsed_s = 0.0
        self.update()

    # ---- geometry helpers ------------------------------------------------

    def _total_duration_s(self) -> float:
        return self._up_duration_s + self._hold_duration_s + self._down_duration_s

    def _plot_rect(self) -> tuple[int, int, int]:
        """(left, right, usable_width) of the plottable area in pixels."""
        left = _LEFT_MARGIN
        right = self.width() - _RIGHT_MARGIN
        return left, right, max(1, right - left)

    def _point_pixel(self, which: str) -> QPointF:
        left, _right, width = self._plot_rect()
        total = self._total_duration_s()
        h = self.height()
        if which == "A":
            t, pct = self._up_duration_s, self._max_percent
        elif which == "B":
            t, pct = self._up_duration_s + self._hold_duration_s, self._max_percent
        else:  # "C"
            t, pct = total, self._min_percent
        x = time_to_pixel_x(t, total, left, width)
        y = percent_to_pixel_y(pct, h, _TOP_MARGIN, _BOTTOM_MARGIN)
        return QPointF(x, y)

    # ---- mouse: drag points A/B/C, idle-only ------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self._interactive:
            return
        pos = event.position()
        best, best_dist = None, _HANDLE_HIT_TOLERANCE_PX + 1.0
        for which in ("A", "B", "C"):
            p = self._point_pixel(which)
            dist = ((pos.x() - p.x()) ** 2 + (pos.y() - p.y()) ** 2) ** 0.5
            if dist < best_dist:
                best, best_dist = which, dist
        self._dragging = best

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging is None:
            return
        left, _right, width = self._plot_rect()
        total = self._total_duration_s()
        t = pixel_x_to_time(event.position().x(), total, left, width)
        pct = pixel_y_to_percent(event.position().y(), self.height(), _TOP_MARGIN, _BOTTOM_MARGIN)

        if self._dragging == "A":
            self._up_duration_s = clamp_duration_s(t)
            self._min_percent, self._max_percent = clamp_bound_pair(self._min_percent, pct, dragging="max")
        elif self._dragging == "B":
            self._hold_duration_s = max(0.0, min(MAX_PHASE_DURATION_S, t - self._up_duration_s))
        else:  # "C"
            self._down_duration_s = clamp_duration_s(t - self._up_duration_s - self._hold_duration_s)
            self._min_percent, self._max_percent = clamp_bound_pair(pct, self._max_percent, dragging="min")
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._dragging is None:
            return
        self._dragging = None
        self.profile_changed.emit(
            self._up_duration_s, self._hold_duration_s, self._down_duration_s, self._min_percent, self._max_percent
        )

    # ---- painting ------------------------------------------------

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()

        painter.setPen(QPen(QColor(BORDER), 1))
        painter.setBrush(QColor(BG_PANEL))
        painter.drawRoundedRect(0, 0, w - 1, h - 1, 6, 6)

        left, right, plot_width = self._plot_rect()
        total = self._total_duration_s()

        phase_color = ACCENT_AMBER if self._phase != PHASE_IDLE else TEXT_MUTED
        painter.setPen(QColor(phase_color))
        painter.drawText(8, 16, _PHASE_TEXT.get(self._phase, self._phase))
        painter.setPen(QColor(TEXT_MUTED))
        summary = (
            f"Розгін {self._up_duration_s:.1f}с · Утримання {self._hold_duration_s:.1f}с · "
            f"Спад {self._down_duration_s:.1f}с · Межі {self._min_percent:.0f}–{self._max_percent:.0f}%"
        )
        painter.drawText(8, 32, summary)

        painter.setPen(QPen(QColor(TEXT_MUTED), 1, Qt.PenStyle.DotLine))
        for pct in (0, 25, 50, 75, 100):
            y = percent_to_pixel_y(pct, h, _TOP_MARGIN, _BOTTOM_MARGIN)
            painter.drawLine(left, round(y), right, round(y))
            painter.drawText(2, round(y) + 4, f"{pct}")

        # progress fill, up to the live marker's elapsed time
        clamped_elapsed = max(0.0, min(total, self._elapsed_s))
        if self._phase != PHASE_IDLE and clamped_elapsed > 0.0:
            fill = QPainterPath()
            floor_y = percent_to_pixel_y(self._min_percent, h, _TOP_MARGIN, _BOTTOM_MARGIN)
            fill.moveTo(left, floor_y)
            steps = max(2, round(_CURVE_SAMPLES * clamped_elapsed / total)) if total > 0 else 2
            for i in range(steps + 1):
                t = min(clamped_elapsed, (i / steps) * total)
                pct = profile_value_at_time(
                    self._use_bezier, t, self._up_duration_s, self._hold_duration_s, self._down_duration_s,
                    self._min_percent, self._max_percent,
                )
                x = time_to_pixel_x(t, total, left, plot_width)
                y = percent_to_pixel_y(pct, h, _TOP_MARGIN, _BOTTOM_MARGIN)
                fill.lineTo(x, y)
            fill.lineTo(time_to_pixel_x(clamped_elapsed, total, left, plot_width), floor_y)
            fill.closeSubpath()
            painter.setPen(Qt.PenStyle.NoPen)
            fill_color = QColor(phase_color)
            fill_color.setAlpha(50)
            painter.setBrush(fill_color)
            painter.drawPath(fill)

        # the full up -> hold -> down profile curve
        curve = QPainterPath()
        for i in range(_CURVE_SAMPLES + 1):
            t = (i / _CURVE_SAMPLES) * total
            pct = profile_value_at_time(
                self._use_bezier, t, self._up_duration_s, self._hold_duration_s, self._down_duration_s,
                self._min_percent, self._max_percent,
            )
            x = time_to_pixel_x(t, total, left, plot_width)
            y = percent_to_pixel_y(pct, h, _TOP_MARGIN, _BOTTOM_MARGIN)
            if i == 0:
                curve.moveTo(x, y)
            else:
                curve.lineTo(x, y)
        painter.setPen(QPen(QColor(TEXT_MAIN), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(curve)

        # draggable points A (end of up), B (end of hold), C (end of down)
        point_color = QColor(ACCENT_GREEN if self._interactive else TEXT_MUTED)
        for which, label in (("A", f"{self._up_duration_s:.1f}с"), ("B", f"{self._up_duration_s + self._hold_duration_s:.1f}с"), ("C", f"{total:.1f}с")):
            p = self._point_pixel(which)
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(QPointF(p.x() - 12, p.y() - 10), label)
            painter.setPen(QPen(QColor(BG_MAIN), 2))
            painter.setBrush(point_color)
            painter.drawEllipse(p, 5, 5)

        # live marker - only while an actual cycle is in motion
        if self._phase != PHASE_IDLE:
            mx = time_to_pixel_x(clamped_elapsed, total, left, plot_width)
            my = percent_to_pixel_y(self._live_percent, h, _TOP_MARGIN, _BOTTOM_MARGIN)
            painter.setPen(QPen(QColor(BG_MAIN), 2))
            painter.setBrush(QColor(ACCENT_AMBER))
            painter.drawEllipse(QPointF(mx, my), 6, 6)
