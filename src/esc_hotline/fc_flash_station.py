"""The state machine behind the "Прошивка FC" tab (fc_flash_widget.py):
auto-detects whether a DFU-mode flight controller is present, flashes it
via the external STM32_Programmer_CLI.exe (dfu_flash_manager.py), verifies
the write, resets it back into normal run mode, then - once a normal
FourWay-ready port is confirmed present - hands off to watching one or more
operator-chosen ESC outputs at once (e.g. both halves of a 2-in-1 ESC board
that shows up as two separate ESC indices), the same spirit as
hotspot_station.py's HotspotStationController but generalized from exactly
one watched slot to a small operator-chosen set. If the FC is already in
normal mode when this tab starts (no DFU flash needed this round), it
skips straight to that ESC-watching phase.

IMPORTANT, confirmed by the operator from real-world use of the AM32
website for the same DFU step: the FC's COM port number is NOT stable
across a DFU flash+reset cycle. Windows has been observed handing out a
DIFFERENT port number each time (reportedly climbing past COM400+ after
many cycles) - never rely on the operator-selected port name still being
the right one once "waiting_fc_normal" is reached. Instead, a snapshot of
every currently-visible port is taken right before the flash starts
(_pre_dfu_port_snapshot), and once normal mode is detected, whichever
port(s) are NEW relative to that snapshot are treated as the candidate
FC - see _pick_reenumerated_port(). This also transparently handles the
case where the number happens to stay the same (nothing "new" appears
with the old name either, since it already left the snapshot the moment
the FC entered DFU mode and disappeared from the normal port list).

Two cooperating private workers, each on its own dedicated QThread so
neither ever blocks the GUI:
- ConnectionWorker (connection_worker.py) - the same EscManager/FourWay
  worker every other private-connection tab uses, for the ESC phase.
- _DfuWorkerThread (this file) - runs the blocking
  STM32_Programmer_CLI.exe subprocess calls (a flash can take tens of
  seconds), mirroring ConnectionWorker's own moveToThread pattern applied
  to dfu_flash_manager's plain functions instead of EscManager.

Deliberately duplicates hotspot_station.py's ESC-watching phases/logic
rather than importing HotspotStationController - this project's own
convention (see hotspot_station.py's docstring, and docs/PROTOCOL_NOTES.md
2026-08-26) is that similar-but-not-identical workflows stay separate
classes, so a change meant for one can never accidentally reach the other.

The motor-protocol dropdown this tab's widget exposes (DSHOT300 etc., from
motor_ramp.MOTOR_PROTOCOLS) is informational/persisted only in this first
version - it is NOT wired to actually write MSP_SET_ADVANCED_CONFIG and
reboot the FC. motor_test_manager.py's set_protocol() is the proven
reference for that write-and-reboot dance if it's ever needed here, but
stacking a SECOND unverified reboot cycle on top of the also-unverified
DFU-flash step (no real STM32_Programmer_CLI.exe available while writing
this) is deliberately not done in the same change - add it as a focused
follow-up once the DFU step itself is confirmed on real hardware.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from . import dfu_flash_manager
from .connection_worker import ConnectionWorker
from .file_logger import get_logger
from .serial_transport import PortInfo, list_fc_ports
from .theme import ACCENT_AMBER, ACCENT_ERROR, ACCENT_GREEN, BG_PANEL, TEXT_MAIN, TEXT_MUTED

DEFAULT_BAUD = 115200
MAX_CONNECT_ATTEMPTS = 5
POLL_INTERVAL_MS = 1000
EMPTY_PROBES_BEFORE_POWER_CYCLE_HINT = 20  # same rationale as hotspot_station.py's own constant

# DFU-phase-only states, checked before ever reaching the ESC-watching
# phases shared in spirit (not in code) with hotspot_station.py.
DFU_PHASES = (
    "idle",
    "waiting_device",
    "flashing_fc",
    "resetting_fc",
    "waiting_fc_normal",
    "fc_flash_error",
)
# ESC-watching phases - same names/meaning as hotspot_station.py's own, so
# a reader already familiar with "Хотспот (лінія)" recognizes them
# immediately, even though this is a separate implementation.
WATCH_PHASES = {"watching", "esc_ready", "esc_error"}
RETRY_PHASES = {"error_connect", "fc_flash_error"}

PHASE_STYLE = {
    "idle": (BG_PANEL, TEXT_MUTED),
    "waiting_device": (BG_PANEL, TEXT_MAIN),
    "flashing_fc": (ACCENT_ERROR, "white"),
    "resetting_fc": (ACCENT_AMBER, "white"),
    "waiting_fc_normal": (BG_PANEL, TEXT_MAIN),
    "fc_flash_error": (ACCENT_ERROR, "white"),
    "connecting": (ACCENT_AMBER, "white"),
    "watching": (BG_PANEL, TEXT_MAIN),
    "flashing": (ACCENT_ERROR, "white"),
    "esc_ready": (ACCENT_GREEN, "white"),
    "esc_error": (ACCENT_ERROR, "white"),
    "error_connect": (ACCENT_ERROR, "white"),
}
PHASE_LABEL = {
    "idle": "Очікування",
    "waiting_device": "Очікування FC (звичайний режим або DFU)...",
    "flashing_fc": "Прошивка FC через DFU...",
    "resetting_fc": "Перезавантаження FC...",
    "waiting_fc_normal": "Очікування завантаження FC у звичайний режим...",
    "fc_flash_error": "Помилка прошивки FC",
    "connecting": "Підключення до FC...",
    "watching": "Стеження за ESC...",
    "flashing": "Прошивка ESC...",
    "esc_ready": "ESC готові",
    "esc_error": "Помилка прошивки ESC",
    "error_connect": "Помилка підключення",
}


def choose_reenumerated_port(candidates: list[PortInfo]) -> Optional[str]:
    """Pure: given the ports that are new relative to the pre-DFU snapshot
    (see this file's module docstring for why that snapshot exists at
    all), which one is most likely the FC that just came back from its
    reset. Normally there's exactly one; if several show up in the same
    tick, prefer one flagged likely_fc (vendor ID match - serial_transport.
    py) over an unrelated device that happened to be plugged in at the
    same moment. Pure and Qt-free, unit-tested in
    tests/test_fc_flash_station.py.
    """
    if not candidates:
        return None
    likely = [c for c in candidates if c.likely_fc]
    return (likely or candidates)[0].device


def phase_after_discovery_many(tick_states: dict[int, str], indices: list[int]) -> tuple[str, list[int]]:
    """Multi-slot sibling of hotspot_station.phase_after_discovery: given
    this tick's {index: state} for every watched index that actually
    responded (an index missing from `tick_states` simply wasn't found this
    tick - "not currently present" is the normal steady state between swaps,
    same as the single-slot version, not an error), decide the next phase
    and which indices (if any) need a config write this tick.

    Any responding index whose state isn't "ok" gets flashed right away,
    even if not every watched index has shown up yet - this is "esc_ready"
    only once every watched index has both been found AND already matches.
    Pure and Qt-free, unit-tested in tests/test_fc_flash_station.py.
    """
    if not tick_states:
        return "watching", []
    need_flash = [i for i in indices if tick_states.get(i) not in (None, "ok")]
    if need_flash:
        return "flashing", need_flash
    if len(tick_states) < len(indices):
        return "watching", []
    return "esc_ready", []


class _DfuWorker(QObject):
    """Lives on its own QThread (see _DfuWorkerThread) - runs the blocking
    STM32_Programmer_CLI.exe subprocess calls off the GUI thread. `cli_path`
    is passed as a plain string with every request (empty = "not found"),
    resolved on the GUI thread right before each call - cheap filesystem
    stat calls, no need to share mutable state across the thread boundary.
    """

    devices_listed = pyqtSignal(list)  # list[dfu_flash_manager.DfuDeviceInfo]
    flash_done = pyqtSignal(bool, str)
    reset_done = pyqtSignal(bool)

    def list_devices(self, cli_path: str) -> None:
        if not cli_path:
            self.devices_listed.emit([])
            return
        self.devices_listed.emit(dfu_flash_manager.list_devices(Path(cli_path)))

    def flash(self, cli_path: str, port_id: str, firmware_path: str) -> None:
        if not cli_path:
            self.flash_done.emit(False, "STM32_Programmer_CLI.exe не знайдено.")
            return
        result = dfu_flash_manager.flash_and_verify(Path(cli_path), port_id, firmware_path)
        self.flash_done.emit(result.success, result.message)

    def reset(self, cli_path: str, port_id: str) -> None:
        ok = bool(cli_path) and dfu_flash_manager.reset_to_app(Path(cli_path), port_id)
        self.reset_done.emit(ok)


class _DfuWorkerThread:
    """Mirrors connection_worker.ConnectionWorker's own moveToThread pattern."""

    def __init__(self) -> None:
        self.thread = QThread()
        self.thread.setObjectName("DfuWorkerThread")
        self.worker = _DfuWorker()
        self.worker.moveToThread(self.thread)
        self.thread.start()

    def stop(self) -> None:
        self.thread.quit()
        self.thread.wait(2000)


