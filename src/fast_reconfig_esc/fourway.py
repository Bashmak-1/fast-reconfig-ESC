"""FourWay Interface protocol (BLHeli/AM32 ESC bootloader link over MSP passthrough).

Byte layout and retry/timing parameters verified against the official
am32-firmware/am32-configurator source (src/communication/four_way.ts) -
not reverse-engineered/guessed. In particular:

- Request frame:  0x2F, cmd, addr_hi, addr_lo, len(0=256), params..., crc_hi, crc_lo
- Response frame: 0x2E, cmd, addr_hi, addr_lo, len(0=256), params..., ack, crc_hi, crc_lo
  (the ACK byte sits between params and the CRC, and IS covered by the CRC)
- CRC is CRC-16/XMODEM (poly 0x1021, init 0) over every byte except the
  trailing 2 CRC bytes themselves.
- Per-ESC addressing happens via cmd_DeviceInitFlash/cmd_DeviceReset's single
  param byte (the 0-based ESC index), address field = 0. The ESC bootloader
  remembers the selected target for subsequent Read/Write calls.
- Config read/write uses cmd_DeviceRead/cmd_DeviceWrite at an MCU-specific
  EEPROM offset - NOT cmd_DeviceReadEEprom/cmd_DeviceWriteEEprom, which AM32's
  bootloader does not implement for this purpose.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import IntEnum

from .serial_transport import SerialTransport, remaining_time

HOST_START_BYTE = 0x2F
DEVICE_START_BYTE = 0x2E


class FourWayCommand(IntEnum):
    InterfaceTestAlive = 0x30
    ProtocolGetVersion = 0x31
    InterfaceGetName = 0x32
    InterfaceGetVersion = 0x33
    InterfaceExit = 0x34
    DeviceReset = 0x35
    DeviceInitFlash = 0x37
    DeviceEraseAll = 0x38
    DevicePageErase = 0x39
    DeviceRead = 0x3A
    DeviceWrite = 0x3B  # config/EEPROM write goes here, NOT DeviceWriteEEprom
    DeviceC2CK_LOW = 0x3C
    DeviceReadEEprom = 0x3D  # unused by the config-write path; kept for future firmware-flash mode
    DeviceWriteEEprom = 0x3E  # unused by the config-write path
    InterfaceSetMode = 0x3F


class FourWayAck(IntEnum):
    ACK_OK = 0x00
    ACK_I_UNKNOWN_ERROR = 0x01
    ACK_I_INVALID_CMD = 0x02
    ACK_I_INVALID_CRC = 0x03
    ACK_I_VERIFY_ERROR = 0x04
    ACK_D_INVALID_COMMAND = 0x05
    ACK_D_COMMAND_FAILED = 0x06
    ACK_D_UNKNOWN_ERROR = 0x07
    ACK_I_INVALID_CHANNEL = 0x08
    ACK_I_INVALID_PARAM = 0x09
    ACK_D_GENERAL_ERROR = 0x0F


class FourWayError(Exception):
    pass


class FourWayFramingError(FourWayError):
    pass


class FourWayAckError(FourWayError):
    def __init__(self, ack: int) -> None:
        self.ack = ack
        try:
            name = FourWayAck(ack).name
        except ValueError:
            name = f"0x{ack:02X}"
        super().__init__(f"ESC returned ACK error: {name}")


@dataclass
class FourWayResponse:
    command: int
    address: int
    params: bytes
    ack: int


@dataclass
class DeviceInfo:
    signature: int
    input_pin: int
    interface_mode: int


def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def make_frame(cmd: int, params: bytes = b"\x00", address: int = 0) -> bytes:
    if len(params) == 0:
        params = b"\x00"
    if len(params) > 256:
        raise ValueError("too many FourWay params (max 256)")
    length_byte = 0 if len(params) == 256 else len(params)

    buf = bytearray()
    buf.append(HOST_START_BYTE)
    buf.append(cmd & 0xFF)
    buf.append((address >> 8) & 0xFF)
    buf.append(address & 0xFF)
    buf.append(length_byte)
    buf += params

    crc = crc16_xmodem(bytes(buf))
    buf.append((crc >> 8) & 0xFF)
    buf.append(crc & 0xFF)
    return bytes(buf)


def parse_frame(buf: bytes) -> FourWayResponse:
    if len(buf) < 9:
        raise FourWayFramingError("frame too short")
    if buf[0] != DEVICE_START_BYTE:
        raise FourWayFramingError(f"bad FourWay start byte: {buf[0]:#x}")

    param_count = buf[4] or 256
    expected_len = 8 + param_count
    if len(buf) < expected_len:
        raise FourWayFramingError("incomplete FourWay frame")

    params = buf[5 : 5 + param_count]
    ack = buf[5 + param_count]
    crc_received = (buf[6 + param_count] << 8) | buf[7 + param_count]
    crc_calculated = crc16_xmodem(buf[0 : 6 + param_count])
    if crc_received != crc_calculated:
        raise FourWayFramingError(
            f"FourWay CRC mismatch: received {crc_received:#06x}, calculated {crc_calculated:#06x}"
        )

    return FourWayResponse(
        command=buf[1],
        address=(buf[2] << 8) | buf[3],
        params=bytes(params),
        ack=ack,
    )


class FourWayClient:
    def __init__(self, transport: SerialTransport) -> None:
        self._transport = transport

    def _exchange(self, cmd: int, params: bytes, address: int, timeout_s: float) -> FourWayResponse:
        frame = make_frame(cmd, params, address)
        self._transport.reset_input_buffer()
        self._transport.write(frame)

        deadline = time.monotonic() + timeout_s
        header = self._transport.read_exact(5, remaining_time(deadline))
        if header[0] != DEVICE_START_BYTE:
            raise FourWayFramingError(f"bad FourWay start byte: {header[0]:#x}")
        param_count = header[4] or 256
        rest = self._transport.read_exact(param_count + 3, remaining_time(deadline))
        return parse_frame(header + rest)

    def send(
        self,
        cmd: FourWayCommand,
        params: bytes = b"\x00",
        address: int = 0,
        retries: int = 10,
        timeout_s: float = 0.2,
        retry_delay_s: float = 0.25,
    ) -> FourWayResponse:
        last_error: Exception = FourWayError("max retries reached")
        for attempt in range(retries):
            try:
                response = self._exchange(int(cmd), params, address, timeout_s)
            except (TimeoutError, FourWayFramingError) as e:
                last_error = e
                time.sleep(retry_delay_s)
                continue
            if response.ack == FourWayAck.ACK_OK:
                return response
            last_error = FourWayAckError(response.ack)
            time.sleep(retry_delay_s)
        raise last_error

    def init_flash(self, esc_index: int, retries: int = 10) -> DeviceInfo:
        response = self.send(FourWayCommand.DeviceInitFlash, bytes([esc_index]), address=0, retries=retries)
        if len(response.params) < 4:
            raise FourWayFramingError("DeviceInitFlash response too short")
        # signature is little-endian on the wire (confirmed against four_way.ts / flash.ts)
        signature = response.params[0] | (response.params[1] << 8)
        return DeviceInfo(signature=signature, input_pin=response.params[2], interface_mode=response.params[3])

    def read(self, address: int, length: int, retries: int = 10, timeout_s: float = 0.2) -> bytes:
        param = bytes([0 if length == 256 else length])
        response = self.send(FourWayCommand.DeviceRead, param, address=address, retries=retries, timeout_s=timeout_s)
        return response.params

    def write(self, address: int, data: bytes, retries: int = 10, timeout_s: float = 0.3) -> FourWayResponse:
        return self.send(FourWayCommand.DeviceWrite, data, address=address, retries=retries, timeout_s=timeout_s)

    def reset(self, esc_index: int) -> None:
        try:
            self.send(FourWayCommand.DeviceReset, bytes([esc_index]), address=0, retries=1)
        except FourWayError:
            pass

    def exit_interface(self) -> None:
        # Fire-and-forget, matches reference (no ACK wait on InterfaceExit).
        try:
            self._transport.write(make_frame(int(FourWayCommand.InterfaceExit), b"\x00", 0))
        except Exception:
            pass
