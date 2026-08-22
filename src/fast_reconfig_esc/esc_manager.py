"""Per-ESC discovery and config-flash orchestration.

Runs on a worker thread (see connection_worker.py). Every ESC index is
handled in its own try/except so one missing/unresponsive ESC can never
abort or crash a batch covering the rest - this is the tool's core
reliability requirement.
"""
from __future__ import annotations

import time
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

from .eeprom_config import ConfigFileError, load_config_file
from .fourway import FourWayClient
from .i18n import tr
from .mcu_profiles import LAYOUT_SIZE, MCU_VARIANTS
from .models import EscState, ProbeResult
from .msp import MspClient, MspCommand, MspError
from .serial_transport import SerialTransport

MAX_ESC_COUNT = 12
POST_PASSTHROUGH_DELAY_S = 2.0  # matches the reference tool's `await delay(2000)`
DEFAULT_ESC_CONNECT_RETRIES = 2
MIN_ESC_CONNECT_RETRIES = 1
MAX_ESC_CONNECT_RETRIES = 20
EMPTY_SCANS_BEFORE_POWER_CYCLE_HINT = 2


class VerificationError(Exception):
    pass


def _is_blank(settings: bytes) -> bool:
    return all(b == 0xFF for b in settings) or sum(settings) == 0


