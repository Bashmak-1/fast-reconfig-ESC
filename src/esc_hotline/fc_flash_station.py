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
motor_ramp.MOTOR_PROTOCOLS) is wired for real: once every watched ESC
slot is confirmed flashed/matching, this controller automatically exits
the FourWay/ESC world, opens a plain-MSP connection on the same port, and
writes the selected protocol via MSP_SET_ADVANCED_CONFIG ->
MSP_EEPROM_WRITE -> MSP_REBOOT - the exact "Save and Reboot" dance
motor_test_manager.py's own set_protocol() already does, run here by a
dedicated _ProtocolWorker/_ProtocolWorkerThread (same isolated-worker
pattern as _DfuWorker/_DfuWorkerThread below). This turns the ESC-watching
phase from an open-ended hot-swap loop (like "Хотспот (лінія)") into a
ONE-SHOT terminal step for this tab specifically: the moment every
watched slot is confirmed good, it moves on to the protocol write and
then stops, rather than looping back to watch for a swap.

Confirmed working end-to-end on real hardware (2026-10): DFU detection,
flash+verify, exiting DFU (see dfu_flash_manager.reset_to_app - "-g
<address>", not "-rst"), ESC flashing, and the protocol write+reboot all
completed successfully through this tab.

Two real-world operator-reported gaps, both fixed the same way - watching
for DFU is no longer confined to a single "waiting_device" phase that's
only ever visited once per run:
- Forgetting to hold the board's DFU button: this tab finds the FC
  already answering in normal mode (no DFU device yet) and moves straight
  to "connecting"/"watching" - if the operator notices within moments and
  power-cycles the board correctly into DFU mode, the disconnect this
  causes lands in "waiting_fc_normal", which now ALSO watches for DFU
  (not just normal-mode reappearance), so the correction is still caught.
- Flashing several boards in a row: once a board reaches "all_done" (see
  _on_poll_tick's own handling there), this controller waits for THAT
  board to physically disappear, a short settle delay, then automatically
  goes back to watching for the next one - no manual Стоп/Старт needed
  between boards.

After the protocol reboot, this controller goes one step further: it
waits for the FC to come back up under its new protocol (same
snapshot/new-port-detection trick as the DFU reset above), connects to
it with a plain MSP connection, and automatically runs a motor spin test
- reusing MotorTestManager (the exact class "Прогон мотором" itself
uses) as a private worker on its own thread, with whatever ramp/test-mode
settings the operator already has configured on that tab (read fresh
from settings_store at the moment each one is needed, never cached here
- this tab doesn't duplicate those controls, it just reuses them). This
is the one place in this file that doesn't follow the "every workflow
owns its own state machine" convention: a live motor spin test has
enough safety-critical logic (disarm check, bounded ramp, panic stop)
that re-running it exactly as-is, instead of reimplementing it a second
time, is the safer choice.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from . import dfu_flash_manager
from .connection_worker import ConnectionWorker
from .file_logger import get_logger
from .fourway import FourWayClient
from .i18n import tr
from .motor_ramp import MOTOR_PROTOCOLS, build_advanced_config_payload
from .ramp_curve import DOWN_CURVE_KEY, TICK_KEY, UP_CURVE_KEY, RampCurve
from .motor_test_manager import ARMED_BIT, PHASE_IDLE as MOTOR_PHASE_IDLE, RAMP_BEZIER, RAMP_LINEAR, TEST_MODE_ALL, MotorTestManager
from .msp import MspClient, MspCommand, MspError
from .serial_transport import PortInfo, SerialTransport, list_fc_ports
from .settings_store import load_int, load_str
from .theme import ACCENT_AMBER, ACCENT_ERROR, ACCENT_GREEN, BG_PANEL, TEXT_MAIN, TEXT_MUTED

# Reuses "Прогон мотором"'s own persisted settings (motor_test_widget.py) -
# same keys, read fresh here each time rather than cached, deliberately not
# re-exposed as new controls on this tab (see module docstring above).
_MOTOR_UP_DURATION_KEY = "motor_test_up_duration_s"
_MOTOR_HOLD_DURATION_KEY = "motor_test_hold_duration_s"
_MOTOR_DOWN_DURATION_KEY = "motor_test_down_duration_s"
_MOTOR_RAMP_MODE_KEY = "motor_test_ramp_mode"
_MOTOR_TEST_MODE_KEY = "motor_test_mode"
_MOTOR_MIN_BOUND_KEY = "motor_test_min_percent"
_MOTOR_MAX_BOUND_KEY = "motor_test_max_percent"
DEFAULT_MOTOR_UP_DURATION_S = 4
DEFAULT_MOTOR_HOLD_DURATION_S = 0
DEFAULT_MOTOR_DOWN_DURATION_S = 4
DEFAULT_MOTOR_MIN_BOUND = 0
DEFAULT_MOTOR_MAX_BOUND = 100

DEFAULT_BAUD = 115200
MOTOR_CONNECT_ATTEMPTS = 5  # motor-test connect tries (1.5 s apart) before giving up
# A freshly DFU-flashed FC needs a while before its USB-CDC port answers (real logs: Write timeout,
# then 'Access is denied' for ~10s+), so give it ~a minute of 1s polls instead of giving up early.
MAX_CONNECT_ATTEMPTS = 20
POLL_INTERVAL_MS = 1000
# Operator-adjustable via FcFlashStationController.set_poll_interval_ms() -
# same values/rationale as hotspot_station.py's own constants: a
# freshly-powered-up ESC can take a few seconds to boot, and re-probing it
# too eagerly while it's still booting risks wedging a BLHeli/AM32
# bootloader until a full power cycle (see docs/PROTOCOL_NOTES.md,
# "Known gotchas"). Duplicated here rather than imported, same as
# EMPTY_PROBES_BEFORE_POWER_CYCLE_HINT below - this project's convention
# is separate workflows own their own tuning constants.
MIN_POLL_INTERVAL_MS = 1000
MAX_POLL_INTERVAL_MS = 10_000
POLL_INTERVAL_STEP_MS = 100
EMPTY_PROBES_BEFORE_POWER_CYCLE_HINT = 20  # same rationale as hotspot_station.py's own constant
# How long to wait, once the just-finished board has physically disappeared
# at "all_done", before starting to watch for the next one - a port can
# bounce (briefly vanish then reappear) right at power-down, so this is not
# just cosmetic: starting the search mid-bounce could latch onto the same
# board's own disappearing act as "ready", then immediately find nothing.
NEXT_BOARD_SETTLE_DELAY_S = 2.0

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
RETRY_PHASES = {"error_connect", "fc_flash_error", "protocol_error", "motor_test_error"}
# Phases where a motor-test worker call (or its reply) is in flight - used by
# stop()/shutdown() to know when _motor_disconnect_requested is also needed.
MOTOR_TEST_PHASES = {"waiting_fc_for_motor_test", "connecting_motor_test", "checking_motor_armed", "running_motor_test", "motor_test_error", "all_done"}

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
    "esc_ready": (ACCENT_AMBER, "white"),  # intermediate: only some ESCs done; green is reserved for the true end
    "esc_error": (ACCENT_ERROR, "white"),
    "error_connect": (ACCENT_ERROR, "white"),
    "writing_protocol": (ACCENT_AMBER, "white"),
    "waiting_fc_for_motor_test": (BG_PANEL, TEXT_MAIN),
    "waiting_fc_after_protocol": (ACCENT_AMBER, "white"),
    "connecting_motor_test": (ACCENT_AMBER, "white"),
    "checking_motor_armed": (ACCENT_AMBER, "white"),
    "running_motor_test": (ACCENT_AMBER, "white"),
    "all_done": (ACCENT_GREEN, "white"),
    "motor_test_error": (ACCENT_ERROR, "white"),
    "protocol_error": (ACCENT_ERROR, "white"),
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
    "esc_ready": "ESC прошито - чекаю решту...",
    "esc_error": "Помилка прошивки ESC",
    "error_connect": "Помилка підключення",
    "writing_protocol": "Запис протоколу й перезавантаження FC...",
    "waiting_fc_for_motor_test": "Очікування перезавантаження FC після зміни протоколу...",
    "waiting_fc_after_protocol": "Протокол записано, FC перезавантажується...",
    "connecting_motor_test": "Підключення для тесту моторів...",
    "checking_motor_armed": "Перевірка стану FC (має бути disarmed)...",
    "running_motor_test": "Тест моторів...",
    "all_done": "Готово! FC і ESC прошиті, протокол встановлено.",
    "motor_test_error": "Помилка тесту моторів",
    "protocol_error": "Помилка запису протоколу",
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
    flash_progress = pyqtSignal(str, int)  # stage ("write"/"verify"), percent - see dfu_flash_manager.flash_and_verify
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
        # Emitting a Qt signal from this worker thread is safe - PyQt
        # auto-queues delivery to whatever thread the connected slot lives
        # on (here, the GUI-thread controller), same as every other
        # cross-thread signal in this file.
        result = dfu_flash_manager.flash_and_verify(
            Path(cli_path), port_id, firmware_path, progress_callback=self.flash_progress.emit
        )
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


class _ProtocolWorker(QObject):
    """Lives on its own QThread (see _ProtocolWorkerThread) - runs the
    blocking plain-MSP write-protocol-and-reboot dance off the GUI thread,
    once the ESC phase is fully done and FourWay/passthrough has already
    been confirmed exited (EscManager.disconnect() already completed by
    the time this is ever called - see FcFlashStationController's
    _on_connection_state_changed).

    Deliberately duplicates motor_test_manager.py's connect()+
    set_protocol() CONNECTION dance (not the byte-packing itself, which is
    the shared motor_ramp.build_advanced_config_payload()) rather than
    importing MotorTestManager - this project's convention (see
    hotspot_station.py's docstring) is that similar-but-separate
    workflows each own their own state/orchestration, so a change meant
    for the live spin-test tab's safety checks can never accidentally
    reach this one-shot provisioning step, or vice versa.
    """

    done = pyqtSignal(bool, str, bool)  # success, message, rebooted (False = the FC was left running as is)

    def write_protocol_and_reboot(self, port: str, baud: int, protocol_index: int, allow_write: bool = True) -> None:
        flog = get_logger()
        if not port:
            flog.error("Protocol write: no port given.")
            self.done.emit(False, "Немає порту для запису протоколу.", False)
            return
        transport = None
        last_error: Optional[Exception] = None
        for attempt in range(10):  # the port can still be held for a moment right after FourWay disconnect
            try:
                transport = SerialTransport(port, baud)
                break
            except Exception as e:
                last_error = e
                flog.warning("Protocol write: open %s failed (attempt %d/10): %s", port, attempt + 1, e)
                time.sleep(0.5)
        if transport is None:
            flog.error("Protocol write: could not open %s: %s", port, last_error)
            self.done.emit(False, f"Не вдалось відкрити {port}: {last_error}", False)
            return

        msp = MspClient(transport)
        try:
            try:
                msp.send(MspCommand.MSP_API_VERSION)
            except MspError:
                # FC may still be latched in FourWay-relay mode right after
                # EscManager.disconnect() - same recovery esc_manager.py/
                # motor_test_manager.py already use for the same situation.
                flog.warning("Protocol write: MSP_API_VERSION failed, trying FourWayClient.exit_interface() recovery")
                FourWayClient(transport).exit_interface()
                time.sleep(1.0)
                msp.send(MspCommand.MSP_API_VERSION)

            adv_resp = msp.send(MspCommand.MSP_ADVANCED_CONFIG, timeout_s=0.3, retries=3)
            if len(adv_resp.payload) < 8:
                raise MspError(f"занадто коротка відповідь MSP_ADVANCED_CONFIG ({len(adv_resp.payload)} байт)")
            motor_pwm_rate = int.from_bytes(adv_resp.payload[4:6], "little")
            servo_pwm_rate = int.from_bytes(adv_resp.payload[6:8], "little")
            cur_idx = adv_resp.payload[3]
            flog.info(
                "Protocol write: current protocol=%s (index=%d), motor_pwm_rate=%d, servo_pwm_rate=%d (rates echoed back unchanged)",
                MOTOR_PROTOCOLS[cur_idx] if cur_idx < len(MOTOR_PROTOCOLS) else "?", cur_idx, motor_pwm_rate, servo_pwm_rate,
            )

            name = MOTOR_PROTOCOLS[protocol_index] if 0 <= protocol_index < len(MOTOR_PROTOCOLS) else str(protocol_index)
            cur_name = MOTOR_PROTOCOLS[cur_idx] if 0 <= cur_idx < len(MOTOR_PROTOCOLS) else str(cur_idx)
            if cur_idx == protocol_index:
                # Detect first: already right -> nothing to write, and no pointless EEPROM write + reboot.
                transport.close()
                flog.info("Protocol write: FC already on %s - nothing to write, no reboot", name)
                self.done.emit(True, f"Протокол {name} вже встановлено - перезапуск не потрібен.", False)
                return
            if not allow_write:
                transport.close()
                flog.warning("Protocol write: FC is on %s, expected %s - auto-write is switched off, left as is", cur_name, name)
                self.done.emit(True, f"УВАГА: на FC протокол {cur_name}, а потрібен {name}. Автозапис вимкнено - протокол НЕ змінено.", False)
                return
            flog.info("Protocol write: wrong protocol %s (expected %s) - writing and rebooting", cur_name, name)

            status_resp = msp.send(MspCommand.MSP_STATUS_EX, timeout_s=0.5, retries=3)
            if len(status_resp.payload) < 15:
                raise MspError(f"занадто коротка відповідь MSP_STATUS_EX ({len(status_resp.payload)} байт)")
            arming_flags = int.from_bytes(status_resp.payload[13:15], "little")
            if arming_flags & ARMED_BIT:
                raise MspError("FC заармлений (ARMED) - запис протоколу заблоковано")

            payload = build_advanced_config_payload(protocol_index, motor_pwm_rate, servo_pwm_rate)
            flog.info("Protocol write: writing MSP_SET_ADVANCED_CONFIG, protocol=%s (index=%d)", name, protocol_index)
            msp.send(MspCommand.MSP_SET_ADVANCED_CONFIG, payload, timeout_s=0.3, retries=2)
            flog.info("Protocol write: saving to EEPROM (MSP_EEPROM_WRITE)...")
            # Confirmed on real hardware (2026-10-19): the FC's own EEPROM
            # commit can take longer than the previous 1.0s*2=~2.05s total
            # budget (observed failure: ~2.06s elapsed before giving up) -
            # a plain "no DFU step is a one-shot event, not a tight polling
            # loop" situation, so a generous worst case here costs nothing.
            msp.send(MspCommand.MSP_EEPROM_WRITE, timeout_s=3.0, retries=3)
            flog.info("Protocol write: rebooting FC (MSP_REBOOT)...")
            msp.send(MspCommand.MSP_REBOOT, timeout_s=0.3, retries=1)
        except Exception as e:
            flog.error("Protocol write failed: %s", e)
            transport.close()
            self.done.emit(False, f"Не вдалось записати протокол: {e}", False)
            return

        transport.close()
        flog.info("Protocol write: done successfully (%s)", name)
        self.done.emit(True, f"Був неправильний протокол {cur_name} - записано {name}. FC перезавантажується...", True)


class _ProtocolWorkerThread:
    """Mirrors _DfuWorkerThread's own moveToThread pattern."""

    def __init__(self) -> None:
        self.thread = QThread()
        self.thread.setObjectName("ProtocolWorkerThread")
        self.worker = _ProtocolWorker()
        self.worker.moveToThread(self.thread)
        self.thread.start()

    def stop(self) -> None:
        self.thread.quit()
        self.thread.wait(2000)


class _MotorTestWorkerThread:
    """Mirrors _DfuWorkerThread's own moveToThread pattern, but around a
    real MotorTestManager instance - deliberately reused as-is (see module
    docstring) rather than duplicated."""

    def __init__(self) -> None:
        self.thread = QThread()
        self.thread.setObjectName("FcFlashMotorTestThread")
        self.manager = MotorTestManager()
        self.manager.moveToThread(self.thread)
        self.thread.start()

    def stop(self) -> None:
        self.thread.quit()
        self.thread.wait(2000)


class FcFlashStationController(QObject):
    """Owns a private ConnectionWorker (EscManager + its own QThread, for
    the ESC phase), a private _DfuWorkerThread (for the DFU-flash phase),
    a private _ProtocolWorkerThread (for the protocol-write phase), and a
    private MotorTestManager (for the final motor-test phase), and drives
    the combined loop: idle -> waiting_device -> [flashing_fc ->
    resetting_fc ->] waiting_fc_normal -> connecting -> watching ->
    (right after a DFU flash: connecting -> writing_protocol -> waiting_fc_after_protocol -> connecting, so the
    FC never stays on the default STANDARD protocol if an ESC later fails to answer)
    [flashing ->] esc_ready -> writing_protocol -> waiting_fc_for_motor_test
    -> connecting_motor_test -> checking_motor_armed -> running_motor_test
    -> all_done (or one of several *_error phases along the way).

    Unlike hotspot_station.py, "esc_ready" here is NOT a steady state to
    loop on - the moment every watched ESC slot is confirmed good for the
    first time, this controller moves straight on to writing the FC's
    motor protocol and rebooting it, then on to the motor test, then stops
    (a one-shot provisioning run for one board, not an open-ended hot-swap
    line).

    Three independent operator toggles (set_flash_fc_enabled()/
    set_flash_esc_enabled()/set_motor_test_enabled(), all default True)
    let a run skip stages that aren't needed this time, in any
    combination:
    - flash_fc=False: start() skips straight to "waiting_fc_normal" - no
      DFU search at all, just auto-detect the FC already in normal mode.
    - flash_esc=False: once connected, skips the whole watching/flashing
      ESC loop - straight from "connecting" to "writing_protocol" (see
      _on_connection_state_changed) - UNLESS motor_test is ALSO False, in
      which case there's nothing left to do at all and this goes straight
      to "all_done". The protocol write itself is NOT skipped just
      because ESC-flash is off: confirmed in practice that an ESC on the
      wrong motor protocol won't even initialize/arm, so motor_test=True
      always needs a correct protocol on the FC first, regardless of
      whether this run also touches the ESC's own EEPROM settings.
    - motor_test=False: the automatic motor-spin step itself is skipped -
      whatever earlier stages ran still happen as configured, this run
      just stops at "all_done" right after the last of them (right after
      protocol write succeeds, or right after connecting if ESC is also
      disabled) instead of connecting again and spinning the motors.
    - All False: this run is purely "confirm the FC connects", nothing
      else.
    """

    phase_changed = pyqtSignal(str)
    esc_detail_changed = pyqtSignal(int, str, str)  # index, EscState.value, detail text
    dfu_detail_changed = pyqtSignal(str)
    counters_changed = pyqtSignal(int, int, int)  # flashed, ok, errors
    armed_changed = pyqtSignal(bool)
    power_cycle_hint_changed = pyqtSignal(bool)
    port_changed = pyqtSignal(str)  # FC re-enumerated under a different COM port after a DFU reset
    dfu_flash_progress_changed = pyqtSignal(str, int)  # stage ("write"/"verify"), percent - see dfu_flash_manager.py

    # Fired exactly once per board each time its stage finishes
    # successfully - never on failure, never on a skipped (checkbox-off)
    # stage, since each is only ever emitted from the one code path where
    # that specific stage just succeeded. The widget's own per-stage
    # ManualCounterWidget (see fc_flash_widget.py, same widget hotspot's
    # "Всього перероблено" tally uses) listens for these and does the
    # +1 - no checkbox check needed on that end, since a disabled stage's
    # code path is never reached in the first place.
    fc_flash_succeeded = pyqtSignal()
    esc_flash_succeeded = pyqtSignal()
    motor_test_succeeded = pyqtSignal()

    # Raw forwards from the private MotorTestManager, for an optional
    # read-only RampChartWidget on this tab's own widget - mirrors exactly
    # what motor_test_widget.py's own chart is fed.
    motor_test_phase_changed = pyqtSignal(str)  # PHASE_IDLE/PHASE_UP/PHASE_HOLD/PHASE_DOWN
    motor_test_value_changed = pyqtSignal(list)  # raw MSP values, one per real motor slot

    # Internal-only: reach the private EscManager, which lives on another
    # thread - never call its methods directly, only through these signals.
    _connect_requested = pyqtSignal(str, int)
    _disconnect_requested = pyqtSignal()
    _discover_many_requested = pyqtSignal(list)
    _flash_requested = pyqtSignal(bytes, object)
    _set_config_path_requested = pyqtSignal(str)

    # Internal-only: reach the private _ProtocolWorker.
    _write_protocol_requested = pyqtSignal(str, int, int, bool)  # port, baud, protocol_index, allow_write

    # Internal-only: reach the private _DfuWorker.
    _list_devices_requested = pyqtSignal(str)
    _flash_fc_requested = pyqtSignal(str, str, str)
    _reset_fc_requested = pyqtSignal(str, str)

    # Internal-only: reach the private MotorTestManager (final motor-test step).
    _motor_connect_requested = pyqtSignal(str, int)
    _motor_disconnect_requested = pyqtSignal()
    _motor_check_disarmed_requested = pyqtSignal()
    _motor_start_sweep_requested = pyqtSignal(float, float, float)
    _motor_set_ramp_mode_requested = pyqtSignal(str)
    _motor_set_curves_requested = pyqtSignal(object, object)
    _motor_set_tick_requested = pyqtSignal(int)
    _motor_set_test_mode_requested = pyqtSignal(str)
    _motor_set_speed_bounds_requested = pyqtSignal(float, float)

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
        dfu_worker.flash_progress.connect(self.dfu_flash_progress_changed.emit)
        dfu_worker.reset_done.connect(self._on_reset_fc_done)

        self._protocol_worker_thread = _ProtocolWorkerThread()
        self._write_protocol_requested.connect(self._protocol_worker_thread.worker.write_protocol_and_reboot)
        self._protocol_worker_thread.worker.done.connect(self._on_protocol_write_done)

        self._motor_test_worker_thread = _MotorTestWorkerThread()
        motor_mgr = self._motor_test_worker_thread.manager
        self._motor_connect_requested.connect(motor_mgr.connect)
        self.dfu_detail_changed.connect(lambda text: self._flog.info("FC-tab message: %s", text))  # whatever the operator sees, the log keeps
        self._motor_disconnect_requested.connect(motor_mgr.disconnect)
        self._motor_check_disarmed_requested.connect(motor_mgr.check_disarmed)
        self._motor_start_sweep_requested.connect(motor_mgr.start_sweep)
        self._motor_set_ramp_mode_requested.connect(motor_mgr.set_ramp_mode)
        self._motor_set_curves_requested.connect(motor_mgr.set_curves)
        self._motor_set_tick_requested.connect(motor_mgr.set_tick_ms)
        self._motor_set_test_mode_requested.connect(motor_mgr.set_test_mode)
        self._motor_set_speed_bounds_requested.connect(motor_mgr.set_speed_bounds)
        motor_mgr.connection_state_changed.connect(self._on_motor_connection_state_changed)
        motor_mgr.protocol_detected.connect(self._on_motor_protocol_detected)
        motor_mgr.battery_voltage.connect(self._on_motor_battery_voltage)
        motor_mgr.motor_count_detected.connect(self._on_motor_count_detected)
        motor_mgr.armed_check_result.connect(self._on_motor_armed_check_result)
        motor_mgr.phase_changed.connect(self._on_motor_phase_changed)
        motor_mgr.phase_changed.connect(self.motor_test_phase_changed.emit)
        motor_mgr.value_changed.connect(self.motor_test_value_changed.emit)
        motor_mgr.log_message.connect(self._on_motor_log_message)

        self._firmware_path: Optional[str] = None
        self._cli_path_override: Optional[str] = None
        self._config_bytes: Optional[bytes] = None
        self._port_device: Optional[str] = None
        self._flash_fc_enabled = True
        self._flash_esc_enabled = True
        self._motor_test_enabled = True
        self._esc_indices: list[int] = [0]
        self._protocol_index: int = MOTOR_PROTOCOLS.index("DSHOT300")
        self._fc_flashed_no_protocol = False
        self._protocol_stage_early = False  # the protocol write now running is the pre-ESC one
        self._write_protocol_enabled = True  # operator checkbox: fix a wrong protocol (write + reboot) or only report it
        self._motor_detected_protocol = -1  # what the motor-test connection read from the FC (-1 = unknown)
        self._motor_battery_v = -1.0
        self._motor_connect_attempts = 0
        self._after_protocol_since = 0.0
        self._flashed_indices: set[int] = set()  # ESCs flashed on the board currently in place (for the status lines)
        self._flashing_indices: list[int] = []  # what the running flash batch covers (for the banner)
        self._last_esc_states: dict[int, str] = {}
        self._tick_seen_states: dict[int, str] = {}
        self._armed = False
        self._phase = "idle"
        self._connect_attempts = 0
        self._connect_in_flight = False
        self._dfu_op_in_flight = False
        self._dfu_port_id: Optional[str] = None
        self._dfu_poll_count = 0  # how many `-l usb` ticks since armed - visible proof the search loop is actually running
        self._pre_dfu_port_snapshot: set[str] = set()
        self._fc_gone_since: Optional[float] = None
        self._motor_count_for_test = 0
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

    def poll_interval_ms(self) -> int:
        return self._poll_timer.interval()

    def set_poll_interval_ms(self, value: int) -> None:
        """How often to re-probe the watched ESC slot(s) - safe to call at
        any time, including while armed (QTimer.setInterval() on an
        already-running timer restarts its remaining-time countdown from
        the new value immediately, same as hotspot_station.py's own
        identical method)."""
        clamped = max(MIN_POLL_INTERVAL_MS, min(int(value), MAX_POLL_INTERVAL_MS))
        self._poll_timer.setInterval(clamped)

    def set_firmware_path(self, path: Optional[str]) -> None:
        self._firmware_path = path or None

    def set_cli_path_override(self, path: Optional[str]) -> None:
        self._cli_path_override = path or None

    def set_port(self, device: Optional[str]) -> None:
        self._port_device = device

    def set_flash_fc_enabled(self, enabled: bool) -> None:
        """Operator toggle: when False, the DFU search/flash/reset phases
        are skipped entirely and start() goes straight to
        "waiting_fc_normal" - the FC is assumed to already be in normal
        mode (e.g. a previous run already flashed it), and that phase's
        existing re-enumeration-tolerant auto-detect (see this file's
        module docstring) finds/confirms it just the same as it would
        after a real DFU reset."""
        self._flash_fc_enabled = enabled

    def set_flash_esc_enabled(self, enabled: bool) -> None:
        """Operator toggle: when False, the ESC-watch/flash loop AND the
        protocol write that normally follows it are both skipped - once
        connected to the FC, this goes straight to the final motor-test
        phase (see _on_connection_state_changed)."""
        self._flash_esc_enabled = enabled

    def set_motor_test_enabled(self, enabled: bool) -> None:
        """Operator toggle: when False, the final automatic motor-spin
        step is skipped entirely - whatever earlier stages ran (DFU flash,
        ESC flash, protocol write) still happen as configured, this run
        just stops at "all_done" right after the last of them instead of
        going on to connect and spin the motors."""
        self._motor_test_enabled = enabled

    def set_esc_indices(self, indices: list[int]) -> None:
        self._esc_indices = list(indices)

    def set_protocol_index(self, index: int) -> None:
        self._protocol_index = index

    def set_write_protocol_enabled(self, enabled: bool) -> None:
        """True: a wrong FC protocol is fixed (write + reboot). False: it is only detected and reported;
        the FC is never rewritten or rebooted by this tool."""
        self._write_protocol_enabled = enabled

    def set_config(self, path: str, config_bytes: bytes) -> None:
        self._config_bytes = config_bytes
        self._set_config_path_requested.emit(path)

    def start(self) -> None:
        if self._armed:
            return
        self._armed = True
        self.armed_changed.emit(True)
        self._connect_attempts = 0
        self._fc_flashed_no_protocol = False
        self._protocol_stage_early = False
        self._connect_in_flight = False
        self._dfu_op_in_flight = False
        self._dfu_port_id = None
        self._dfu_poll_count = 0
        self._pre_dfu_port_snapshot = set()
        self._fc_gone_since = None
        self._motor_count_for_test = 0
        self._last_esc_states = {}
        self._tick_seen_states = {}
        self._consecutive_not_found = 0
        self.power_cycle_hint_changed.emit(False)
        self.dfu_detail_changed.emit("")
        self._flog.info(
            "=== FC flash station armed: firmware=%s, cli_override=%s, esc_config_bytes=%s, port=%s, esc_indices=%s, "
            "protocol_index=%d, flash_fc_enabled=%s, flash_esc_enabled=%s, motor_test_enabled=%s ===",
            self._firmware_path, self._cli_path_override,
            len(self._config_bytes) if self._config_bytes is not None else None,
            self._port_device, self._esc_indices, self._protocol_index,
            self._flash_fc_enabled, self._flash_esc_enabled, self._motor_test_enabled,
        )
        if self._flash_fc_enabled:
            self._set_phase("waiting_device")
        else:
            # Skip the DFU phase entirely - straight to "waiting_fc_normal"
            # with an empty snapshot, so EVERY currently-present port
            # counts as a fresh candidate for its own re-enumeration-
            # tolerant auto-detect (choose_reenumerated_port() already
            # prefers a likely_fc-flagged port over an unrelated one) -
            # the operator's remembered port name may well be stale after
            # an earlier DFU cycle (see module docstring), so this is not
            # just "trust the dropdown".
            self._flog.info("FC flash station: FC-flash stage disabled by operator - skipping DFU, auto-detecting normal-mode FC")
            self._set_phase("waiting_fc_normal")
        self._poll_timer.start()

    def stop(self) -> None:
        if not self._armed:
            return
        self._armed = False
        self.armed_changed.emit(False)
        self._poll_timer.stop()
        self._flog.info("=== FC flash station disarmed (was in phase %s) ===", self._phase)
        if self._fc_flashed_no_protocol:
            self._flog.warning("FC was flashed but the motor protocol was NOT written - FC is left on its default (STANDARD) protocol")
            self.dfu_detail_changed.emit("УВАГА: FC прошито, але протокол моторів НЕ записано - FC на стандартному протоколі!")
            self._fc_flashed_no_protocol = False
        if self._phase not in DFU_PHASES and self._phase not in MOTOR_TEST_PHASES:
            self._disconnect_requested.emit()
        if self._phase in MOTOR_TEST_PHASES:
            self._motor_disconnect_requested.emit()
        self._set_phase("idle")

    def retry(self) -> None:
        self._flog.info("FC flash station: manual retry from phase %s", self._phase)
        if self._phase == "fc_flash_error":
            self._set_phase("waiting_device")
        elif self._phase == "error_connect":
            self._connect_attempts = 0
            self._set_phase("connecting")
            self._attempt_connect()
        elif self._phase == "protocol_error":
            self._begin_protocol_write()
        elif self._phase == "motor_test_error":
            self._set_phase("connecting_motor_test")
            self._begin_motor_connect()

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
        self._protocol_worker_thread.stop()
        self._motor_test_worker_thread.stop()

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
            # IMPORTANT, confirmed on real hardware: a `-l usb` check is
            # dispatched on EVERY tick here, even when the operator's
            # remembered port name already happens to be present - that
            # "normal_present" shortcut used to short-circuit straight to
            # "connecting" before ever checking for DFU at all, which is
            # wrong whenever the remembered port is stale (e.g. left over
            # from an earlier session, while the real FC has since
            # re-enumerated under a completely different, unrelated COM
            # number after repeated DFU cycles) or simply coincides with
            # some OTHER, unrelated serial device that happens to share
            # that exact port name right now. The actual FC genuinely
            # sitting in DFU mode is a far stronger signal than "a port
            # with this name exists" and must always win - see
            # _on_devices_listed(), where the "already normal" fallback now
            # only applies once a tick's `-l usb` has confirmed no DFU
            # device is present.
            if not self._dfu_op_in_flight:
                cli_path = self._resolved_cli_path()
                if not cli_path:
                    # A clearly different message from "DFU not found yet" -
                    # this means the search can never succeed until the
                    # path is fixed, not just "keep waiting".
                    self._dfu_poll_count += 1
                    self.dfu_detail_changed.emit(
                        f"STM32_Programmer_CLI.exe не знайдено на диску (спроба {self._dfu_poll_count}) - "
                        "вкажіть шлях вручну в налаштуваннях."
                    )
                    return
                self._dfu_op_in_flight = True
                self._list_devices_requested.emit(cli_path)
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
            # ALSO keep watching for DFU here, not just normal-mode
            # reappearance (confirmed real-world need: the operator can
            # forget to hold the DFU button, notice within milliseconds
            # that this tab went straight for FourWay instead of a DFU
            # search, and power-cycle the board to correct it - by then
            # this phase has already moved on from "waiting_device" and
            # would otherwise never look for DFU again. Same mechanism
            # also covers unplugging this board and plugging in the NEXT
            # one already in DFU mode, see "all_done"'s own handling below).
            if self._flash_fc_enabled and not self._dfu_op_in_flight:
                cli_path = self._resolved_cli_path()
                if cli_path:
                    self._dfu_op_in_flight = True
                    self._list_devices_requested.emit(cli_path)
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

        if self._phase == "waiting_fc_after_protocol":
            if time.monotonic() - self._after_protocol_since < 3.0:
                return  # let the FC actually go down first, or we would reconnect to the dying session
            current_ports = list_fc_ports()
            if self._port_device and self._port_device in {info.device for info in current_ports}:
                chosen: Optional[str] = self._port_device
            else:
                chosen = self._pick_reenumerated_port(
                    [info for info in current_ports if info.device not in self._pre_dfu_port_snapshot]
                )
            if chosen is not None:
                if chosen != self._port_device:
                    self._flog.info("FC flash station: FC re-enumerated after protocol reboot: %s -> %s", self._port_device, chosen)
                    self._port_device = chosen
                    self.port_changed.emit(chosen)
                self._connect_attempts = 0
                self._set_phase("connecting")
            return

        if self._phase == "waiting_fc_for_motor_test":
            if time.monotonic() - self._after_protocol_since < 3.0:
                return  # real log: connecting 150 ms after MSP_REBOOT reached the still-dying FC and failed
            # NOT the same re-enumeration trick as "waiting_fc_normal" -
            # that one relies on the old port already being gone from
            # list_fc_ports() by snapshot time (DFU bootloader mode drops
            # the VCP entirely). A plain MSP_REBOOT gives no such
            # guarantee: confirmed on real hardware that the exact same
            # COM port stays enumerated by Windows the whole time, since
            # our own transport.close() only releases OUR handle, not the
            # USB device itself. Filtering it out via _pre_dfu_port_snapshot
            # (taken WHILE that port was still open, right before the
            # reboot) left this phase waiting forever even though the FC
            # had already rebooted and reappeared under the same name -
            # so check for that port specifically before falling back to
            # the stricter "genuinely new port" detection.
            current_ports = list_fc_ports()
            current_devices = {info.device for info in current_ports}
            if self._port_device and self._port_device in current_devices:
                chosen: Optional[str] = self._port_device
            else:
                new_ports = [info for info in current_ports if info.device not in self._pre_dfu_port_snapshot]
                chosen = self._pick_reenumerated_port(new_ports)
            if chosen is not None:
                if chosen != self._port_device:
                    self._flog.info(
                        "FC flash station: FC re-enumerated after protocol reboot: %s -> %s",
                        self._port_device, chosen,
                    )
                    self._port_device = chosen
                    self.port_changed.emit(chosen)
                self._set_phase("connecting_motor_test")
                self._begin_motor_connect()
            return

        if self._phase == "all_done":
            # Automatic "ready for the next board" reset - confirmed real
            # need: without this, the operator had to manually press
            # Стоп then Старт between every single board. Waits for the
            # JUST-flashed FC to physically disappear (powered off/
            # unplugged) first - never starts searching for a new one
            # while the old one might still answer - then a short settle
            # delay (the port can bounce once right at power-down), then
            # goes back to actively watching for the next board exactly
            # like a fresh start() would.
            present = bool(self._port_device) and self._port_device in {info.device for info in list_fc_ports()}
            if present:
                self._fc_gone_since = None
                return
            if self._fc_gone_since is None:
                self._fc_gone_since = time.monotonic()
                self.dfu_detail_changed.emit("FC вимкнено - готуюсь до наступної плати...")
                return
            if time.monotonic() - self._fc_gone_since < NEXT_BOARD_SETTLE_DELAY_S:
                return
            self._flog.info("FC flash station: previous FC gone - watching for the next board")
            self._fc_gone_since = None
            self._port_device = None
            self._pre_dfu_port_snapshot = set()
            self._dfu_poll_count = 0
            self.dfu_detail_changed.emit("")
            self._set_phase("waiting_device" if self._flash_fc_enabled else "waiting_fc_normal")
            return
        # "flashing_fc"/"resetting_fc"/"flashing"/"fc_flash_error"/
        # "writing_protocol"/"connecting_motor_test"/"checking_motor_armed"/
        # "running_motor_test"/"protocol_error"/"motor_test_error": no
        # polling action, waits for the relevant *_done signal or a manual
        # retry().

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
        if not self._armed or self._phase not in ("waiting_device", "waiting_fc_normal"):
            return
        self._dfu_poll_count += 1
        self._flog.info("FC flash station: `-l usb` tick #%d -> %d DFU device(s)", self._dfu_poll_count, len(devices))
        if not devices:
            if self._phase != "waiting_device":
                # "waiting_fc_normal": this was just the background DFU
                # check (see _on_poll_tick's own comment there) - normal-
                # mode reappearance is this phase's own primary watch,
                # handled separately, nothing more to do here.
                return
            # No DFU device found THIS tick - only now consider "maybe the
            # FC is already in normal mode, no flash needed this round".
            # Checked here, after confirming no DFU device is present,
            # rather than unconditionally at the top of the poll tick -
            # see _on_poll_tick()'s own comment for why a DFU device
            # actually being there must always take priority over a
            # same-named port that merely happens to exist.
            normal_present = bool(self._port_device) and self._port_device in {
                info.device for info in list_fc_ports()
            }
            if normal_present:
                self._flog.info(
                    "FC flash station: normal-mode port %s already present (no DFU device found) - "
                    "skipping DFU flash this round", self._port_device,
                )
                self._connect_attempts = 0
                self._set_phase("connecting")
                self._attempt_connect()
                return
            # Visible, constantly-updating proof the search loop is really
            # running - previously this tick was entirely silent in the
            # GUI when nothing was found, which looked indistinguishable
            # from "stuck"/"not even trying" to an operator watching.
            self.dfu_detail_changed.emit(f"DFU не знайдено (спроба {self._dfu_poll_count}) - шукаю далі...")
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
        # Fresh baseline right before the flash - whatever "waiting_fc_normal"
        # was previously tracking (if that's where this flash started from,
        # e.g. operator corrected a forgotten-DFU mistake, or plugged in the
        # next board) is now moot: this is the new "before" snapshot for the
        # upcoming DFU reset's own re-enumeration detection.
        self._pre_dfu_port_snapshot = {info.device for info in list_fc_ports()}
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
        self._fc_flashed_no_protocol = True
        self.fc_flash_succeeded.emit()
        self._set_phase("waiting_fc_normal")

    # ---- forwarded EscManager signals (identical logic to hotspot_station.py) ---

    def _on_connection_state_changed(self, connected: bool, expected_count: int) -> None:
        self._connect_in_flight = False
        if not self._armed:
            return
        if self._phase == "writing_protocol" and not connected:
            # The point of synchronization that avoids a race between the
            # ESC worker's transport.close() and the protocol worker's own
            # SerialTransport() open on the same port, both on different
            # threads - see this file's module docstring. EscManager.
            # disconnect() always emits connection_state_changed(False)
            # AFTER transport.close() has already returned, so only once
            # THIS arrives is it safe to reopen the port elsewhere.
            self._flog.info("FC flash station: FourWay confirmed disconnected - starting protocol write")
            # Fresh snapshot right before the MSP_REBOOT this triggers, so
            # "waiting_fc_for_motor_test" can tell the FC's new port apart
            # from whatever was already there - same trick as the DFU reset.
            self._pre_dfu_port_snapshot = {info.device for info in list_fc_ports()}
            self._write_protocol_requested.emit(
                self._port_device or "", DEFAULT_BAUD, self._protocol_index, self._write_protocol_enabled
            )
            return
        if self._phase != "connecting":
            return
        if connected:
            if not self._flash_esc_enabled:
                if not self._motor_test_enabled:
                    self._flog.info("FC flash station: ESC-flash and motor-test stages both disabled - nothing left to do")
                    self._set_phase("all_done")
                    return
                self._flog.info(
                    "FC flash station: ESC-flash stage disabled by operator - still writing the motor "
                    "protocol before the motor test (the ESC can't arm/initialize on the wrong one)"
                )
                self._begin_protocol_write()
                return
            if self._fc_flashed_no_protocol and self._write_protocol_enabled:
                # A freshly DFU-flashed FC is back on INAV's default (STANDARD) protocol. Writing the
                # motor protocol only AFTER every ESC is done left boards on STANDARD whenever an ESC
                # never answered and the operator gave up (real logs: ~3% of boards) - so do it now,
                # first thing, independent of the ESC outcome. The usual final write still follows.
                self._flog.info("FC flash station: freshly flashed FC - writing the motor protocol BEFORE the ESC stage")
                self._protocol_stage_early = True
                self._begin_protocol_write()
                return
            self._last_esc_states = {}
            self._flashed_indices = set()
            self._tick_seen_states = {}
            for i in self._esc_indices:
                self.esc_detail_changed.emit(i, "idle", "")
            self._poll_timer.start()
            self._set_phase("watching")
        elif self._connect_attempts < MAX_CONNECT_ATTEMPTS:
            # a freshly flashed/rebooted FC often needs a few tries - say so instead of leaving a bare error in the log
            self.dfu_detail_changed.emit(
                f"FC ще не готовий до підключення (спроба {self._connect_attempts} з {MAX_CONNECT_ATTEMPTS}) - пробую ще раз..."
            )

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
        for i in self._esc_indices:
            if i not in self._tick_seen_states:  # gone (or not there yet): forget it, show "-" - the others keep their lines
                self._last_esc_states.pop(i, None)
                self._flashed_indices.discard(i)
                self.esc_detail_changed.emit(i, "idle", "")
        next_phase, need_flash = phase_after_discovery_many(self._tick_seen_states, self._esc_indices)
        if next_phase == "watching":
            self._set_phase("watching")
            return
        if need_flash:
            self._flashing_indices = list(need_flash)
            self._set_phase("flashing")
            self._flash_requested.emit(self._config_bytes, need_flash)
            return
        # Every watched ESC slot is confirmed present and already matching
        # - unlike hotspot_station.py, this is NOT a steady state to keep
        # watching from: move straight on to the one-shot protocol write,
        # see _begin_protocol_write()'s own docstring for why.
        self._counter_ok += 1
        self.counters_changed.emit(self._counter_flashed, self._counter_ok, self._counter_errors)
        self.esc_flash_succeeded.emit()
        self._set_phase("esc_ready")
        self._begin_protocol_write()

    def _on_flash_finished(self, success: int, attempted: int) -> None:
        if not self._armed or self._phase != "flashing":
            return
        # Per-ESC counts, not per-batch: one flash_bytes() call here can
        # cover several indices at once (e.g. both halves of a 2-in-1 ESC
        # board), unlike hotspot_station.py's always-exactly-one-slot flash.
        self._counter_flashed += success
        self._counter_errors += max(attempted - success, 0)
        self.counters_changed.emit(self._counter_flashed, self._counter_ok, self._counter_errors)
        # Deliberately does NOT emit esc_flash_succeeded (the per-BOARD
        # counter) here, even on full success, and does NOT trigger
        # _begin_protocol_write() directly either - the next poll tick's
        # own _discover_many_requested re-reads every watched slot fresh,
        # and ONLY once that readback confirms every slot genuinely
        # matches does _on_discovery_finished() both move on to the
        # protocol write AND fire esc_flash_succeeded exactly once - same
        # "never trust a write without reading it back" principle as the
        # write itself, just applied one level up. Emitting it here TOO
        # used to double-count every board that actually needed a flash
        # (once here, a second time from that very next confirmation
        # tick) - confirmed from real multi-board runs where the per-board
        # ESC counter came out roughly double the number of boards
        # actually processed.
        self._set_phase("esc_ready" if success == attempted and attempted > 0 else "esc_error")

    def banner_text(self) -> str:
        """PHASE_LABEL, except "esc_ready" names which ESC(s) are done so
        a half-finished 2-in-1 board never reads like a finished one."""
        def names(indices) -> str:
            return ", ".join(str(i + 1) for i in sorted(indices))

        if self._phase == "flashing" and self._flashing_indices:
            return f"Прошивка ESC {names(self._flashing_indices)}..."
        if self._phase in ("watching", "esc_ready"):
            done = {i for i in self._esc_indices if self._last_esc_states.get(i) == "ok"}
            pending = set(self._esc_indices) - done
            if done and pending:
                return f"ESC {names(done)} готова - чекаю ESC {names(pending)}..."
            if done and self._phase == "esc_ready":
                return f"ESC {names(done)} готові"
        if self._phase == "all_done" and self._motor_test_enabled:
            return "Готово! FC і ESC прошиті, протокол встановлено, мотори перевірено."
        return PHASE_LABEL[self._phase]

    def _on_esc_status_changed(self, index: int, state: str, detail: str, esc_info) -> None:
        if not self._armed or index not in self._esc_indices:
            return
        self._last_esc_states[index] = state
        self._tick_seen_states[index] = state
        if state == "ok":
            if self._phase == "flashing":
                self._flashed_indices.add(index)
            if index in self._flashed_indices:
                detail = tr("flashed_verified")
        self.esc_detail_changed.emit(index, state, detail)

    # ---- protocol-write phase (terminal step, once every watched ESC is ready) ---

    def _begin_protocol_write(self) -> None:
        """Every watched ESC slot just got confirmed good for the first
        time - exit the FourWay/ESC world and write the FC's motor
        protocol (MSP_SET_ADVANCED_CONFIG -> MSP_EEPROM_WRITE ->
        MSP_REBOOT via _ProtocolWorker), the exact "Save and Reboot" dance
        INAV Configurator does by hand. The actual write only happens once
        _on_connection_state_changed() sees the resulting disconnect
        confirmed - see that method for why the ordering matters."""
        self._flog.info(
            "FC flash station: all watched ESC slot(s) ready - disconnecting FourWay and writing protocol "
            "(index=%d, port=%s)", self._protocol_index, self._port_device,
        )
        self._set_phase("writing_protocol")
        self._disconnect_requested.emit()

    def _on_protocol_write_done(self, success: bool, message: str, rebooted: bool = True) -> None:
        if not self._armed or self._phase != "writing_protocol":
            return
        self._flog.info("FC flash station: protocol write done: success=%s, message=%s", success, message)
        self.dfu_detail_changed.emit(message)
        if not success:
            self._set_phase("protocol_error")
            return
        self._fc_flashed_no_protocol = False
        if self._protocol_stage_early:
            self._protocol_stage_early = False
            if rebooted:
                self._after_protocol_since = time.monotonic()
                self._set_phase("waiting_fc_after_protocol")  # FC reboots; then reconnect and carry on with the ESCs
            else:
                self._connect_attempts = 0  # nothing was rebooted: reconnect straight away
                self._set_phase("connecting")
            return
        if not self._motor_test_enabled:
            self._flog.info("FC flash station: motor-test stage disabled by operator - stopping after protocol write")
            self._set_phase("all_done")
            return
        if rebooted:
            self._after_protocol_since = time.monotonic()
            self._set_phase("waiting_fc_for_motor_test")
        else:  # the FC was never rebooted, so there is nothing to wait for
            self._set_phase("connecting_motor_test")
            self._begin_motor_connect()

    # ---- final motor-test phase (reuses MotorTestManager as-is) ---------

    def _begin_motor_connect(self) -> None:
        self._motor_connect_attempts = 0
        self._try_motor_connect()

    def _try_motor_connect(self) -> None:
        self._motor_connect_attempts += 1
        self._motor_connect_requested.emit(self._port_device or "", DEFAULT_BAUD)

    def _retry_motor_connect(self) -> None:
        if self._armed and self._phase == "connecting_motor_test":
            self._try_motor_connect()

    def _on_motor_connection_state_changed(self, connected: bool) -> None:
        if not self._armed or self._phase != "connecting_motor_test":
            return
        if not connected:
            if self._motor_connect_attempts < MOTOR_CONNECT_ATTEMPTS:
                self.dfu_detail_changed.emit(
                    f"FC ще не готовий для тесту моторів (спроба {self._motor_connect_attempts} з {MOTOR_CONNECT_ATTEMPTS}) - пробую ще раз..."
                )
                QTimer.singleShot(1500, self._retry_motor_connect)
                return
            self.dfu_detail_changed.emit("Не вдалось підключитись для тесту моторів.")
            self._set_phase("motor_test_error")
            return
        self._motor_set_ramp_mode_requested.emit(load_str(_MOTOR_RAMP_MODE_KEY, RAMP_BEZIER))
        self._motor_set_curves_requested.emit(
            RampCurve.from_json(load_str(UP_CURVE_KEY)), RampCurve.from_json(load_str(DOWN_CURVE_KEY))
        )
        self._motor_set_tick_requested.emit(load_int(TICK_KEY, 100))
        self._motor_set_test_mode_requested.emit(load_str(_MOTOR_TEST_MODE_KEY, TEST_MODE_ALL))
        self._motor_set_speed_bounds_requested.emit(
            float(load_int(_MOTOR_MIN_BOUND_KEY, DEFAULT_MOTOR_MIN_BOUND)),
            float(load_int(_MOTOR_MAX_BOUND_KEY, DEFAULT_MOTOR_MAX_BOUND)),
        )
        self._set_phase("checking_motor_armed")
        self._motor_check_disarmed_requested.emit()

    def _on_motor_protocol_detected(self, index: int, _name: str) -> None:
        self._motor_detected_protocol = index

    def _on_motor_battery_voltage(self, volts: float) -> None:
        self._motor_battery_v = volts

    def _on_motor_count_detected(self, count: int) -> None:
        self._motor_count_for_test = count

    def _on_motor_armed_check_result(self, disarmed: bool, detail: str) -> None:
        if not self._armed or self._phase != "checking_motor_armed":
            return
        if not disarmed:
            self.dfu_detail_changed.emit(f"Тест моторів заблоковано: {detail or 'не вдалось підтвердити стан disarmed'}")
            self._set_phase("motor_test_error")
            return
        if self._motor_detected_protocol >= 0 and self._motor_detected_protocol != self._protocol_index:
            have = MOTOR_PROTOCOLS[self._motor_detected_protocol] if self._motor_detected_protocol < len(MOTOR_PROTOCOLS) else "?"
            want = MOTOR_PROTOCOLS[self._protocol_index] if 0 <= self._protocol_index < len(MOTOR_PROTOCOLS) else "?"
            self.dfu_detail_changed.emit(
                f"Тест моторів скасовано: на FC протокол {have}, а потрібен {want} - ESC не запуститься. "
                "Увімкніть запис протоколу або виправте його вручну."
            )
            self._set_phase("motor_test_error")
            return
        if self._motor_count_for_test == 0:
            self.dfu_detail_changed.emit("FC не повідомляє жодного мотора - нема що тестувати.")
            self._set_phase("motor_test_error")
            return
        if 0.0 <= self._motor_battery_v < 1.0:
            # not a block: INAV reports 0 V when no battery sensor is configured, too - but a test on an
            # unpowered ESC spins nothing, so say so instead of silently "verifying" the motors
            self.dfu_detail_changed.emit(
                f"Увага: напруга живлення {self._motor_battery_v:.1f} В - схоже, на ESC не подано живлення; "
                "мотори можуть не обертатись."
            )
        self._flog.info("FC flash station: starting final motor test (%d motor(s))", self._motor_count_for_test)
        self._set_phase("running_motor_test")
        self._motor_start_sweep_requested.emit(
            float(load_int(_MOTOR_UP_DURATION_KEY, DEFAULT_MOTOR_UP_DURATION_S)),
            float(load_int(_MOTOR_HOLD_DURATION_KEY, DEFAULT_MOTOR_HOLD_DURATION_S)),
            float(load_int(_MOTOR_DOWN_DURATION_KEY, DEFAULT_MOTOR_DOWN_DURATION_S)),
        )

    def _on_motor_phase_changed(self, phase: str) -> None:
        if not self._armed or self._phase != "running_motor_test":
            return
        if phase == MOTOR_PHASE_IDLE:
            self._flog.info("FC flash station: final motor test finished - all done")
            self._motor_disconnect_requested.emit()
            self.motor_test_succeeded.emit()
            self._set_phase("all_done")

    def _on_motor_log_message(self, level: str, text: str) -> None:
        if not self._armed or self._phase not in MOTOR_TEST_PHASES:
            return
        self.dfu_detail_changed.emit(text)
