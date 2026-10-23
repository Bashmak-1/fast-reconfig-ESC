"""Per-ESC discovery and config-flash orchestration.

Runs on a worker thread (see connection_worker.py). Every ESC index is
handled in its own try/except so one missing/unresponsive ESC can never
abort or crash a batch covering the rest - this is the tool's core
reliability requirement.

Every user-visible log line also goes to the persistent technical log file
(file_logger.py), plus a lot of extra low-level protocol detail (raw MSP
payloads, full hex dumps of settings before/after a write) that would be
too noisy for the GUI panel but is exactly what you want when something
confusing happens and you need to reconstruct what actually went over the
wire - see `~/.esc_hotline/logs/esc_hotline.log`.
"""
from __future__ import annotations

import time
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

from .eeprom_config import ConfigFileError, load_config_file
from .eeprom_layout import (
    EEPROM_LAYOUT,
    FILENAME_BLOCK_SIZE,
    parse_boot_loader_pin,
    parse_name_block,
    settings_equal_ignoring_identity,
    with_identity_from,
)
from .eeprom_values import decode_settings
from .file_logger import get_logger, hex_dump, log_at
from .fourway import FourWayClient
from .i18n import tr
from .mcu_profiles import LAYOUT_SIZE, MCU_VARIANTS
from .models import EscInfo, EscState, ProbeResult
from .msp import MspClient, MspCommand, MspError, MspResponse
from .serial_transport import SerialTransport, explain_serial_error

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
    esc_status_changed = pyqtSignal(int, str, str, object)  # index, EscState.value, detail text, EscInfo|None
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
        self._flog = get_logger()

    def _log(self, level: str, text: str) -> None:
        """Emit to the GUI panel and the technical log file at once."""
        self.log_message.emit(level, text)
        log_at(self._flog, level, text)

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
            self._log("error", tr("config_file_error", error=e))
            return
        self._flog.info("Config file loaded: %s (%d bytes, %s)", path, len(self._loaded_config_bytes), hex_dump(self._loaded_config_bytes))
        self._reevaluate_against_loaded_config()

    def _reevaluate_against_loaded_config(self) -> None:
        # Re-classify already-probed ESCs immediately when a new config is
        # loaded, instead of waiting for the next Rescan.
        for index, result in self._probe_results.items():
            if not result.ok or result.settings is None:
                continue
            state, detail = self._classify(result)
            self.esc_status_changed.emit(index, state.value, detail, result.esc_info)

    @staticmethod
    def _display_label(result: ProbeResult) -> str:
        info = result.esc_info
        if info is None:
            return result.mcu_name
        label = info.mcu_type or info.mcu_family
        return f"{label} v{info.firmware_version}" if info.firmware_version else label

    def _classify(self, result: ProbeResult) -> tuple[EscState, str]:
        settings = result.settings or b""
        label = self._display_label(result)
        if _is_blank(settings):
            return EscState.BLANK, label
        if self._loaded_config_bytes is not None:
            if settings_equal_ignoring_identity(settings, self._loaded_config_bytes):
                return EscState.OK, tr("config_matches", mcu=label)
            return EscState.DIFF, tr("config_differs", mcu=label)
        return EscState.OK, label

    def set_esc_connect_retries(self, value: int) -> None:
        """How many times to retry cmd_DeviceInitFlash before giving up on one
        ESC index (during discovery or before a flash write). Low by design:
        a missing ESC (nothing wired to that output) legitimately returns
        ACK_D_GENERAL_ERROR every time, so a high retry count just makes
        scanning an FC with fewer ESCs than outputs slower for no benefit.
        """
        clamped = max(MIN_ESC_CONNECT_RETRIES, min(int(value), MAX_ESC_CONNECT_RETRIES))
        self._esc_connect_retries = clamped
        self._log("info", tr("retries_set", value=clamped))

    # ---- lifecycle ----------------------------------------------------

    def connect(self, port: str, baud: int) -> None:
        self._flog.info("Connect requested: port=%s baud=%d", port, baud)
        try:
            transport = SerialTransport(port, baud)
        except Exception as e:
            key, transient = explain_serial_error(e)
            hint = f" {tr(key)}" if key else ""
            # transient (port not ready / busy right after a reboot) is a warning, not a scary error
            self._log("warning" if transient else "error", tr("could_not_open_port", port=port, error=f"{e}.{hint}"))
            self.connection_state_changed.emit(False, 0)
            return
        self._flog.info("Serial port opened: %s @ %d", port, baud)

        self._transport = transport
        msp = MspClient(transport)
        try:
            api_version, fc_variant = self._handshake(msp)
            expected = self._enter_passthrough(msp)
        except Exception as e:
            key, _transient = explain_serial_error(e)
            self._log("error", tr("connect_failed", error=f"{e}.{' ' + tr(key) if key else ''}"))
            self._flog.exception("Connect failed during handshake/passthrough")
            transport.close()
            self._transport = None
            self.connection_state_changed.emit(False, 0)
            return

        self._expected_count = min(max(expected, 0), MAX_ESC_COUNT)
        self._fourway = FourWayClient(transport)
        self._probe_results = {}
        self._consecutive_empty_scans = 0
        cap = tr("capped_suffix", max=MAX_ESC_COUNT) if expected > MAX_ESC_COUNT else ""
        self._log("info", tr("connected_report", count=expected, cap=cap))
        if api_version or fc_variant:
            self._log("info", tr("connected_fc_info", api=api_version or "?", variant=fc_variant or "?"))
        self.connection_state_changed.emit(True, self._expected_count)

    def _handshake(self, msp: MspClient) -> tuple[Optional[str], Optional[str]]:
        try:
            response = msp.send(MspCommand.MSP_API_VERSION)
        except MspError as e:
            # Mirrors the reference site's own recovery: if a previous session
            # left the FC latched in FourWay mode, explicitly exit it and retry once.
            self._log("warning", tr("fourway_exit_retry", error=e))
            assert self._transport is not None
            FourWayClient(self._transport).exit_interface()
            time.sleep(1.0)
            response = msp.send(MspCommand.MSP_API_VERSION)

        api_version = self._log_msp_api_version(response)
        fc_variant = self._log_msp_fc_variant(msp.send(MspCommand.MSP_FC_VARIANT))
        self._log_msp_battery_state(msp.send(MspCommand.MSP_BATTERY_STATE))
        self._log_msp_raw("MSP_MOTOR", msp.send(MspCommand.MSP_MOTOR))
        return api_version, fc_variant

    def _log_msp_api_version(self, response: MspResponse) -> Optional[str]:
        # Payload is [mspProtocolVersion, apiVersionMajor, apiVersionMinor],
        # but displayed as "major.minor.protocol" (e.g. raw 00 02 05 -> "2.5.0")
        # - confirmed against a real INAV FC session, not just guessed.
        if len(response.payload) >= 3:
            protocol, major, minor = response.payload[0], response.payload[1], response.payload[2]
            version = f"{major}.{minor}.{protocol}"
            self._flog.info("MSP_API_VERSION: %s (raw=%s)", version, hex_dump(response.payload))
            return version
        self._log_msp_raw("MSP_API_VERSION", response)
        return None

    def _log_msp_fc_variant(self, response: MspResponse) -> Optional[str]:
        variant = response.payload.decode("ascii", errors="replace").strip("\x00").strip()
        self._flog.info("MSP_FC_VARIANT: %s (raw=%s)", variant or "?", hex_dump(response.payload))
        return variant or None

    def _log_msp_battery_state(self, response: MspResponse) -> None:
        # Best-effort field parse (both a legacy 0.1V byte at offset 3 and an
        # extended 0.01V uint16 at offset 9-10, when present) - confirmed
        # against a real FC reading 25.3V both ways, not just guessed.
        payload = response.payload
        voltage = None
        if len(payload) >= 11:
            voltage = int.from_bytes(payload[9:11], "little") / 100.0
        elif len(payload) >= 4:
            voltage = payload[3] / 10.0
        if voltage is not None:
            self._flog.info("MSP_BATTERY_STATE: voltage=%.2fV (raw=%s)", voltage, hex_dump(payload))
        else:
            self._log_msp_raw("MSP_BATTERY_STATE", response)

    def _log_msp_raw(self, name: str, response: MspResponse) -> None:
        self._flog.info("%s: raw=%s", name, hex_dump(response.payload))

    def _enter_passthrough(self, msp: MspClient) -> int:
        response = msp.send(MspCommand.MSP_SET_PASSTHROUGH, payload=b"")
        if not response.payload:
            raise MspError("MSP_SET_PASSTHROUGH returned no data")
        expected_count = response.payload[0]
        self._flog.info("MSP_SET_PASSTHROUGH: FC reports %d ESC output(s) (raw=%s)", expected_count, hex_dump(response.payload))
        time.sleep(POST_PASSTHROUGH_DELAY_S)
        return expected_count

    def disconnect(self) -> None:
        self._flog.info("Disconnect requested")
        if self._fourway is not None:
            self._fourway.exit_interface()
        if self._transport is not None:
            self._transport.close()
        self._transport = None
        self._fourway = None
        self._expected_count = 0
        self._probe_results = {}
        self.connection_state_changed.emit(False, 0)
        self._log("info", tr("disconnected_log"))

    # ---- discovery ----------------------------------------------------

    def discover_all(self) -> None:
        if self._fourway is None:
            self._log("error", tr("not_connected_log"))
            return

        self._flog.info("--- Discovery started: probing %d ESC index(es) ---", self._expected_count)
        found = 0
        for i in range(self._expected_count):
            self._flog.info("ESC #%d: probing (cmd_DeviceInitFlash, retries=%d)...", i + 1, self._esc_connect_retries)
            self.esc_status_changed.emit(i, EscState.PROBING.value, "", None)
            try:
                result = self._probe_one(i)
            except Exception as e:  # noqa: BLE001 - deliberately broad: never abort the batch
                self._probe_results[i] = ProbeResult(ok=False, error=str(e))
                self._log("error", tr("esc_probe_error", n=i + 1, error=e))
                self._flog.info("ESC #%d: not found/no response (%s)", i + 1, e)
                self.esc_status_changed.emit(i, EscState.ERROR.value, str(e), None)
                continue

            self._probe_results[i] = result
            found += 1
            state, detail = self._classify(result)
            self._flog.info(
                "ESC #%d: found - %s | settings (%d bytes): %s",
                i + 1, self._describe_esc_info(result.esc_info), len(result.settings or b""), hex_dump(result.settings or b""),
            )
            if result.settings is not None:
                decoded = decode_settings(result.settings)
                self._flog.info(
                    "ESC #%d: decoded settings: %s",
                    i + 1, "; ".join(f"{d.label}={d.value}" for d in decoded),
                )
            self.esc_status_changed.emit(i, state.value, detail, result.esc_info)

        self._log("info", tr("discovery_summary", found=found, expected=self._expected_count))
        self._flog.info("--- Discovery finished: %d/%d found ---", found, self._expected_count)
        self.discovery_finished.emit(found, self._expected_count)

        if found == 0 and self._expected_count > 0:
            self._consecutive_empty_scans += 1
        else:
            self._consecutive_empty_scans = 0
        if self._consecutive_empty_scans >= EMPTY_SCANS_BEFORE_POWER_CYCLE_HINT:
            self._log("warning", tr("power_cycle_hint"))

    def discover_one(self, index: int) -> None:
        """Probe and classify exactly one ESC index - the building block for
        the Хотспот "watch one output" workflow, where an operator only ever
        has one physical ESC wired to one specific output at a time.

        Deliberately does NOT call discover_all(): sweeping every possible
        index every ~1s (as the continuous Хотспот loop does) would log an
        "error"-level line for every one of the other, permanently-empty
        outputs, forever. Here, "not currently responding" is the normal,
        expected steady state between ESC swaps - it's only logged to the
        technical file log (info level), never surfaced to the GUI panel as
        an error.
        """
        if self._fourway is None:
            self._log("error", tr("not_connected_log"))
            return

        try:
            result = self._probe_one(index)
        except Exception as e:  # noqa: BLE001 - "not plugged in yet" is routine here, not a batch-abort condition
            self._probe_results[index] = ProbeResult(ok=False, error=str(e))
            self._flog.info("ESC #%d: not found/no response (%s)", index + 1, e)
            self.discovery_finished.emit(0, 1)
            return

        self._probe_results[index] = result
        state, detail = self._classify(result)
        self._flog.info(
            "ESC #%d: found - %s | settings (%d bytes): %s",
            index + 1, self._describe_esc_info(result.esc_info), len(result.settings or b""), hex_dump(result.settings or b""),
        )
        if result.settings is not None:
            decoded = decode_settings(result.settings)
            self._flog.info(
                "ESC #%d: decoded settings: %s",
                index + 1, "; ".join(f"{d.label}={d.value}" for d in decoded),
            )
        self.esc_status_changed.emit(index, state.value, detail, result.esc_info)
        self.discovery_finished.emit(1, 1)

    def discover_many(self, indices: list[int]) -> None:
        """Probe and classify exactly the given ESC indices - the multi-slot
        sibling of discover_one(), for a board with more than one ESC wired
        to more than one specific output at once (e.g. a 2-in-1 ESC board
        that shows up as two separate ESC indices on the same physical
        board). Same quiet-about-absence rationale as discover_one(): a
        currently-unplugged watched index is the normal steady state here,
        not an error, so it's only logged at info level, not surfaced to the
        GUI panel.
        """
        if self._fourway is None:
            self._log("error", tr("not_connected_log"))
            return

        found = 0
        for index in indices:
            try:
                result = self._probe_one(index)
            except Exception as e:  # noqa: BLE001 - one missing slot must never abort the rest
                self._probe_results[index] = ProbeResult(ok=False, error=str(e))
                self._flog.info("ESC #%d: not found/no response (%s)", index + 1, e)
                continue

            self._probe_results[index] = result
            found += 1
            state, detail = self._classify(result)
            self._flog.info(
                "ESC #%d: found - %s | settings (%d bytes): %s",
                index + 1, self._describe_esc_info(result.esc_info), len(result.settings or b""), hex_dump(result.settings or b""),
            )
            if result.settings is not None:
                decoded = decode_settings(result.settings)
                self._flog.info(
                    "ESC #%d: decoded settings: %s",
                    index + 1, "; ".join(f"{d.label}={d.value}" for d in decoded),
                )
            self.esc_status_changed.emit(index, state.value, detail, result.esc_info)

        self.discovery_finished.emit(found, len(indices))

    @staticmethod
    def _describe_esc_info(info: Optional[EscInfo]) -> str:
        if info is None:
            return "no identity info"
        parts = [f"MCU={info.mcu_family}"]
        if info.mcu_type:
            parts.append(f"type={info.mcu_type}")
        if info.firmware_name:
            parts.append(f"firmware={info.firmware_name}")
        if info.firmware_version:
            parts.append(f"fw_version={info.firmware_version}")
        if info.boot_pin:
            parts.append(f"boot_pin={info.boot_pin}")
        if info.boot_version is not None:
            parts.append(f"boot_version={info.boot_version}")
        if info.eeprom_version is not None:
            parts.append(f"eeprom_version={info.eeprom_version}")
        parts.append(f"reversed={info.reversed}")
        parts.append(f"3d_mode={info.bidirectional}")
        return ", ".join(parts)

    def _probe_one(self, index: int) -> ProbeResult:
        assert self._fourway is not None
        info = self._fourway.init_flash(index, retries=self._esc_connect_retries)
        mcu = MCU_VARIANTS.get(info.signature)
        if mcu is None:
            raise KeyError(f"unrecognized MCU signature 0x{info.signature:04X}")

        settings = self._fourway.read(mcu.eeprom_offset, LAYOUT_SIZE, retries=self._esc_connect_retries)

        # Name-block read is best-effort: a failure here (e.g. an odd
        # bootloader that rejects reads before eeprom_offset) must not fail
        # the whole probe - ESC presence/config status already stands on
        # the settings read above.
        file_name = mcu_type = None
        try:
            name_block = self._fourway.read(
                mcu.eeprom_offset - FILENAME_BLOCK_SIZE, FILENAME_BLOCK_SIZE, retries=self._esc_connect_retries
            )
            file_name, mcu_type = parse_name_block(name_block)
        except Exception as e:
            self._flog.info("ESC #%d: name-block read failed (non-fatal): %s", index + 1, e)

        esc_info = self._build_esc_info(mcu.name, info.input_pin, settings, file_name, mcu_type)
        return ProbeResult(ok=True, mcu_name=mcu.name, settings=settings, esc_info=esc_info)

    @staticmethod
    def _build_esc_info(
        mcu_family: str,
        boot_input_pin: int,
        settings: bytes,
        file_name: Optional[str],
        mcu_type: Optional[str],
    ) -> EscInfo:
        main_rev = settings[EEPROM_LAYOUT["MAIN_REVISION"].offset]
        sub_rev = settings[EEPROM_LAYOUT["SUB_REVISION"].offset]
        boot_version = settings[EEPROM_LAYOUT["BOOT_LOADER_REVISION"].offset]
        eeprom_version = settings[EEPROM_LAYOUT["LAYOUT_REVISION"].offset]
        return EscInfo(
            mcu_family=mcu_family,
            mcu_type=mcu_type,
            firmware_name=file_name,
            firmware_version=f"{main_rev}.{sub_rev}" if main_rev != 0xFF else None,
            boot_pin=parse_boot_loader_pin(boot_input_pin),
            boot_version=boot_version if boot_version != 0xFF else None,
            eeprom_version=eeprom_version if eeprom_version != 0xFF else None,
            reversed=settings[EEPROM_LAYOUT["MOTOR_DIRECTION"].offset] not in (0, 0xFF),
            bidirectional=settings[EEPROM_LAYOUT["BIDIRECTIONAL_MODE"].offset] not in (0, 0xFF),
            settings=settings,
        )

    # ---- flashing ----------------------------------------------------

    def _auto_select_flash_targets(self, config_bytes: bytes) -> list[int]:
        """Default 'Flash All' target list: every successfully-probed ESC
        whose current settings do NOT already exactly match what we're
        about to write. An ESC that already has the exact right config is
        skipped entirely - no point re-writing flash that's already correct,
        and it cuts down on needless wear/time on a big batch.
        """
        ok_results = [(i, r) for i, r in self._probe_results.items() if r.ok]
        if not ok_results:
            return list(range(self._expected_count))

        targets: list[int] = []
        skipped: list[int] = []
        for i, r in ok_results:
            if settings_equal_ignoring_identity(r.settings, config_bytes):
                skipped.append(i)
            else:
                targets.append(i)

        if skipped:
            self._log("info", tr("skipped_matching", n=len(skipped)))
            for i in skipped:
                result = self._probe_results[i]
                self._flog.info("ESC #%d: already matches target config, skipping write", i + 1)
                self.esc_status_changed.emit(
                    i, EscState.OK.value, tr("config_matches", mcu=self._display_label(result)), result.esc_info
                )

        return targets

    def flash_all(self, config_path: str, target_indices: Optional[list] = None) -> None:
        try:
            config_bytes = load_config_file(config_path)
        except ConfigFileError as e:
            self._log("error", tr("config_file_error", error=e))
            return
        self._flog.info("Flash source: file %s", config_path)
        self.flash_bytes(config_bytes, target_indices)

    def flash_bytes(self, config_bytes: bytes, target_indices: Optional[list] = None) -> None:
        """Writes config_bytes to ESC(s), used both by file-based Flash All
        (via flash_all above) and by the config editor's direct Apply
        button. Same per-ESC isolation, skip-if-already-matches, and
        readback-verify guarantees either way - `_flash_one` and
        `_auto_select_flash_targets` don't know or care where the bytes
        came from.
        """
        if self._fourway is None:
            self._log("error", tr("not_connected_log"))
            return

        if len(config_bytes) != LAYOUT_SIZE:
            self._log("error", tr("config_bytes_wrong_length", expected=LAYOUT_SIZE, actual=len(config_bytes)))
            return

        if target_indices is None:
            target_indices = self._auto_select_flash_targets(config_bytes)

        self._flog.info(
            "--- Flash started: %d bytes, %s -> ESC index(es) %s ---",
            len(config_bytes), hex_dump(config_bytes), target_indices,
        )
        self._flog.info(
            "Flash target decoded settings: %s",
            "; ".join(f"{d.label}={d.value}" for d in decode_settings(config_bytes)),
        )
        success = 0
        for i in target_indices:
            self.esc_status_changed.emit(i, EscState.PROBING.value, tr("writing"), None)
            try:
                wrote = self._flash_one(i, config_bytes)
            except Exception as e:  # noqa: BLE001 - one ESC's failure must never block the rest
                self._log("error", tr("esc_flash_error", n=i + 1, error=e))
                self._flog.info("ESC #%d: flash failed (%s)", i + 1, e)
                self.esc_status_changed.emit(i, EscState.ERROR.value, str(e), None)
                continue
            result = self._probe_results[i]
            detail = tr("flashed_verified") if wrote else tr("config_matches", mcu=self._display_label(result))
            self.esc_status_changed.emit(i, EscState.OK.value, detail, result.esc_info)
            success += 1

        self._log("info", tr("flash_summary", success=success, attempted=len(target_indices)))
        self._flog.info("--- Flash finished: %d/%d succeeded ---", success, len(target_indices))
        self.flash_finished.emit(success, len(target_indices))

    def _flash_one(self, index: int, config_bytes: bytes) -> bool:
        """Writes config_bytes to one ESC, UNLESS its current settings
        already exactly match - checked with a fresh read right here, not
        just via the caller's target-list pre-filter, so this guarantee
        holds no matter how _flash_one ends up being called (never wear
        flash writing a config that's already correct).

        Returns True if a write actually happened, False if skipped because
        it already matched.
        """
        assert self._fourway is not None
        previous = self._probe_results.get(index)

        # re-select defensively even if already probed
        info = self._fourway.init_flash(index, retries=self._esc_connect_retries)
        mcu = MCU_VARIANTS.get(info.signature)
        if mcu is None:
            raise KeyError(f"unrecognized MCU signature 0x{info.signature:04X}")

        current = self._fourway.read(mcu.eeprom_offset, LAYOUT_SIZE)
        self._flog.info("ESC #%d: current settings (fresh read, before write decision): %s", index + 1, hex_dump(current))

        esc_info = previous.esc_info if previous is not None else None
        if settings_equal_ignoring_identity(current, config_bytes):
            self._flog.info(
                "ESC #%d: already matches target config (ignoring bootloader/hardware identity bytes - see "
                "eeprom_layout.IDENTITY_FIELD_NAMES) - skipping cmd_DeviceWrite entirely (no flash wear)",
                index + 1,
            )
            self._probe_results[index] = ProbeResult(ok=True, mcu_name=mcu.name, settings=current, esc_info=esc_info)
            return False

        # Confirmed on real hardware: never send the config file's own
        # identity/revision header bytes (BOOT_LOADER_REVISION etc. - see
        # eeprom_layout.py) - they describe THIS ESC's own bootloader/
        # hardware, not a configurable setting, a stale value from a
        # config dumped off a different board gets silently rejected by
        # the firmware, and comparing the readback against the UNCHANGED
        # request byte-for-byte then always "fails" even though every
        # real setting was applied correctly.
        outgoing = with_identity_from(config_bytes, current)
        self._flog.info(
            "ESC #%d: writing %d bytes to eeprom_offset=0x%X (identity bytes preserved from the ESC itself): %s",
            index + 1, len(outgoing), mcu.eeprom_offset, hex_dump(outgoing),
        )
        self._fourway.write(mcu.eeprom_offset, outgoing)  # cmd_DeviceWrite, NOT DeviceWriteEEprom
        readback = self._fourway.read(mcu.eeprom_offset, LAYOUT_SIZE)
        self._flog.info("ESC #%d: readback after write: %s", index + 1, hex_dump(readback))
        if readback != outgoing:
            raise VerificationError("readback mismatch after write")
        self._flog.info("ESC #%d: write verified OK (readback matches what was written)", index + 1)

        # A config write doesn't change the ESC's bootloader/hardware identity,
        # only its settings - carry forward the name/firmware info from the
        # last probe rather than doing another name-block read.
        self._probe_results[index] = ProbeResult(ok=True, mcu_name=mcu.name, settings=readback, esc_info=esc_info)
        return True
