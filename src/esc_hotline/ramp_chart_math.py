"""Pure (no Qt) math behind the ramp-chart widget (ramp_chart_widget.py):
mapping the ramp curve's shape and the current leg's live progress onto
drawable (0..1 fraction, percent) coordinates, plus clamping the two
operator-draggable sweep bounds (min/max throttle %). Kept separate from
motor_ramp.py (which stays scoped to "how MSP values step/ease from A to
B" for the actual motor-control path) so this file is strictly chart
geometry - unit-testable without a QApplication, same spirit as
motor_ramp.py itself.
"""
from __future__ import annotations

from .motor_ramp import cubic_bezier_ease

MIN_BOUND_GAP_PERCENT = 5.0


def curve_fraction_to_percent(use_bezier: bool, fraction: float, min_percent: float, max_percent: float) -> float:
    """fraction 0..1 = how far through the CURRENT leg (0=leg start,
    1=leg end) -> throttle percent between min_percent and max_percent,
    shaped by the curve - a straight line, or the exact same
    cubic_bezier_ease() the real ramp motion uses for "Безе", so the
    drawn curve and the real motor motion are provably the same
    function, never a look-alike copy."""
    clamped = max(0.0, min(1.0, fraction))
    eased = cubic_bezier_ease(clamped) if use_bezier else clamped
    return min_percent + (max_percent - min_percent) * eased


def leg_progress_fraction(
    use_bezier: bool, elapsed_s: float, rate_percent_per_s: float, min_percent: float, max_percent: float
) -> float:
    """0..1 progress through the current ramp leg, given elapsed time
    since it began. Must mirror motor_test_manager.py's own duration math
    exactly, or the chart's live marker visibly drifts from the real
    motor position:
    - Bezier: duration_s = 100.0 / rate - motor_test_manager._on_tick's
      own formula, a fixed duration based on rate alone, NOT the leg's
      actual width. Kept bit-for-bit identical here on purpose, even
      though it means a narrow-bounds Bezier leg takes the same wall-clock
      time as a full-range one would - that's the manager's real,
      existing behaviour, and this chart must stay truthful to it rather
      than "fixing" it independently.
    - Linear: next_value() moves at a constant rate_percent_per_s
      (percent-of-the-full-0..100-scale per second), so a leg spanning
      abs(max_percent - min_percent) percentage points takes exactly that
      distance divided by the rate - this one DOES scale with the actual
      leg width, matching next_value()'s real continuous-time behaviour.
    """
    elapsed = max(0.0, elapsed_s)
    if rate_percent_per_s <= 0:
        return 0.0
    if use_bezier:
        duration_s = 100.0 / rate_percent_per_s
    else:
        duration_s = abs(max_percent - min_percent) / rate_percent_per_s
    if duration_s <= 0:
        return 1.0
    return max(0.0, min(1.0, elapsed / duration_s))


def clamp_bound_pair(min_percent: float, max_percent: float, dragging: str = "min") -> tuple[float, float]:
    """Clamps both to [0,100] and keeps max_percent - min_percent >=
    MIN_BOUND_GAP_PERCENT, pushing whichever bound is NOT currently being
    dragged (`dragging`: "min" or "max") out of the way instead of
    snapping the dragged one back to its old spot - keeps a drag gesture
    tracking the mouse smoothly even once it hits the gap limit."""
    min_p = max(0.0, min(100.0, min_percent))
    max_p = max(0.0, min(100.0, max_percent))
    if max_p - min_p < MIN_BOUND_GAP_PERCENT:
        if dragging == "max":
            min_p = max(0.0, max_p - MIN_BOUND_GAP_PERCENT)
        else:
            max_p = min(100.0, min_p + MIN_BOUND_GAP_PERCENT)
    return min_p, max_p


def percent_to_pixel_y(percent: float, height: float, top_margin: float = 0.0, bottom_margin: float = 0.0) -> float:
    """Percent (0..100, 0=bottom/100=top - the usual chart convention) ->
    pixel Y within a widget of the given `height` (0 at the TOP, growing
    downward - Qt's own pixel convention), reserving `top_margin`/
    `bottom_margin` pixels for any header/axis-label band."""
    usable = max(1.0, height - top_margin - bottom_margin)
    clamped = max(0.0, min(100.0, percent))
    frac_from_top = 1.0 - clamped / 100.0
    return top_margin + frac_from_top * usable


def pixel_y_to_percent(y: float, height: float, top_margin: float = 0.0, bottom_margin: float = 0.0) -> float:
    """Inverse of percent_to_pixel_y() above - used by the widget's drag
    handling to turn a mouse Y position back into a percent."""
    usable = max(1.0, height - top_margin - bottom_margin)
    frac_from_top = max(0.0, min(1.0, (y - top_margin) / usable))
    return (1.0 - frac_from_top) * 100.0
