"""Experimental, isolated manager for the "UID Lab" tab: reading a hardware
factory-unique Device ID via ADDRESS_MAGIC_UID (a magic FourWay address only
understood by a custom-forked AM32 bootloader, not the stock one - see
docs-local/IDEA_ESC_TRACEABILITY.md) and flashing a bootloader-updater .hex
image that carries such a fork.

Deliberately does NOT touch esc_manager.py or share its EscManager instance:
this feature is experimental and its firmware-flash operation is genuinely
destructive (it erases the ESC's working application until a new one is
flashed back), unlike anything the other four tabs do. Keeping it on its own
manager and its own private connection (see uid_lab_widget.py, which owns
this class's QThread the same way hotspot_station.py's
HotspotStationController owns its ConnectionWorker) means a bug here cannot
regress the proven config read/write path every other tab depends on.

The connect()/disconnect() sequence below duplicates EscManager's MSP
handshake + passthrough-entry (esc_manager.py's _handshake/_enter_passthrough)
rather than sharing it, for the same isolation reason - it is small, stable,
already-proven protocol logic, not something worth a shared-file refactor
that would touch the production class for an experimental feature.
"""
from __future__ import annotations

import time
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

from .eeprom_layout import EEPROM_LAYOUT
from .firmware_image import HexParseError, build_flash_image, parse_intel_hex, to_wire_address
from .fourway import FourWayClient, FourWayError
from .mcu_profiles import LAYOUT_SIZE, MCU_VARIANTS
from .msp import MspClient, MspCommand, MspError
from .serial_transport import SerialTransport

POST_PASSTHROUGH_DELAY_S = 2.0  # matches EscManager - same FC-side protocol requirement

# EXPERIMENTAL, not part of stock AM32: only a bootloader patched per
# docs-local/IDEA_ESC_TRACEABILITY.md understands this address (returns the
# chip's factory Unique ID); every other ESC just returns a clean ACK error
# for it, exactly like any other out-of-range address.
ADDRESS_MAGIC_UID = 0x24
# 16 bytes covers the largest known UID (NXP MCXA153, 128-bit); AT32F421's
# 96-bit UID only fills the first 12 - the trailing 4 are other STM32-style
# system-memory bytes, harmless to read, left in the displayed hex for the
# operator to eyeball rather than guessing a per-MCU length here.
UID_READ_LENGTH = 16


