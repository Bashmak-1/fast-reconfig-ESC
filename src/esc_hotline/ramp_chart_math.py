"""Pure (no Qt) math for the motor ramp profile: a three-phase
розгін (up) -> утримання (hold) -> спад (down) timeline, each phase given
directly as a duration in seconds (not a %/s rate) - this is the single
shared source of truth for BOTH the real motor control
(motor_test_manager.py's own ticking) and the chart's drawing/dragging
(ramp_chart_widget.py), so the two can never silently drift apart. Kept
separate from motor_ramp.py (which stays scoped to generic MSP-value
helpers like percent_to_msp/cubic_bezier_ease) - this file is specifically
the ramp PROFILE shape and its chart geometry.
"""
from __future__ import annotations

from typing import Optional

from .motor_ramp import cubic_bezier_ease
from .ramp_curve import RampCurve

MIN_BOUND_GAP_PERCENT = 5.0
MIN_PHASE_DURATION_S = 0.1  # anything shorter is instantaneous for any practical purpose - also avoids div-by-zero
MAX_PHASE_DURATION_S = 60.0

# What "Скинути графік" restores: durations (s) and bounds (%).
DEFAULT_UP_DURATION_S = 4.0
DEFAULT_HOLD_DURATION_S = 0.0
DEFAULT_DOWN_DURATION_S = 4.0
DEFAULT_MIN_PERCENT = 0.0
DEFAULT_MAX_PERCENT = 100.0


def segment_value(
    use_bezier: bool, elapsed_s: float, duration_s: float, start_percent: float, end_percent: float,
    curve: Optional[RampCurve] = None,
) -> float:
    """Percent position `elapsed_s` seconds into a `duration_s`-long move
    from start_percent to end_percent - a straight line, or the same
    cubic_bezier_ease() the real ramp motion uses for "Безе". Clamped to
    the segment's own span: elapsed_s before 0 clamps to the start value,
    at/after duration_s clamps to the end value (never overshoots,
    mirrors next_value_bezier()'s own contract before it was folded into
    this shared function)."""
    if duration_s <= 0:
        return end_percent
    t = max(0.0, min(1.0, elapsed_s / duration_s))
    # `curve` is the operator-edited shape; without one, "Безе" is the old fixed smoothstep (same numbers).
    eased = (curve.ease(t) if curve is not None else cubic_bezier_ease(t)) if use_bezier else t
    return start_percent + (end_percent - start_percent) * eased


def profile_value_at_time(
    use_bezier: bool,
    elapsed_s: float,
    up_duration_s: float,
    hold_duration_s: float,
    down_duration_s: float,
    min_percent: float,
    max_percent: float,
    up_curve: Optional[RampCurve] = None,
    down_curve: Optional[RampCurve] = None,
) -> float:
    """Percent value at `elapsed_s` into the FULL розгін->утримання->спад
    profile for one motor's one cycle, given as a single continuous
    timeline (0 = cycle start). This is what both the chart draws across
    its whole width and what motor_test_manager.py's own phase-by-phase
    ticking computes (one call per tick, with elapsed_s measured from
    whichever phase is current) - same function, same numbers, by
    construction.
    """
    if elapsed_s < up_duration_s:
        return segment_value(use_bezier, elapsed_s, up_duration_s, min_percent, max_percent, up_curve)
    if elapsed_s < up_duration_s + hold_duration_s:
        return max_percent
    down_elapsed = elapsed_s - up_duration_s - hold_duration_s
    return segment_value(use_bezier, down_elapsed, down_duration_s, max_percent, min_percent, down_curve)


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


def clamp_duration_s(value_s: float) -> float:
    """Clamps a phase duration (up/hold/down) to a sane range - used both
    for the steppers' own min/max and for values arriving via chart drag."""
    return max(MIN_PHASE_DURATION_S, min(MAX_PHASE_DURATION_S, value_s))


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


def time_to_pixel_x(t_s: float, total_duration_s: float, left: float, width: float) -> float:
    """Elapsed-time (0..total_duration_s) -> pixel X within [left, left+width]."""
    if total_duration_s <= 0:
        return left
    frac = max(0.0, min(1.0, t_s / total_duration_s))
    return left + frac * width


def pixel_x_to_time(x: float, total_duration_s: float, left: float, width: float) -> float:
    """Inverse of time_to_pixel_x() - used by the widget's drag handling
    to turn a mouse X position back into an elapsed-time-in-seconds."""
    if width <= 0:
        return 0.0
    frac = max(0.0, min(1.0, (x - left) / width))
    return frac * total_duration_s
