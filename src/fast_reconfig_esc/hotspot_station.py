"""The state machine behind the "Хотспот (лінія)" tab (hotspot_line_widget.py):
watches exactly ONE operator-chosen ESC output index, not a full sweep of
every index the FC reports. This is a deliberately DIFFERENT workflow from
the original "Хотспот" tab (hotspot_widget.py), which sweeps every ESC
output the FC reports (EscManager.discover_all) - the two tabs share
nothing here, not even the phase constants, since they're genuinely
different state machines for genuinely different use cases (flash a whole
batch of already-wired ESCs, vs. hot-swap one ESC through one output
forever). See docs/PROTOCOL_NOTES.md 2026-08-26 for why an earlier version
of this file's design was briefly (wrongly) applied to hotspot_widget.py
too.

Pure QObject, no widgets: reports everything through signals so any UI can
bind to it.

Sweeping every possible output every ~1s (as hotspot_widget.py's own,
separate design still does, by design) means probing every permanently-
empty output too, forever - which logs an "error"-level GUI line for each
one (see EscManager.discover_all). Here, "nothing plugged into the watched
slot right now" is the expected steady state between ESC swaps, not an
anomaly - see EscManager.discover_one, which never does that.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .connection_worker import ConnectionWorker
from .serial_transport import list_fc_ports
from .theme import ACCENT_AMBER, ACCENT_ERROR, ACCENT_GREEN, BG_PANEL, TEXT_MAIN, TEXT_MUTED

DEFAULT_BAUD = 115200
MAX_CONNECT_ATTEMPTS = 5
POLL_INTERVAL_MS = 1000

PHASE_STYLE = {
    "idle": (BG_PANEL, TEXT_MUTED),
    "waiting_device": (BG_PANEL, TEXT_MAIN),
    "connecting": (ACCENT_AMBER, "white"),
    "watching": (BG_PANEL, TEXT_MAIN),
    "flashing": (ACCENT_ERROR, "white"),
    "esc_ready": (ACCENT_GREEN, "white"),
    "esc_error": (ACCENT_ERROR, "white"),
    "error_connect": (ACCENT_ERROR, "white"),
}
PHASE_BANNER_KEY = {
    "idle": "hotspot_banner_idle",
    "waiting_device": "hotspot_banner_waiting_device",
    "connecting": "hotspot_banner_connecting",
    "watching": "hotspot_banner_watching_esc",
    "flashing": "hotspot_banner_flashing",
    "esc_ready": "hotspot_banner_done",
    "esc_error": "hotspot_banner_error_flash",
    "error_connect": "hotspot_banner_error_connect",
}
# Phases where, once the FC is confirmed still present, the poll tick keeps
# acting on its own (checking the watched slot, or re-checking it after a
# flash) - no click needed. Only a true connection-level failure
# (error_connect) waits for a manual retry or the FC itself disappearing.
WATCH_PHASES = {"watching", "esc_ready", "esc_error"}
RETRY_PHASES = {"error_connect"}


def phase_after_discovery(found: int, last_state: Optional[str]) -> tuple[str, bool]:
    """What to do after discover_one(slot) reports `found` (0 or 1) and,
    when found, the ESC's last-known classification state
    ("ok"/"diff"/"blank"). Returns (next_phase, should_flash).

    Pure and Qt-free, unit-tested in tests/test_hotspot_station.py.
    """
    if found == 0:
        return "watching", False
    if last_state == "ok":
        return "esc_ready", False
    return "flashing", True


def phase_after_flash(success: int, attempted: int) -> str:
    """What phase a flash_bytes([slot]) call resolves to. Only ever called
    once discover_one has already confirmed the slot needs a write, so
    `attempted` is always 1 here - `success == attempted` still guards
    against the degenerate 0/0 case defensively."""
    return "esc_ready" if (success == attempted and attempted > 0) else "esc_error"


class HotspotStationController(QObject):
    """Owns one private ConnectionWorker (its own EscManager + dedicated
    QThread) and drives the continuous hot-plug loop for ONE watched ESC
    slot: idle -> waiting_device -> connecting -> watching -> [flashing ->]
    esc_ready/esc_error -> loops back to watching the moment the ESC is
    unplugged, all while the FC stays connected, with error_connect as the
    one phase needing a manual retry (or the FC disappearing).
    """

    phase_changed = pyqtSignal(str)
    esc_detail_changed = pyqtSignal(str, str)  # EscState.value, detail text
    counters_changed = pyqtSignal(int, int, int)  # flashed, ok, errors
    armed_changed = pyqtSignal(bool)

    # Internal-only: reach the private EscManager, which lives on another
    # thread - never call its methods directly, only through these signals.
    _connect_requested = pyqtSignal(str, int)
    _disconnect_requested = pyqtSignal()
    _discover_one_requested = pyqtSignal(int)
    _flash_requested = pyqtSignal(bytes, object)
    _set_config_path_requested = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._worker = ConnectionWorker()
        manager = self._worker.manager

        self._connect_requested.connect(manager.connect)
        self._disconnect_requested.connect(manager.disconnect)
        self._discover_one_requested.connect(manager.discover_one)
        self._flash_requested.connect(manager.flash_bytes)
        self._set_config_path_requested.connect(manager.set_config_path)

        manager.connection_state_changed.connect(self._on_connection_state_changed)
        manager.discovery_finished.connect(self._on_discovery_finished)
        manager.flash_finished.connect(self._on_flash_finished)
        manager.esc_status_changed.connect(self._on_esc_status_changed)

        self._config_bytes: Optional[bytes] = None
        self._port_device: Optional[str] = None
        self._esc_index: int = 0
        self._last_esc_state: Optional[str] = None
        self._armed = False
        self._phase = "idle"
        self._connect_attempts = 0
        self._connect_in_flight = False
        self._counter_flashed = 0
        self._counter_ok = 0
        self._counter_errors = 0

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._on_poll_tick)

    # ---- public API ------------------------------------------------

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def armed(self) -> bool:
        return self._armed

    def counters(self) -> tuple[int, int, int]:
        return self._counter_flashed, self._counter_ok, self._counter_errors

    def connect_attempts(self) -> int:
        return self._connect_attempts

    def poll_timer_remaining_ms(self) -> int:
        """Milliseconds until the next poll tick, or -1 if not currently
        polling. Lets the UI show a reassuring "next check in..." countdown
        - an impatient operator watching a static banner for a few seconds
        can otherwise easily mistake "still polling" for "stuck"."""
        return self._poll_timer.remainingTime() if self._poll_timer.isActive() else -1

    def set_port(self, device: Optional[str]) -> None:
        self._port_device = device

    def set_esc_index(self, index: int) -> None:
        self._esc_index = index

    def set_config(self, path: str, config_bytes: bytes) -> None:
        self._config_bytes = config_bytes
        self._set_config_path_requested.emit(path)

    def start(self) -> None:
        if self._armed:
            return
        self._armed = True
        self.armed_changed.emit(True)
        self._connect_attempts = 0
        self._connect_in_flight = False
        self._last_esc_state = None
        self._set_phase("waiting_device")
        self._poll_timer.start()

    def stop(self) -> None:
        if not self._armed:
            return
        self._armed = False
        self.armed_changed.emit(False)
        self._poll_timer.stop()
        if self._phase not in ("idle", "waiting_device"):
            self._disconnect_requested.emit()
        self._set_phase("idle")

    def retry(self) -> None:
        if self._phase == "error_connect":
            self._connect_attempts = 0
            self._set_phase("connecting")
            self._attempt_connect()

    def reset_counters(self) -> None:
        self._counter_flashed = 0
        self._counter_ok = 0
        self._counter_errors = 0
        self.counters_changed.emit(0, 0, 0)

    def shutdown(self) -> None:
        """Stop this station's loop/connection and its dedicated thread -
        call when the app is closing, so no thread or serial handle is left
        dangling."""
        self.stop()
        self._worker.stop()

    # ---- polling / hot-plug detection ------------------------------------------------

    def _on_poll_tick(self) -> None:
        if not self._armed or not self._port_device:
            return
        present = self._port_device in {info.device for info in list_fc_ports()}

        if self._phase == "waiting_device":
            if present:
                self._connect_attempts = 0
                self._set_phase("connecting")
                self._attempt_connect()
        elif self._phase == "connecting":
            if self._connect_in_flight:
                # EscManager.connect() includes a required ~2s protocol
                # delay (POST_PASSTHROUGH_DELAY_S) after entering
                # passthrough - longer than this 1s poll interval. Firing
                # another connect_requested before the first one has even
                # resolved queues a second EscManager.connect() call that
                # runs right after the first succeeds, tries to open the
                # already-open port, and fails - and that failed open
                # attempt was confirmed on real hardware to disrupt the
                # FIRST connection's own FourWay traffic, not just fail
                # cleanly. Wait for the in-flight attempt to resolve
                # (success or failure) before ever attempting another.
                return
            if not present:
                self._set_phase("waiting_device")
            elif self._connect_attempts >= MAX_CONNECT_ATTEMPTS:
                self._set_phase("error_connect")
            else:
                self._attempt_connect()
        elif self._phase == "error_connect":
            if not present:
                self._set_phase("waiting_device")
        elif self._phase in WATCH_PHASES:
            if present:
                self._discover_one_requested.emit(self._esc_index)
            else:
                # The FC's own USB link is actually gone (not just the
                # watched ESC's wire, which is the normal every-swap event)
                # - release the connection and wait for it to come back.
                self._disconnect_requested.emit()
                self._set_phase("waiting_device")
        # "flashing": no polling action, waits for _on_flash_finished.

    def _attempt_connect(self) -> None:
        if not self._port_device:
            self._set_phase("error_connect")
            return
        self._connect_attempts += 1
        self._connect_in_flight = True
        self._connect_requested.emit(self._port_device, DEFAULT_BAUD)

    def _set_phase(self, phase: str) -> None:
        self._phase = phase
        self.phase_changed.emit(phase)

    # ---- forwarded EscManager signals ------------------------------------------------

    def _on_connection_state_changed(self, connected: bool, expected_count: int) -> None:
        # Cleared unconditionally, before the phase guard below - a stale
        # in-flight flag left True would otherwise silently block every
        # future reconnect attempt if this particular signal happens to
        # arrive after the phase already moved on (see _on_poll_tick).
        self._connect_in_flight = False
        if not self._armed or self._phase != "connecting":
            return
        if connected:
            self._last_esc_state = None
            self.esc_detail_changed.emit("idle", "")
            self._set_phase("watching")
        # A failed attempt just leaves us in "connecting" - the poll timer retries or gives up.

    def _on_discovery_finished(self, found: int, expected: int) -> None:
        if not self._armed or self._phase not in WATCH_PHASES:
            return
        was_watching = self._phase == "watching"
        next_phase, should_flash = phase_after_discovery(found, self._last_esc_state)
        if next_phase == "watching":
            if not was_watching:
                self.esc_detail_changed.emit("idle", "")
            self._set_phase("watching")
            return
        if should_flash:
            self._set_phase("flashing")
            self._flash_requested.emit(self._config_bytes, [self._esc_index])
            return
        # Already matches - no write needed. Count it as "already OK" only
        # the moment it's newly detected, not on every subsequent tick.
        if self._phase != "esc_ready":
            self._counter_ok += 1
            self.counters_changed.emit(self._counter_flashed, self._counter_ok, self._counter_errors)
        self._set_phase("esc_ready")

    def _on_flash_finished(self, success: int, attempted: int) -> None:
        if not self._armed or self._phase != "flashing":
            return
        next_phase = phase_after_flash(success, attempted)
        if next_phase == "esc_ready":
            self._counter_flashed += 1
        else:
            self._counter_errors += 1
        self.counters_changed.emit(self._counter_flashed, self._counter_ok, self._counter_errors)
        self._set_phase(next_phase)
        # Deliberately stays connected - the next poll tick keeps watching
        # the same slot (see _on_poll_tick / WATCH_PHASES).

    def _on_esc_status_changed(self, index: int, state: str, detail: str, esc_info) -> None:
        if not self._armed or index != self._esc_index:
            return
        self._last_esc_state = state
        self.esc_detail_changed.emit(state, detail)
