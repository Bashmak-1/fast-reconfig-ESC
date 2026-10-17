"""Visual ramp-curve chart for the "Прогон мотором" tab (motor_test_widget.py):
draws the selected ramp curve (linear or "Безе"), a live marker showing
where the real throttle value currently sits on it, a shaded fill showing
how far the current leg (up or down) has progressed, and two draggable
horizontal lines letting the operator narrow the automatic sweep's
min/max bounds (e.g. 20%-80% instead of always 0%-100%) - confirmed by the
operator: dragging only ever changes those bounds, never the motor live,
and only while the sweep is stopped (PHASE_IDLE) - while running, the
lines are read-only.

First hand-rolled, live-repainting QWidget in this codebase (every other
QPainter use, flag_icons.py, only ever paints a static QPixmap once for a
QIcon) - there is no charting library in requirements.txt and this
deliberately doesn't add one (PyQt6 + pyserial stays the whole dependency
list), so this is plain QPainter in an overridden paintEvent.

The live marker needs no new signal from MotorTestManager: this widget
mirrors MotorTestManager._begin_leg()'s own logic locally - every
set_phase() call that starts a new up/down leg records a fresh
(leg_start_time, leg_start_percent) baseline the same way the manager
does on its side, and every set_current_value() call (10Hz, matching the
manager's own TICK_MS=100 value_changed cadence) recomputes elapsed time
since that baseline. See ramp_chart_math.leg_progress_fraction()'s
docstring for why its duration formula must stay bit-for-bit identical to
motor_test_manager.py's own, or this marker would visibly drift from the
real motor position.
"""
from __future__ import annotations

import time
from typing import Optional

from PyQt6.QtCore import QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen
from PyQt6.QtWidgets import QWidget

from .motor_ramp import msp_to_percent
from .motor_test_manager import PHASE_DOWN, PHASE_IDLE, PHASE_UP, RAMP_BEZIER
from .ramp_chart_math import (
    clamp_bound_pair,
    curve_fraction_to_percent,
    leg_progress_fraction,
    percent_to_pixel_y,
    pixel_y_to_percent,
)
from .theme import ACCENT_AMBER, ACCENT_GREEN, BG_MAIN, BG_PANEL, BORDER, TEXT_MAIN, TEXT_MUTED

_TOP_MARGIN = 26
_BOTTOM_MARGIN = 18
_LEFT_MARGIN = 34
_RIGHT_MARGIN = 10
_HANDLE_HIT_TOLERANCE_PX = 8
_CURVE_SAMPLES = 40

_PHASE_TEXT = {PHASE_IDLE: "Очікування", PHASE_UP: "Розгін", PHASE_DOWN: "Спад"}


