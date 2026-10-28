"""ESC telemetry from the custom INAV 7.1.1 + AM32 ID2BIT firmware (MSPv2 0x2F00, read-only).

Everything here is optional: on a stock INAV the probe simply reports "not available" and the rest of
the app carries on. Layout comes from docs-local/INAV_AM32_ID2BIT_V4_Developer_Guide.docx (section 7):
18-byte header + 26 bytes per ESC, little-endian.

While a motor run (sweep / slider drive) is active the poller asks for a sample every POLL_MS and, next to
each ESC's real values, records what the operator planned (curve value for that moment) and what was
actually sent (after the slew guard) into a CSV, plus a compact summary into the technical log.
"""
from __future__ import annotations

import csv
import struct
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal

from .file_logger import LOG_DIR
from .motor_ramp import msp_to_percent
from .msp import MspClient, MspCommand, MspError

POLL_TIMEOUT_S = 0.3  # a slow USB-serial driver must not turn every poll into a timeout
POLL_MS = 30  # the ESCs send a KISS frame every ~30.6 ms per ESC, so faster polling only repeats samples
HEADER_FMT = "<BBIIII"
ESC_FMT = "<BBBBHHHHIIIH"
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 18
ESC_SIZE = struct.calcsize(ESC_FMT)  # 26
SUPPORTED_VERSION = 1
AGE_INVALID = 255
MAX_FAILS_IN_ROW = 5  # then the poller gives up for this run (a motor run is never aborted because of it)
SUMMARY_EVERY_S = 1.0  # one compact line into the technical log; the full 30 ms stream goes to the CSV
KEEP_CSV_FILES = 30
TELEMETRY_DIR = LOG_DIR / "telemetry"

CSV_COLUMNS = (
    "t_s", "phase", "esc", "id", "planned_msp", "sent_msp", "planned_pct", "sent_pct", "plan_minus_sent_pct",
    "rpm", "erpm", "rpm_per_pct", "voltage_v", "current_a", "temp_c", "cons_mah", "frames", "new_sample",
    "age", "dt_us",
)


@dataclass
class EscHeader:
    version: int
    motor_count: int
    valid: int
    crc_errors: int
    resync: int
    bad_id: int


@dataclass
class EscRecord:
    id: int
    active: bool
    age: int
    temp_c: int
    voltage_v: float
    current_a: float
    consumption_mah: int
    erpm: int
    rpm: int
    frames: int
    dt_us: int
    raw_current: int

    @property
    def stale(self) -> bool:
        return (not self.active) or self.age == AGE_INVALID


@dataclass
class CommandSnapshot:
    """What the motor loop is doing right now: per motor, the curve value vs. the value really sent."""

    phase: str
    planned: list[int]  # raw MSP 1000-2000, the curve value for this moment
    sent: list[int]  # raw MSP 1000-2000, after the slew guard


def parse_telemetry(payload: bytes) -> tuple[EscHeader, list[EscRecord]]:
    if len(payload) < HEADER_SIZE:
        raise ValueError(f"телеметрія ESC: {len(payload)} байт, потрібно ≥ {HEADER_SIZE}")
    version, count, valid, crc, resync, bad_id = struct.unpack_from(HEADER_FMT, payload, 0)
    if version != SUPPORTED_VERSION:
        raise ValueError(f"телеметрія ESC: непідтримувана версія формату {version}")
    if len(payload) < HEADER_SIZE + count * ESC_SIZE:
        raise ValueError(f"телеметрія ESC: {len(payload)} байт для {count} ESC, очікувалось {HEADER_SIZE + count * ESC_SIZE}")
    records = []
    for i in range(count):
        (esc_id, active, age, temp, volt_cv, cur_ca, cons, erpm, rpm, frames, dt_us, raw_cur) = struct.unpack_from(
            ESC_FMT, payload, HEADER_SIZE + i * ESC_SIZE
        )
        records.append(EscRecord(esc_id, bool(active), age, temp, volt_cv / 100.0, cur_ca / 100.0, cons, erpm,
                                 rpm, frames, dt_us, raw_cur))
    return EscHeader(version, count, valid, crc, resync, bad_id), records


def describe_sample(payload: bytes, header: EscHeader, records: list[EscRecord]) -> str:
    """One line for the technical log: raw bytes and every decoded field, to check what the FC really sent."""
    esc = "; ".join(
        f"ESC{i + 1}: id={r.id} active={int(r.active)} age={r.age} rpm={r.rpm} erpm={r.erpm} {r.voltage_v:.2f}V "
        f"{r.current_a:.2f}A {r.temp_c}C frames={r.frames} dt={r.dt_us}us" for i, r in enumerate(records)
    )
    return (f"{len(payload)} байт, valid={header.valid} crc_err={header.crc_errors} resync={header.resync} "
            f"bad_id={header.bad_id}; {esc}; raw={payload[:HEADER_SIZE + ESC_SIZE * 2].hex(' ')}")