class EscManager(QObject):
    esc_status_changed = pyqtSignal(int, str, str)  # index, EscState.value, detail text
    log_message = pyqtSignal(str, str)  # level ("info"/"warning"/"error"/"success"), text
    connection_state_changed = pyqtSignal(bool, int)  # connected, expected_esc_count
    discovery_finished = pyqtSignal(int, int)  # found, expected
    flash_finished = pyqtSignal(int, int)  # success, attempted

    def __init__(self) -> None:
        super().__init__()
        self._transport: Optional[SerialTransport] = None
        self._fourway: Optional[FourWayClient] = None
        self._expected_count = 0
        self._probe_results: dict[int, ProbeResult] = {}
        self._esc_connect_retries = DEFAULT_ESC_CONNECT_RETRIES
        self._loaded_config_bytes: Optional[bytes] = None
        self._consecutive_empty_scans = 0

    def set_config_path(self, path: str) -> None:
        """Keep a copy of the currently-loaded config so discovery can show,
        per ESC, whether its existing settings already match it - without
        needing a flash to find out.
        """
        if not path:
            self._loaded_config_bytes = None
            return
        try:
            self._loaded_config_bytes = load_config_file(path)
        except ConfigFileError as e:
            self._loaded_config_bytes = None
            self.log_message.emit("error", tr("config_file_error", error=e))
            return
        self._reevaluate_against_loaded_config()

    def _reevaluate_against_loaded_config(self) -> None:
        # Re-classify already-probed ESCs immediately when a new config is
        # loaded, instead of waiting for the next Rescan.
        for index, result in self._probe_results.items():
            if not result.ok or result.settings is None:
                continue
            state, detail = self._classify(result)
            self.esc_status_changed.emit(index, state.value, detail)

    def _classify(self, result: ProbeResult) -> tuple[EscState, str]:
        settings = result.settings or b""
        if _is_blank(settings):
            return EscState.BLANK, result.mcu_name
        if self._loaded_config_bytes is not None:
            if settings == self._loaded_config_bytes:
                return EscState.OK, tr("config_matches", mcu=result.mcu_name)
            return EscState.DIFF, tr("config_differs", mcu=result.mcu_name)
        return EscState.OK, result.mcu_name

    def set_esc_connect_retries(self, value: int) -> None:
        """How many times to retry cmd_DeviceInitFlash before giving up on one
        ESC index (during discovery or before a flash write). Low by design:
        a missing ESC (nothing wired to that output) legitimately returns
        ACK_D_GENERAL_ERROR every time, so a high retry count just makes
        scanning an FC with fewer ESCs than outputs slower for no benefit.
        """
        clamped = max(MIN_ESC_CONNECT_RETRIES, min(int(value), MAX_ESC_CONNECT_RETRIES))
        self._esc_connect_retries = clamped
        self.log_message.emit("info", tr("retries_set", value=clamped))

    # ---- lifecycle ----------------------------------------------------

    def connect(self, port: str, baud: int) -> None:
        try:
            transport = SerialTransport(port, baud)
        except Exception as e:
            self.log_message.emit("error", tr("could_not_open_port", port=port, error=e))
            self.connection_state_changed.emit(False, 0)
            return

        self._transport = transport
        msp = MspClient(transport)
        try:
            self._handshake(msp)
            expected = self._enter_passthrough(msp)
        except Exception as e:
            self.log_message.emit("error", tr("connect_failed", error=e))
            transport.close()
            self._transport = None
            self.connection_state_changed.emit(False, 0)
            return

        self._expected_count = min(max(expected, 0), MAX_ESC_COUNT)
        self._fourway = FourWayClient(transport)
        self._probe_results = {}
        self._consecutive_empty_scans = 0
        cap = tr("capped_suffix", max=MAX_ESC_COUNT) if expected > MAX_ESC_COUNT else ""
        self.log_message.emit("info", tr("connected_report", count=expected, cap=cap))
        self.connection_state_changed.emit(True, self._expected_count)

    def _handshake(self, msp: MspClient) -> None:
        try:
            msp.send(MspCommand.MSP_API_VERSION)
        except MspError as e:
            # Mirrors the reference site's own recovery: if a previous session
            # left the FC latched in FourWay mode, explicitly exit it and retry once.
            self.log_message.emit("warning", tr("fourway_exit_retry", error=e))
            assert self._transport is not None
            FourWayClient(self._transport).exit_interface()
            time.sleep(1.0)
            msp.send(MspCommand.MSP_API_VERSION)

        msp.send(MspCommand.MSP_FC_VARIANT)
        msp.send(MspCommand.MSP_BATTERY_STATE)
        msp.send(MspCommand.MSP_MOTOR)

    def _enter_passthrough(self, msp: MspClient) -> int:
        response = msp.send(MspCommand.MSP_SET_PASSTHROUGH, payload=b"")
        if not response.payload:
            raise MspError("MSP_SET_PASSTHROUGH returned no data")
        expected_count = response.payload[0]
        time.sleep(POST_PASSTHROUGH_DELAY_S)
        return expected_count

    def disconnect(self) -> None:
        if self._fourway is not None:
            self._fourway.exit_interface()
        if self._transport is not None:
            self._transport.close()
        self._transport = None
        self._fourway = None
        self._expected_count = 0
        self._probe_results = {}
        self.connection_state_changed.emit(False, 0)
        self.log_message.emit("info", tr("disconnected_log"))

    # ---- discovery ----------------------------------------------------

    def discover_all(self) -> None:
        if self._fourway is None:
            self.log_message.emit("error", tr("not_connected_log"))
            return

        found = 0
        for i in range(self._expected_count):
            self.esc_status_changed.emit(i, EscState.PROBING.value, "")
            try:
                result = self._probe_one(i)
            except Exception as e:  # noqa: BLE001 - deliberately broad: never abort the batch
                self._probe_results[i] = ProbeResult(ok=False, error=str(e))
                self.log_message.emit("error", tr("esc_probe_error", n=i + 1, error=e))
                self.esc_status_changed.emit(i, EscState.ERROR.value, str(e))
                continue

            self._probe_results[i] = result
            found += 1
            state, detail = self._classify(result)
            self.esc_status_changed.emit(i, state.value, detail)

        self.log_message.emit("info", tr("discovery_summary", found=found, expected=self._expected_count))
        self.discovery_finished.emit(found, self._expected_count)

        if found == 0 and self._expected_count > 0:
            self._consecutive_empty_scans += 1
        else:
            self._consecutive_empty_scans = 0
        if self._consecutive_empty_scans >= EMPTY_SCANS_BEFORE_POWER_CYCLE_HINT:
            self.log_message.emit("warning", tr("power_cycle_hint"))

    def _probe_one(self, index: int) -> ProbeResult:
        assert self._fourway is not None
        info = self._fourway.init_flash(index, retries=self._esc_connect_retries)
        mcu = MCU_VARIANTS.get(info.signature)
        if mcu is None:
            raise KeyError(f"unrecognized MCU signature 0x{info.signature:04X}")
        settings = self._fourway.read(mcu.eeprom_offset, LAYOUT_SIZE)
        return ProbeResult(ok=True, mcu_name=mcu.name, settings=settings)

    # ---- flashing ----------------------------------------------------

    def flash_all(self, config_path: str, target_indices: Optional[list] = None) -> None:
        if self._fourway is None:
            self.log_message.emit("error", tr("not_connected_log"))
            return

        try:
            config_bytes = load_config_file(config_path)
        except ConfigFileError as e:
            self.log_message.emit("error", tr("config_file_error", error=e))
            return

        if target_indices is None:
            ok_indices = [i for i, r in self._probe_results.items() if r.ok]
            target_indices = ok_indices if ok_indices else list(range(self._expected_count))

        success = 0
        for i in target_indices:
            self.esc_status_changed.emit(i, EscState.PROBING.value, tr("writing"))
            try:
                self._flash_one(i, config_bytes)
            except Exception as e:  # noqa: BLE001 - one ESC's failure must never block the rest
                self.log_message.emit("error", tr("esc_flash_error", n=i + 1, error=e))
                self.esc_status_changed.emit(i, EscState.ERROR.value, str(e))
                continue
            self.esc_status_changed.emit(i, EscState.OK.value, tr("flashed_verified"))
            success += 1

        self.log_message.emit("info", tr("flash_summary", success=success, attempted=len(target_indices)))
        self.flash_finished.emit(success, len(target_indices))

    def _flash_one(self, index: int, config_bytes: bytes) -> None:
        assert self._fourway is not None
        # re-select defensively even if already probed
        info = self._fourway.init_flash(index, retries=self._esc_connect_retries)
        mcu = MCU_VARIANTS.get(info.signature)
        if mcu is None:
            raise KeyError(f"unrecognized MCU signature 0x{info.signature:04X}")

        self._fourway.write(mcu.eeprom_offset, config_bytes)  # cmd_DeviceWrite, NOT DeviceWriteEEprom
        readback = self._fourway.read(mcu.eeprom_offset, LAYOUT_SIZE)
        if readback != config_bytes:
            raise VerificationError("readback mismatch after write")

        self._probe_results[index] = ProbeResult(ok=True, mcu_name=mcu.name, settings=readback)
