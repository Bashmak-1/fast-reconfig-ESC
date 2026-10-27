"""Editable ease curve for one ramp leg (розгін or спад): a piecewise cubic
Bezier in NORMALIZED coordinates - x = time fraction 0..1, y = progress
0..1 (0 = where the leg starts, 1 = where it ends). Normalized so changing
a phase's duration or the min/max bounds never deforms the shape, and the
same curve serves both the chart drawing and the real motor control.

Control points are stored flat as P0, c, c, P1, c, c, P2, ... (1 + 3n points
for n segments): every third point is an anchor, the two points around an
anchor are its handles. Illustrator-style: drag an anchor, drag its handles
to change the slope through it.

Default = the old fixed "Безе" curve 3t^2 - 2t^3 split exactly at t=0.5 into
two Bezier segments (so a default curve behaves bit-for-bit like before,
and the middle anchor already sits on it).

Every interior anchor has a handle type (saved with the curve): SMOOTH - the two handles stay on one
straight line through the anchor (180 degrees, dragging one turns the other); INDEPENDENT - each handle is
moved on its own, so the curve can bend sharply or leave the anchor along one handle only.

Safety rules (enforced by every constructor/editor here, not by the UI):
- anchors strictly increase in x and control x stays inside its own segment,
  so x(t) is monotonic - the curve is always a real function of time;
- every y is clamped to [0, 1], so the output can never overshoot the
  min/max bounds the operator configured.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

Point = tuple[float, float]

# settings_store keys shared by "Прогон мотором", "Прошивка FC" and the FC station's own motor test
UP_CURVE_KEY = "motor_test_up_curve"
DOWN_CURVE_KEY = "motor_test_down_curve"
TICK_KEY = "motor_test_tick_ms"
SHOW_POINTS_KEY = "motor_test_show_points"

MIN_ANCHOR_GAP = 0.05  # keep anchors this far (in x) from each other / the ends
_SOLVE_ITERATIONS = 50
SMOOTH, INDEPENDENT = 0, 1  # per-anchor handle type

_DEFAULT_POINTS: tuple[Point, ...] = (
    (0.0, 0.0), (1 / 6, 0.0), (1 / 3, 0.25), (0.5, 0.5), (2 / 3, 0.75), (5 / 6, 1.0), (1.0, 1.0),
)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _bezier(p0: float, p1: float, p2: float, p3: float, s: float) -> float:
    u = 1.0 - s
    return u * u * u * p0 + 3 * u * u * s * p1 + 3 * u * s * s * p2 + s * s * s * p3


def _valid(pts: tuple[Point, ...]) -> bool:
    if len(pts) < 4 or (len(pts) - 1) % 3 != 0:
        return False
    if pts[0] != (0.0, 0.0) or pts[-1] != (1.0, 1.0):
        return False
    anchors = pts[0::3]
    for a, b in zip(anchors, anchors[1:]):
        if b[0] - a[0] < MIN_ANCHOR_GAP - 1e-9:
            return False
    for k, (x, y) in enumerate(pts):
        if not (0.0 <= y <= 1.0 and 0.0 <= x <= 1.0):
            return False
        if k % 3 != 0:  # a handle must stay inside its own segment's x-range
            seg = (k - 1) // 3
            if not anchors[seg][0] <= x <= anchors[seg + 1][0]:
                return False
    return True


@dataclass(frozen=True)
class RampCurve:
    pts: tuple[Point, ...]
    modes: tuple[int, ...] = ()  # handle type per interior anchor (anchor k -> modes[k - 1]); missing = SMOOTH

    def mode_of(self, anchor: int) -> int:
        return self.modes[anchor - 1] if 0 < anchor <= len(self.modes) else SMOOTH

    # ---- construction ------------------------------------------------

    @staticmethod
    def default() -> "RampCurve":
        return RampCurve(_DEFAULT_POINTS)

    @staticmethod
    def from_points(points, modes=()) -> Optional["RampCurve"]:
        try:
            pts = tuple((float(x), float(y)) for x, y in points)
            modes = tuple(INDEPENDENT if int(m) == INDEPENDENT else SMOOTH for m in modes)
        except (TypeError, ValueError):
            return None
        return RampCurve(pts, modes) if _valid(pts) else None

    @staticmethod
    def from_json(text: str) -> "RampCurve":
        """Never raises: anything missing/corrupt/unsafe falls back to the default curve."""
        try:
            data = json.loads(text)
            if data.get("v") in (1, 2):  # v1 (older builds) has no handle types - all smooth
                curve = RampCurve.from_points(data["p"], data.get("m", ()))
                if curve is not None:
                    return curve
        except (TypeError, ValueError, KeyError, AttributeError):
            pass
        return RampCurve.default()

    def to_json(self) -> str:
        data = {"v": 2, "p": [[round(x, 5), round(y, 5)] for x, y in self.pts], "m": list(self.modes)}
        return json.dumps(data, separators=(",", ":"))

    # ---- evaluation ------------------------------------------------

    @property
    def segments(self) -> int:
        return (len(self.pts) - 1) // 3

    def ease(self, t: float) -> float:
        """Progress 0..1 at time fraction `t` (clamped). Solves x(s) = t on the segment that
        contains t by bisection (x is monotonic by construction), then returns y(s)."""
        t = _clamp(t, 0.0, 1.0)
        pts = self.pts
        seg = 0
        while seg < self.segments - 1 and t > pts[3 * (seg + 1)][0]:
            seg += 1
        p0, p1, p2, p3 = pts[3 * seg: 3 * seg + 4]
        lo, hi = 0.0, 1.0
        for _ in range(_SOLVE_ITERATIONS):
            mid = (lo + hi) / 2
            if _bezier(p0[0], p1[0], p2[0], p3[0], mid) < t:
                lo = mid
            else:
                hi = mid
        return _clamp(_bezier(p0[1], p1[1], p2[1], p3[1], (lo + hi) / 2), 0.0, 1.0)

    # ---- editing (each returns a new, always-valid curve) ----------------

    def _anchor_x_bounds(self, anchor: int) -> tuple[float, float]:
        anchors = self.pts[0::3]
        return anchors[anchor - 1][0] + MIN_ANCHOR_GAP, anchors[anchor + 1][0] - MIN_ANCHOR_GAP

    def _fit_handle(self, k: int, pos: Point, anchors: list[float]) -> Point:
        seg = (k - 1) // 3
        return _clamp(pos[0], anchors[seg], anchors[seg + 1]), _clamp(pos[1], 0.0, 1.0)

    def move_anchor(self, anchor: int, pos: Point) -> "RampCurve":
        """Move an interior anchor (index 1..segments-1); its handles travel with it."""
        if not 0 < anchor < self.segments:
            return self
        lo, hi = self._anchor_x_bounds(anchor)
        new = (_clamp(pos[0], lo, hi), _clamp(pos[1], 0.0, 1.0))
        old = self.pts[3 * anchor]
        dx, dy = new[0] - old[0], new[1] - old[1]
        pts = list(self.pts)
        pts[3 * anchor] = new
        for k in (3 * anchor - 1, 3 * anchor + 1):
            pts[k] = (pts[k][0] + dx, pts[k][1] + dy)
        anchor_xs = [p[0] for p in pts[0::3]]
        for k in range(len(pts)):  # the neighbouring segments' far handles may now poke outside their range too
            if k % 3:
                pts[k] = self._fit_handle(k, pts[k], anchor_xs)
        return RampCurve(tuple(pts), self.modes)

    def with_mode(self, anchor: int, mode: int) -> "RampCurve":
        """Set an interior anchor's handle type. Switching to SMOOTH turns the out-handle onto the line of the
        in-handle (keeping its length), so the point is smooth right away."""
        if not 0 < anchor < self.segments:
            return self
        modes = list(self.modes) + [SMOOTH] * (self.segments - 1 - len(self.modes))
        modes[anchor - 1] = INDEPENDENT if mode == INDEPENDENT else SMOOTH
        out = RampCurve(self.pts, tuple(modes))
        return out.move_handle(3 * anchor - 1, self.pts[3 * anchor - 1], True) if modes[anchor - 1] == SMOOTH else out

    def move_handle(self, k: int, pos: Point, smooth: bool = True) -> "RampCurve":
        """Move control point `k` (a handle, not an anchor). With `smooth` the opposite handle of an
        interior anchor turns to stay in line (keeping its own length), like a smooth Illustrator point.
        Callers pass smooth=curve.mode_of(anchor) == SMOOTH (and not Alt)."""
        if k % 3 == 0 or not 0 < k < len(self.pts) - 1:
            return self
        pts = list(self.pts)
        anchor_xs = [p[0] for p in pts[0::3]]
        pts[k] = self._fit_handle(k, pos, anchor_xs)
        a = k - 1 if k % 3 == 1 else k + 1  # index of the anchor this handle belongs to
        other = 2 * a - k
        if smooth and 0 < a < len(pts) - 1 and 0 < other < len(pts) - 1:
            ax, ay = pts[a]
            vx, vy = pts[k][0] - ax, pts[k][1] - ay
            n = (vx * vx + vy * vy) ** 0.5
            if n > 1e-9:
                olen = ((pts[other][0] - ax) ** 2 + (pts[other][1] - ay) ** 2) ** 0.5
                pts[other] = self._fit_handle(other, (ax - vx / n * olen, ay - vy / n * olen), anchor_xs)
        return RampCurve(tuple(pts), self.modes)