def probe(msp: MspClient) -> tuple[bool, str]:
    """True + description if the connected FC runs the ID2BIT build; never raises."""
    try:
        resp = msp.send(MspCommand.MSP2_INAV_ESC_ID2BIT_TELEMETRY, timeout_s=0.3, retries=2)
        header, records = parse_telemetry(resp.payload)
    except (MspError, ValueError, OSError, TimeoutError) as e:
        return False, str(e)
    live = sum(1 for r in records if not r.stale)
    detail = f"формат v{header.version}, ESC: {header.motor_count}, з даними: {live}"
    return True, detail + " | " + describe_sample(resp.payload, header, records)


def estimate_lag_ms(series: list[tuple[float, float, int]], max_lag_s: float = 1.5) -> str:
    """How far the RPM curve trails the sent throttle: the shift (ms) that makes the two curves match best."""
    if len(series) < 30:
        return "мало даних"
    ts, sent, rpm = [p[0] for p in series], [p[1] for p in series], [float(p[2]) for p in series]
    if max(sent) - min(sent) < 20 or max(rpm) < 1:
        return "немає руху для оцінки"
    step = (ts[-1] - ts[0]) / (len(ts) - 1)
    if step <= 0:
        return "мало даних"

    def norm(v: list[float]) -> list[float]:
        lo, hi = min(v), max(v)
        return [(x - lo) / (hi - lo or 1) for x in v]

    a, b = norm(sent), norm(rpm)
    best_k, best_err = 0, float("inf")
    for k in range(int(max_lag_s / step) + 1):
        n = len(a) - k
        err = sum((a[i] - b[i + k]) ** 2 for i in range(n)) / n
        if err < best_err:
            best_k, best_err = k, err
    return f"~{best_k * step * 1000:.0f} мс"


