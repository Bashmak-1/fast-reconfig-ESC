"""MSP (MultiWii Serial Protocol) v1/v2 framing.

Byte layout verified against the official @am32/serial-msp package
(dist/msp/codec.js, dist/msp/parser.js, dist/msp/commands.js) shipped by
am32-firmware/am32-configurator - not reverse-engineered/guessed.

Only MSP v1 is actually exercised by the FC-passthrough workflow (every
command used has an id <= 254), but v2 encode is included for completeness.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import IntEnum

from .serial_transport import SerialTransport, remaining_time


class MspCommand(IntEnum):
    MSP_API_VERSION = 1
    MSP_FC_VARIANT = 2
    MSP_BATTERY_STATE = 130
    MSP_MOTOR = 104
    MSP_SET_PASSTHROUGH = 245  # 0xF5 - send with an EMPTY payload (legacy ESC_4WAY trigger)


@dataclass
class MspResponse:
    command: int
    payload: bytes


class MspError(Exception):
    pass


class MspFramingError(MspError):
    pass


def crc8_dvb_s2(data: bytes, start: int = 0, end: int | None = None) -> int:
    if end is None:
        end = len(data)
    crc = 0
    for i in range(start, end):
        crc ^= data[i]
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) & 0xFF) ^ 0xD5
            else:
                crc = (crc << 1) & 0xFF
    return crc


def encode_msp_v1(command: int, payload: bytes = b"") -> bytes:
    if len(payload) > 255:
        raise ValueError("MSP v1 payload too long (max 255 bytes)")
    buf = bytearray(b"$M<")
    buf.append(len(payload))
    buf.append(command & 0xFF)
    checksum = buf[3] ^ buf[4]
    buf += payload
    for b in payload:
        checksum ^= b
    buf.append(checksum & 0xFF)
    return bytes(buf)


def encode_msp_v2(command: int, payload: bytes = b"") -> bytes:
    buf = bytearray(b"$X<")
    buf.append(0)  # flag
    buf.append(command & 0xFF)
    buf.append((command >> 8) & 0xFF)
    buf.append(len(payload) & 0xFF)
    buf.append((len(payload) >> 8) & 0xFF)
    buf += payload
    crc = crc8_dvb_s2(bytes(buf), 3, len(buf))
    buf.append(crc)
    return bytes(buf)


def encode_msp(command: int, payload: bytes = b"") -> bytes:
    return encode_msp_v1(command, payload) if command <= 254 else encode_msp_v2(command, payload)


def _read_msp_response(transport: SerialTransport, timeout_s: float) -> MspResponse:
    deadline = time.monotonic() + timeout_s
    preamble = transport.read_exact(3, remaining_time(deadline))
    if preamble[0:2] != b"$M" or preamble[2] not in (ord("<"), ord(">"), ord("!")):
        raise MspFramingError(f"bad MSP preamble: {preamble!r}")

    header = transport.read_exact(2, remaining_time(deadline))
    length, command = header[0], header[1]
    payload = transport.read_exact(length, remaining_time(deadline)) if length else b""
    checksum_byte = transport.read_exact(1, remaining_time(deadline))[0]

    checksum = length ^ command
    for b in payload:
        checksum ^= b
    if checksum != checksum_byte:
        raise MspFramingError(
            f"MSP checksum mismatch for command {command}: expected {checksum:#x}, got {checksum_byte:#x}"
        )
    if preamble[2] == ord("!"):
        raise MspFramingError(f"MSP error response for command {command}")

    return MspResponse(command=command, payload=payload)


class MspClient:
    def __init__(self, transport: SerialTransport) -> None:
        self._transport = transport

    def send(
        self,
        command: int,
        payload: bytes = b"",
        timeout_s: float = 0.3,
        retries: int = 1,
        retry_delay_s: float = 0.05,
    ) -> MspResponse:
        frame = encode_msp(int(command), payload)
        last_error: Exception | None = None
        for attempt in range(max(retries, 1)):
            try:
                self._transport.reset_input_buffer()
                self._transport.write(frame)
                return _read_msp_response(self._transport, timeout_s)
            except (TimeoutError, MspFramingError) as e:
                last_error = e
                if attempt + 1 < retries:
                    time.sleep(retry_delay_s)
        raise MspError(f"MSP command {int(command)} failed: {last_error}")