class RampChartWidget(QWidget):
    # min_percent, max_percent - emitted exactly once, on mouse release,
    # never on every pixel of mouse movement (that would spam settings
    # saves and a *_requested signal to the manager on every drag frame).
    bounds_changed = pyqtSignal(float, float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(160)
        self._use_bezier = False
        self._min_percent = 0.0
        self._max_percent = 100.0
        self._phase = PHASE_IDLE
        self._interactive = True
        self._up_rate = 10.0
        self._down_rate = 10.0
        self._live_percent = 0.0
        self._marker_fraction = 0.0
        self._leg_start_time: Optional[float] = None
        self._leg_start_percent = 0.0
        self._dragging: Optional[str] = None  # "min" / "max" / None

    # ---- inward setters - never emit bounds_changed ------------------------------------------------

    def set_curve_mode(self, mode: str) -> None:
        self._use_bezier = mode == RAMP_BEZIER
        self.update()

    def set_rates(self, up_percent_s: float, down_percent_s: float) -> None:
        self._up_rate = up_percent_s
        self._down_rate = down_percent_s

    def set_bounds(self, min_percent: float, max_percent: float) -> None:
        """Programmatic update (e.g. from _restore_settings()) - clamps
        the same way a drag would, but deliberately does NOT emit
        bounds_changed, so restoring saved settings can never trigger a
        save-loop back into settings_store."""
        self._min_percent, self._max_percent = clamp_bound_pair(min_percent, max_percent)
        self.update()

    def set_phase(self, phase: str) -> None:
        starting_new_leg = phase in (PHASE_UP, PHASE_DOWN) and phase != self._phase
        self._phase = phase
        self._interactive = phase == PHASE_IDLE
        if self._interactive:
            self._dragging = None  # defensive: a sweep starting mid-drag (shouldn't happen) must not leave a stuck drag
        if starting_new_leg:
            self._leg_start_time = time.monotonic()
            self._leg_start_percent = self._live_percent
            self._marker_fraction = 0.0
        self.update()

    def set_current_value(self, msp: int) -> None:
        self._live_percent = msp_to_percent(msp)
        if self._phase != PHASE_IDLE and self._leg_start_time is not None:
            elapsed = time.monotonic() - self._leg_start_time
            rate = self._up_rate if self._phase == PHASE_UP else self._down_rate
            self._marker_fraction = leg_progress_fraction(
                self._use_bezier, elapsed, rate, self._min_percent, self._max_percent
            )
        else:
            self._marker_fraction = 0.0
        self.update()

    # ---- mouse: drag the min/max bound lines, idle-only ------------------------------------------------

    def _plot_rect(self) -> tuple[int, int, int]:
        """(left, right, usable_width) of the plottable area in pixels."""
        left = _LEFT_MARGIN
        right = self.width() - _RIGHT_MARGIN
        return left, right, max(1, right - left)

    def _bound_pixel_y(self, which: str) -> float:
        percent = self._min_percent if which == "min" else self._max_percent
        return percent_to_pixel_y(percent, self.height(), _TOP_MARGIN, _BOTTOM_MARGIN)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self._interactive:
            return
        y = event.position().y()
        dist_min = abs(y - self._bound_pixel_y("min"))
        dist_max = abs(y - self._bound_pixel_y("max"))
        if min(dist_min, dist_max) > _HANDLE_HIT_TOLERANCE_PX:
            self._dragging = None
            return
        self._dragging = "min" if dist_min <= dist_max else "max"

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging is None:
            return
        percent = pixel_y_to_percent(event.position().y(), self.height(), _TOP_MARGIN, _BOTTOM_MARGIN)
        if self._dragging == "min":
            self._min_percent, self._max_percent = clamp_bound_pair(percent, self._max_percent, dragging="min")
        else:
            self._min_percent, self._max_percent = clamp_bound_pair(self._min_percent, percent, dragging="max")
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._dragging is None:
            return
        self._dragging = None
        self.bounds_changed.emit(self._min_percent, self._max_percent)

    # ---- painting ------------------------------------------------

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()

        painter.setPen(QPen(QColor(BORDER), 1))
        painter.setBrush(QColor(BG_PANEL))
        painter.drawRoundedRect(0, 0, w - 1, h - 1, 6, 6)

        left, right, plot_width = self._plot_rect()

        phase_color = ACCENT_AMBER if self._phase != PHASE_IDLE else TEXT_MUTED
        painter.setPen(QColor(phase_color))
        painter.drawText(8, 18, _PHASE_TEXT.get(self._phase, self._phase))

        painter.setPen(QPen(QColor(TEXT_MUTED), 1, Qt.PenStyle.DotLine))
        for pct in (0, 25, 50, 75, 100):
            y = percent_to_pixel_y(pct, h, _TOP_MARGIN, _BOTTOM_MARGIN)
            painter.drawLine(left, round(y), right, round(y))
            painter.drawText(2, round(y) + 4, f"{pct}")

        # progress fill, up to the live marker's position along the leg
        if self._phase != PHASE_IDLE and self._marker_fraction > 0.0:
            fill = QPainterPath()
            floor_y = percent_to_pixel_y(self._min_percent, h, _TOP_MARGIN, _BOTTOM_MARGIN)
            fill.moveTo(left, floor_y)
            steps = max(2, round(_CURVE_SAMPLES * self._marker_fraction))
            for i in range(steps + 1):
                t = min(self._marker_fraction, i / steps)
                pct = curve_fraction_to_percent(self._use_bezier, t, self._min_percent, self._max_percent)
                x = left + t * plot_width
                y = percent_to_pixel_y(pct, h, _TOP_MARGIN, _BOTTOM_MARGIN)
                fill.lineTo(x, y)
            fill.lineTo(left + self._marker_fraction * plot_width, floor_y)
            fill.closeSubpath()
            painter.setPen(Qt.PenStyle.NoPen)
            fill_color = QColor(phase_color)
            fill_color.setAlpha(50)
            painter.setBrush(fill_color)
            painter.drawPath(fill)

        # the ramp curve itself
        curve = QPainterPath()
        for i in range(_CURVE_SAMPLES + 1):
            t = i / _CURVE_SAMPLES
            pct = curve_fraction_to_percent(self._use_bezier, t, self._min_percent, self._max_percent)
            x = left + t * plot_width
            y = percent_to_pixel_y(pct, h, _TOP_MARGIN, _BOTTOM_MARGIN)
            if i == 0:
                curve.moveTo(x, y)
            else:
                curve.lineTo(x, y)
        painter.setPen(QPen(QColor(TEXT_MAIN), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(curve)

        # min/max bound handle lines
        handle_color = QColor(ACCENT_GREEN if self._interactive else TEXT_MUTED)
        for which in ("min", "max"):
            y = self._bound_pixel_y(which)
            pen = QPen(handle_color, 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(left, round(y), right, round(y))
            if self._interactive:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(handle_color)
                painter.drawRect(left - 4, round(y) - 4, 8, 8)

        # live marker - only while an actual leg is in motion
        if self._phase != PHASE_IDLE:
            mx = left + self._marker_fraction * plot_width
            my = percent_to_pixel_y(self._live_percent, h, _TOP_MARGIN, _BOTTOM_MARGIN)
            painter.setPen(QPen(QColor(BG_MAIN), 2))
            painter.setBrush(QColor(ACCENT_AMBER))
            painter.drawEllipse(QPointF(mx, my), 6, 6)