class _RunRecorder:
    """CSV of one run + running statistics for its summary line."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(CSV_COLUMNS)
        self.started = time.monotonic()
        self.rows = 0
        self.new_samples = 0
        self.stale_rows = 0
        self.max_rpm: dict[int, int] = {}
        self.max_gap_pct = 0.0
        self.first_header: Optional[EscHeader] = None
        self.last_header: Optional[EscHeader] = None
        self._last_frames: dict[int, int] = {}
        self.series: dict[int, list[tuple[float, float, int]]] = {}  # esc -> (t, sent %, rpm) for the lag estimate
        self.rtts: list[float] = []  # seconds one 0x2F00 request took

    def add(self, header: EscHeader, records: list[EscRecord], snap: CommandSnapshot) -> None:
        t = time.monotonic() - self.started
        self.first_header = self.first_header or header
        self.last_header = header
        for i, rec in enumerate(records):
            planned = snap.planned[i] if i < len(snap.planned) else 0
            sent = snap.sent[i] if i < len(snap.sent) else 0
            planned_pct, sent_pct = msp_to_percent(planned), msp_to_percent(sent)
            gap = planned_pct - sent_pct
            is_new = rec.frames != self._last_frames.get(i)
            self._last_frames[i] = rec.frames
            self.new_samples += is_new
            self.stale_rows += rec.stale
            self.max_gap_pct = max(self.max_gap_pct, abs(gap))
            self.max_rpm[i] = max(self.max_rpm.get(i, 0), rec.rpm)
            self.series.setdefault(i, []).append((t, sent_pct, rec.rpm))
            per_pct = f"{rec.rpm / sent_pct:.1f}" if sent_pct >= 1 else ""
            self._writer.writerow((
                f"{t:.3f}", snap.phase, i + 1, rec.id, planned, sent, f"{planned_pct:.2f}", f"{sent_pct:.2f}",
                f"{gap:.2f}", rec.rpm, rec.erpm, per_pct, f"{rec.voltage_v:.2f}", f"{rec.current_a:.2f}",
                rec.temp_c, rec.consumption_mah, rec.frames, int(is_new), rec.age, rec.dt_us,
            ))
            self.rows += 1

    def close(self) -> str:
        self._file.close()
        dur = max(time.monotonic() - self.started, 1e-6)
        h0, h1 = self.first_header, self.last_header
        crc = (h1.crc_errors - h0.crc_errors) if h0 and h1 else 0
        bad = (h1.bad_id - h0.bad_id) if h0 and h1 else 0
        lag = ", ".join(f"ESC{i + 1} {estimate_lag_ms(s)}" for i, s in sorted(self.series.items()))
        rtt = (f"запит телеметрії: середній {sum(self.rtts) / len(self.rtts) * 1000:.1f} мс, найдовший {max(self.rtts) * 1000:.1f} мс"
               if self.rtts else "")
        rpms = ", ".join(f"ESC{i + 1} {v}" for i, v in sorted(self.max_rpm.items())) or "—"
        return (f"{dur:.1f} с, рядків {self.rows}, нових зразків {self.new_samples}, застарілих {self.stale_rows}, "
                f"макс. RPM: {rpms}, макс. розбіжність план/відправлено {self.max_gap_pct:.2f}%, "
                f"CRC-помилок за прогін {crc}, bad_id {bad}. Відставання RPM від відправленого газу: {lag}. {rtt}. CSV: {self.path}")


class EscTelemetryPoller(QObject):
    """Lives in the motor manager's thread. Polls while a run is active (or while `live` is on)."""

    telemetry = pyqtSignal(object, object, object)  # EscHeader, list[EscRecord], CommandSnapshot | None
    log = pyqtSignal(str, str)  # level, text

    def __init__(self, msp_getter: Callable[[], Optional[MspClient]],
                 snapshot_getter: Callable[[], Optional[CommandSnapshot]], parent: Optional[QObject] = None) -> None:
        # `parent` matters: without it the poller (and its timer) stays in the creating thread when the manager
        # is moved to its worker thread, and QTimer.start() from another thread silently does nothing.
        super().__init__(parent)
        self._msp_getter = msp_getter
        self._snapshot_getter = snapshot_getter
        self.available = False
        self.live = False
        self._recorder: Optional[_RunRecorder] = None
        self._fails = 0
        self._last_summary = 0.0
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

    def set_available(self, available: bool) -> None:
        self.available = available
        if not available:
            self.stop()

    def ensure_running(self) -> None:
        if not self.available:
            self.log.emit("info", "Телеметрія ESC: опитування не запущено - прошивка FC без ID2BIT (див. перевірку при підключенні).")
        elif not self._timer.isActive():
            self._fails = 0
            self._timer.start()
            self.log.emit("info", f"Телеметрія ESC: опитування запущено (таймер {POLL_MS} мс, потік {QThread.currentThread().objectName() or 'головний'}).")

    def set_live(self, live: bool) -> None:
        self.live = live
        if live:
            self.ensure_running()

    def stop(self) -> None:
        self._timer.stop()
        self._finish_run()

    def _finish_run(self) -> None:
        if self._recorder is None:
            return
        rec, self._recorder = self._recorder, None
        self.log.emit("info", "Телеметрія ESC, підсумок прогону: " + rec.close())
        self._prune_old_files()

    @staticmethod
    def _prune_old_files() -> None:
        try:
            files = sorted(TELEMETRY_DIR.glob("telemetry_*.csv"))
            for old in files[:-KEEP_CSV_FILES]:
                old.unlink()
        except OSError:
            pass

    def _poll(self) -> None:
        msp = self._msp_getter()
        snap = self._snapshot_getter()
        if msp is None or (snap is None and not self.live):
            self.stop()
            return
        t0 = time.monotonic()
        try:
            resp = msp.send(MspCommand.MSP2_INAV_ESC_ID2BIT_TELEMETRY, timeout_s=POLL_TIMEOUT_S, retries=1)
            header, records = parse_telemetry(resp.payload)
        except (MspError, ValueError, OSError, TimeoutError) as e:
            self._fails += 1
            if self._fails == 1:
                self.log.emit("warning", f"Телеметрія ESC: збій опитування ({type(e).__name__}: {e}).")
            if self._fails >= MAX_FAILS_IN_ROW:
                self.log.emit("warning", f"Телеметрія ESC: {self._fails} збоїв поспіль ({e}) - опитування вимкнено до наступного прогону.")
                self.stop()
            return
        self._fails = 0
        rtt = time.monotonic() - t0

        if snap is None:
            self._finish_run()
        else:
            if self._recorder is None:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                try:
                    self._recorder = _RunRecorder(TELEMETRY_DIR / f"telemetry_{stamp}.csv")
                except OSError as e:
                    self.log.emit("warning", f"Телеметрія ESC: не вдалося створити CSV ({e}) - лише журнал.")
                    self._recorder = None
                self._last_summary = 0.0
                self.log.emit("info", f"Телеметрія ESC: запис прогону кожні {POLL_MS} мс ({header.motor_count} ESC).")
            if self._recorder is not None:
                self._recorder.add(header, records, snap)
                self._recorder.rtts.append(rtt)
            self._log_compact(records, snap)
        self.telemetry.emit(header, records, snap)

    def _log_compact(self, records: list[EscRecord], snap: CommandSnapshot) -> None:
        now = time.monotonic()
        if now - self._last_summary < SUMMARY_EVERY_S:
            return
        self._last_summary = now
        parts = []
        for i, r in enumerate(records):
            planned = msp_to_percent(snap.planned[i]) if i < len(snap.planned) else 0.0
            sent = msp_to_percent(snap.sent[i]) if i < len(snap.sent) else 0.0
            parts.append(f"ESC{i + 1}: план {planned:.1f}% / відправл. {sent:.1f}% / {r.rpm} RPM, "
                         f"{r.voltage_v:.1f} В, {r.current_a:.2f} А, {r.temp_c}°C" + (" [застаріло]" if r.stale else ""))
        self.log.emit("info", f"Телеметрія ESC [{snap.phase}] " + " | ".join(parts))