class FcFlashStationController(QObject):
    """Owns a private ConnectionWorker (EscManager + its own QThread, for
    the ESC phase) and a private _DfuWorkerThread (for the DFU-flash
    phase), and drives the combined loop:
    idle -> waiting_device -> [flashing_fc -> resetting_fc ->]
    waiting_fc_normal -> connecting -> watching -> [flashing ->]
    esc_ready/esc_error, looping back to watching exactly like
    hotspot_station.py once the FC is in normal mode.
    """

    phase_changed = pyqtSignal(str)
    esc_detail_changed = pyqtSignal(int, str, str)  # index, EscState.value, detail text
    dfu_detail_changed = pyqtSignal(str)
    counters_changed = pyqtSignal(int, int, int)  # flashed, ok, errors
    armed_changed = pyqtSignal(bool)
    power_cycle_hint_changed = pyqtSignal(bool)
    port_changed = pyqtSignal(str)  # FC re-enumerated under a different COM port after a DFU reset

    # Internal-only: reach the private EscManager, which lives on another
    # thread - never call its methods directly, only through these signals.
    _connect_requested = pyqtSignal(str, int)
    _disconnect_requested = pyqtSignal()
    _discover_many_requested = pyqtSignal(list)
    _flash_requested = pyqtSignal(bytes, object)
    _set_config_path_requested = pyqtSignal(str)

    # Internal-only: reach the private _DfuWorker.
    _list_devices_requested = pyqtSignal(str)
    _flash_fc_requested = pyqtSignal(str, str, str)
    _reset_fc_requested = pyqtSignal(str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._flog = get_logger()
        self._esc_worker = ConnectionWorker()
        manager = self._esc_worker.manager
        self._connect_requested.connect(manager.connect)
        self._disconnect_requested.connect(manager.disconnect)
        self._discover_many_requested.connect(manager.discover_many)
        self._flash_requested.connect(manager.flash_bytes)
        self._set_config_path_requested.connect(manager.set_config_path)
        manager.connection_state_changed.connect(self._on_connection_state_changed)
        manager.discovery_finished.connect(self._on_discovery_finished)
        manager.flash_finished.connect(self._on_flash_finished)
        manager.esc_status_changed.connect(self._on_esc_status_changed)

        self._dfu_worker_thread = _DfuWorkerThread()
        dfu_worker = self._dfu_worker_thread.worker
        self._list_devices_requested.connect(dfu_worker.list_devices)
        self._flash_fc_requested.connect(dfu_worker.flash)
        self._reset_fc_requested.connect(dfu_worker.reset)
        dfu_worker.devices_listed.connect(self._on_devices_listed)
        dfu_worker.flash_done.connect(self._on_flash_fc_done)
        dfu_worker.reset_done.connect(self._on_reset_fc_done)

        self._firmware_path: Optional[str] = None
        self._cli_path_override: Optional[str] = None
        self._config_bytes: Optional[bytes] = None
        self._port_device: Optional[str] = None
        self._esc_indices: list[int] = [0]
        self._last_esc_states: dict[int, str] = {}
        self._tick_seen_states: dict[int, str] = {}
        self._armed = False
        self._phase = "idle"
        self._connect_attempts = 0
        self._connect_in_flight = False
        self._dfu_op_in_flight = False
        self._dfu_port_id: Optional[str] = None
        self._pre_dfu_port_snapshot: set[str] = set()
        self._counter_flashed = 0
        self._counter_ok = 0
        self._counter_errors = 0
        self._consecutive_not_found = 0

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
        return self._poll_timer.remainingTime() if self._poll_timer.isActive() else -1

    def set_firmware_path(self, path: Optional[str]) -> None:
        self._firmware_path = path or None

    def set_cli_path_override(self, path: Optional[str]) -> None:
        self._cli_path_override = path or None

    def set_port(self, device: Optional[str]) -> None:
        self._port_device = device

    def set_esc_indices(self, indices: list[int]) -> None:
        self._esc_indices = list(indices)

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
        self._dfu_op_in_flight = False
        self._dfu_port_id = None
        self._pre_dfu_port_snapshot = set()
        self._last_esc_states = {}
        self._tick_seen_states = {}
        self._consecutive_not_found = 0
        self.power_cycle_hint_changed.emit(False)
        self.dfu_detail_changed.emit("")
        self._flog.info(
            "=== FC flash station armed: firmware=%s, cli_override=%s, esc_config_bytes=%s, port=%s, esc_indices=%s ===",
            self._firmware_path, self._cli_path_override,
            len(self._config_bytes) if self._config_bytes is not None else None,
            self._port_device, self._esc_indices,
        )
        self._set_phase("waiting_device")
        self._poll_timer.start()

    def stop(self) -> None:
        if not self._armed:
            return
        self._armed = False
        self.armed_changed.emit(False)
        self._poll_timer.stop()
        self._flog.info("=== FC flash station disarmed (was in phase %s) ===", self._phase)
        if self._phase not in DFU_PHASES:
            self._disconnect_requested.emit()
        self._set_phase("idle")

    def retry(self) -> None:
        self._flog.info("FC flash station: manual retry from phase %s", self._phase)
        if self._phase == "fc_flash_error":
            self._set_phase("waiting_device")
        elif self._phase == "error_connect":
            self._connect_attempts = 0
            self._set_phase("connecting")
            self._attempt_connect()

    def reset_counters(self) -> None:
        self._counter_flashed = 0
        self._counter_ok = 0
        self._counter_errors = 0
        self.counters_changed.emit(0, 0, 0)

    def shutdown(self) -> None:
        """Stop this station's loop/connections and both dedicated threads
        - call when the app is closing, so no thread, subprocess, or serial
        handle is left dangling."""
        self.stop()
        self._esc_worker.stop()
        self._dfu_worker_thread.stop()

    # ---- polling ------------------------------------------------

    def _on_poll_tick(self) -> None:
        if not self._armed:
            return

        if self._phase == "waiting_device":
            current_ports = list_fc_ports()
            # Kept fresh every tick up until a DFU device is actually found,
            # so it's the port landscape right before the flash starts, not
            # a stale snapshot from whenever this phase happened to begin -
            # see this file's module docstring for why this snapshot exists.
            self._pre_dfu_port_snapshot = {info.device for info in current_ports}
            normal_present = bool(self._port_device) and self._port_device in self._pre_dfu_port_snapshot
            if normal_present:
                # FC is already in normal mode - no DFU flash needed this
                # round, skip straight to the ESC-watching workflow.
                self._flog.info("FC flash station: normal-mode port %s already present - skipping DFU flash this round", self._port_device)
                self._connect_attempts = 0
                self._set_phase("connecting")
                self._attempt_connect()
                return
            if not self._dfu_op_in_flight:
                self._dfu_op_in_flight = True
                self._list_devices_requested.emit(self._resolved_cli_path())
            return

        if self._phase == "waiting_fc_normal":
            new_ports = [info for info in list_fc_ports() if info.device not in self._pre_dfu_port_snapshot]
            chosen = self._pick_reenumerated_port(new_ports)
            if chosen is not None:
                if chosen != self._port_device:
                    self._flog.info(
                        "FC flash station: FC re-enumerated under a different COM port after DFU reset: %s -> %s",
                        self._port_device, chosen,
                    )
                    self.dfu_detail_changed.emit(f"FC зявився як новий порт: {chosen} (був {self._port_device or '?'}).")
                    self._port_device = chosen
                    self.port_changed.emit(chosen)
                else:
                    self._flog.info("FC flash station: normal-mode port %s reappeared after DFU flash+reset (same name)", chosen)
                self._connect_attempts = 0
                self._set_phase("connecting")
                self._attempt_connect()
            return

        if self._phase == "connecting":
            if self._connect_in_flight:
                # Same reasoning as hotspot_station.py: EscManager.connect()
                # takes longer than one poll interval - never fire a second
                # connect while the first hasn't resolved yet.
                return
            present = bool(self._port_device) and self._port_device in {info.device for info in list_fc_ports()}
            if not present:
                self._set_phase("waiting_fc_normal")
            elif self._connect_attempts >= MAX_CONNECT_ATTEMPTS:
                self._set_phase("error_connect")
            else:
                self._attempt_connect()
            return

        if self._phase == "error_connect":
            present = bool(self._port_device) and self._port_device in {info.device for info in list_fc_ports()}
            if not present:
                self._set_phase("waiting_fc_normal")
            return

        if self._phase in WATCH_PHASES:
            present = bool(self._port_device) and self._port_device in {info.device for info in list_fc_ports()}
            if present:
                self._tick_seen_states = {}
                self._discover_many_requested.emit(list(self._esc_indices))
            else:
                self._disconnect_requested.emit()
                self._set_phase("waiting_fc_normal")
            return
        # "flashing_fc"/"resetting_fc"/"flashing"/"fc_flash_error": no
        # polling action, waits for the relevant *_done signal or a manual retry().

    def _resolved_cli_path(self) -> str:
        path = dfu_flash_manager.find_cli_path(self._cli_path_override)
        return str(path) if path is not None else ""

    def _pick_reenumerated_port(self, candidates: list[PortInfo]) -> Optional[str]:
        if len(candidates) > 1:
            self._flog.warning(
                "FC flash station: %d new port(s) appeared after DFU reset (ambiguous) - picking the most likely one: %s",
                len(candidates), [c.device for c in candidates],
            )
        return choose_reenumerated_port(candidates)

    def _attempt_connect(self) -> None:
        if not self._port_device:
            self._set_phase("error_connect")
            return
        self._connect_attempts += 1
        self._connect_in_flight = True
        self._connect_requested.emit(self._port_device, DEFAULT_BAUD)

    def _set_phase(self, phase: str) -> None:
        if phase != self._phase:
            self._flog.info("FC flash station: phase %s -> %s", self._phase, phase)
        self._phase = phase
        self.phase_changed.emit(phase)

    # ---- DFU-phase callbacks ------------------------------------------------

    def _on_devices_listed(self, devices: list) -> None:
        self._dfu_op_in_flight = False
        if not self._armed or self._phase != "waiting_device":
            return
        self._flog.info("FC flash station: `-l` tick -> %d DFU device(s)", len(devices))
        if not devices:
            return
        if not self._firmware_path:
            self._flog.warning("FC flash station: DFU device present but no firmware file selected - not flashing.")
            self.dfu_detail_changed.emit("DFU-пристрій знайдено, але файл прошивки FC не обрано.")
            return
        device = devices[0]
        self._dfu_port_id = device.port_id
        self._flog.info(
            "FC flash station: DFU device selected: %s (port_id=%s) - starting flash of %s",
            device.raw_line, device.port_id, self._firmware_path,
        )
        self.dfu_detail_changed.emit(f"DFU-пристрій знайдено ({device.raw_line}). Прошиваю...")
        self._dfu_op_in_flight = True
        self._set_phase("flashing_fc")
        self._flash_fc_requested.emit(self._resolved_cli_path(), device.port_id, self._firmware_path)

    def _on_flash_fc_done(self, success: bool, message: str) -> None:
        self._dfu_op_in_flight = False
        if not self._armed or self._phase != "flashing_fc":
            return
        self._flog.info("FC flash station: flash_and_verify done: success=%s, message=%s", success, message)
        self.dfu_detail_changed.emit(message)
        if not success:
            self._set_phase("fc_flash_error")
            return
        self._dfu_op_in_flight = True
        self._set_phase("resetting_fc")
        self._reset_fc_requested.emit(self._resolved_cli_path(), self._dfu_port_id or "usb1")

    def _on_reset_fc_done(self, success: bool) -> None:
        self._dfu_op_in_flight = False
        if not self._armed or self._phase != "resetting_fc":
            return
        self._flog.info("FC flash station: reset_to_app done: success=%s", success)
        if not success:
            self.dfu_detail_changed.emit("Не вдалось перезавантажити FC після прошивки.")
            self._set_phase("fc_flash_error")
            return
        self.dfu_detail_changed.emit("FC перезавантажується у звичайний режим...")
        self._set_phase("waiting_fc_normal")

    # ---- forwarded EscManager signals (identical logic to hotspot_station.py) ---

    def _on_connection_state_changed(self, connected: bool, expected_count: int) -> None:
        self._connect_in_flight = False
        if not self._armed or self._phase != "connecting":
            return
        if connected:
            self._last_esc_states = {}
            self._tick_seen_states = {}
            for i in self._esc_indices:
                self.esc_detail_changed.emit(i, "idle", "")
            self._poll_timer.start()
            self._set_phase("watching")

    def _on_discovery_finished(self, found: int, expected: int) -> None:
        if not self._armed or self._phase not in WATCH_PHASES:
            return
        if found == 0:
            self._consecutive_not_found += 1
            if self._consecutive_not_found == EMPTY_PROBES_BEFORE_POWER_CYCLE_HINT:
                self.power_cycle_hint_changed.emit(True)
        elif self._consecutive_not_found > 0:
            self._consecutive_not_found = 0
            self.power_cycle_hint_changed.emit(False)
        was_watching = self._phase == "watching"
        next_phase, need_flash = phase_after_discovery_many(self._tick_seen_states, self._esc_indices)
        if next_phase == "watching":
            if not was_watching:
                for i in self._esc_indices:
                    self.esc_detail_changed.emit(i, "idle", "")
            self._set_phase("watching")
            return
        if need_flash:
            self._set_phase("flashing")
            self._flash_requested.emit(self._config_bytes, need_flash)
            return
        if self._phase != "esc_ready":
            self._counter_ok += 1
            self.counters_changed.emit(self._counter_flashed, self._counter_ok, self._counter_errors)
        self._set_phase("esc_ready")

    def _on_flash_finished(self, success: int, attempted: int) -> None:
        if not self._armed or self._phase != "flashing":
            return
        # Per-ESC counts, not per-batch: one flash_bytes() call here can
        # cover several indices at once (e.g. both halves of a 2-in-1 ESC
        # board), unlike hotspot_station.py's always-exactly-one-slot flash.
        self._counter_flashed += success
        self._counter_errors += max(attempted - success, 0)
        self.counters_changed.emit(self._counter_flashed, self._counter_ok, self._counter_errors)
        self._set_phase("esc_ready" if success == attempted and attempted > 0 else "esc_error")

    def _on_esc_status_changed(self, index: int, state: str, detail: str, esc_info) -> None:
        if not self._armed or index not in self._esc_indices:
            return
        self._last_esc_states[index] = state
        self._tick_seen_states[index] = state
        self.esc_detail_changed.emit(index, state, detail)