class UidLabManager(QObject):
    log_message = pyqtSignal(str, str)  # level ("info"/"warning"/"error"/"success"), text
    connection_state_changed = pyqtSignal(bool, int)  # connected, expected_esc_count
    uid_result = pyqtSignal(int, bool, str)  # index, ok, hex_uid_or_error_message
    flash_progress = pyqtSignal(int, int)  # bytes_written, total_bytes
    flash_finished = pyqtSignal(bool, str)  # success, message

    def __init__(self) -> None:
        super().__init__()
        self._transport: Optional[SerialTransport] = None
        self._fourway: Optional[FourWayClient] = None
        self._expected_count = 0

    def _log(self, level: str, text: str) -> None:
        self.log_message.emit(level, text)

    # ---- lifecycle ------------------------------------------------

    def connect(self, port: str, baud: int) -> None:
        try:
            transport = SerialTransport(port, baud)
        except Exception as e:
            self._log("error", f"Не вдалося відкрити {port}: {e}")
            self.connection_state_changed.emit(False, 0)
            return

        msp = MspClient(transport)
        try:
            try:
                msp.send(MspCommand.MSP_API_VERSION)
            except MspError:
                FourWayClient(transport).exit_interface()
                time.sleep(1.0)
                msp.send(MspCommand.MSP_API_VERSION)

            response = msp.send(MspCommand.MSP_SET_PASSTHROUGH, payload=b"")
            if not response.payload:
                raise MspError("MSP_SET_PASSTHROUGH returned no data")
            expected = response.payload[0]
            time.sleep(POST_PASSTHROUGH_DELAY_S)
        except Exception as e:
            self._log("error", f"Не вдалося підключитись: {e}")
            transport.close()
            self.connection_state_changed.emit(False, 0)
            return

        self._transport = transport
        self._fourway = FourWayClient(transport)
        self._expected_count = expected
        self._log("info", f"Підключено. FC повідомляє про {expected} вихід(ів) ESC.")
        self.connection_state_changed.emit(True, expected)

    def disconnect(self) -> None:
        if self._fourway is not None:
            self._fourway.exit_interface()
        if self._transport is not None:
            self._transport.close()
        self._transport = None
        self._fourway = None
        self._expected_count = 0
        self.connection_state_changed.emit(False, 0)
        self._log("info", "Відключено.")

    # ---- UID read ------------------------------------------------

    def read_uid(self, index: int) -> None:
        if self._fourway is None:
            self._log("error", "Не підключено.")
            self.uid_result.emit(index, False, "не підключено")
            return
        try:
            info = self._fourway.init_flash(index)
            mcu = MCU_VARIANTS.get(info.signature)
            mcu_name = mcu.name if mcu is not None else f"невідомий (0x{info.signature:04X})"
            uid_bytes = self._fourway.read(ADDRESS_MAGIC_UID, UID_READ_LENGTH)
        except Exception as e:
            self._log(
                "error",
                f"ESC #{index + 1}: читання UID не вдалося ({e}) - стандартний бутлоадер, чи невірний індекс ESC?",
            )
            self.uid_result.emit(index, False, str(e))
            return
        uid_hex = uid_bytes.hex()
        self._log("success", f"ESC #{index + 1} ({mcu_name}): UID = {uid_hex}")
        self.uid_result.emit(index, True, uid_hex)

    # ---- firmware (bootloader-updater) flash ------------------------------------------------

    def flash_firmware(self, index: int, hex_path: str) -> None:
        if self._fourway is None:
            self._log("error", "Не підключено.")
            self.flash_finished.emit(False, "не підключено")
            return
        try:
            with open(hex_path, "r", encoding="ascii") as f:
                records = parse_intel_hex(f.read())
        except (OSError, HexParseError) as e:
            self._log("error", f"Не вдалося прочитати файл прошивки: {e}")
            self.flash_finished.emit(False, str(e))
            return

        try:
            info = self._fourway.init_flash(index, retries=3)
            mcu = MCU_VARIANTS.get(info.signature)
            if mcu is None:
                raise FourWayError(f"unrecognized MCU signature 0x{info.signature:04X}")

            end_address = max((r.address + len(r.data) for r in records), default=mcu.flash_offset)
            image = build_flash_image(records, mcu.flash_offset, end_address - mcu.flash_offset)
            eeprom_wire = to_wire_address(mcu.eeprom_offset)
            firmware_bytes = image[mcu.firmware_start :]

            self._log("info", f"ESC #{index + 1}: прошивка {len(firmware_bytes)} байт firmware...")

            # Clear the boot bit before writing, so the ESC stays in the
            # bootloader (won't try to jump into a half-written application)
            # if flashing is interrupted partway - matches
            # am32-configurator's writeHex() exactly (four_way.ts).
            settings = bytearray(self._fourway.read(eeprom_wire, LAYOUT_SIZE))
            boot_offset = EEPROM_LAYOUT["BOOT_BYTE"].offset
            settings[boot_offset] = 0x00
            self._fourway.write(eeprom_wire, bytes(settings))

            self._fourway.write_image(
                to_wire_address(mcu.firmware_start),
                firmware_bytes,
                progress_cb=lambda done, total: self.flash_progress.emit(done, total),
            )

            settings[boot_offset] = 0x01
            self._fourway.write(eeprom_wire, bytes(settings))
        except Exception as e:
            self._log("error", f"ESC #{index + 1}: прошивка firmware не вдалась: {e}")
            self.flash_finished.emit(False, str(e))
            return

        self._log("success", f"ESC #{index + 1}: firmware прошито - має перезавантажитись і встановити бутлоадер.")
        self.flash_finished.emit(True, "")
