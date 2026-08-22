"""Low-level serial transport: pyserial wrapper with deadline-aware reads,
plus FC-port discovery hints.

USB VID list and its FC/direct-connect split are taken verbatim from the
official am32-configurator source (components/SerialDevice.vue).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import serial
from serial.tools import list_ports

# USB vendor IDs seen on flight controllers (native STM32 CDC-ACM, RP2040,
# pid.codes-shared, Betaflight's own VID, etc.) - used only to pre-highlight
# a likely port, never to hard-filter the list.
FC_VENDOR_IDS = {0x0483, 0x2E3C, 0x2E8A, 0x1209, 0x26AC, 0x27AC, 0x2DAE, 0x3162, 0x35A7}


def remaining_time(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


@dataclass
class PortInfo:
    device: str
    description: str
    vid: Optional[int]
    likely_fc: bool


def list_fc_ports() -> list[PortInfo]:
    """Enumerate serial ports, flagging the ones that look like a flight controller."""
    ports = []
    for p in list_ports.comports():
        vid = p.vid
        ports.append(
            PortInfo(
                device=p.device,
                description=p.description or p.device,
                vid=vid,
                likely_fc=vid in FC_VENDOR_IDS if vid is not None else False,
            )
        )
    ports.sort(key=lambda info: (not info.likely_fc, info.device))
    return ports


class SerialTransport:
    """Thin pyserial wrapper exposing deadline-based exact-length reads.

    Every FourWay/MSP retry attempt gets its own timeout budget; a single
    ESC that never answers can only ever cost that attempt's timeout, never
    hang the transport indefinitely.
    """

    def __init__(self, port: str, baudrate: int = 115200) -> None:
        self._serial = serial.Serial()
        self._serial.port = port
        self._serial.baudrate = baudrate
        self._serial.timeout = 0.05
        self._serial.write_timeout = 1.0
        self._serial.open()

    @property
    def is_open(self) -> bool:
        return self._serial.is_open

    def close(self) -> None:
        if self._serial.is_open:
            self._serial.close()

    def write(self, data: bytes) -> None:
        self._serial.write(data)
        self._serial.flush()

    def reset_input_buffer(self) -> None:
        try:
            self._serial.reset_input_buffer()
        except Exception:
            # Best-effort only; some backends raise if the port is mid-teardown.
            pass

    def read_exact(self, n: int, timeout_s: float) -> bytes:
        """Read exactly n bytes, or raise TimeoutError if timeout_s elapses first."""
        if n == 0:
            return b""
        self._serial.timeout = max(timeout_s, 0.0)
        data = self._serial.read(n)
        if len(data) < n:
            raise TimeoutError(f"timed out reading {n} bytes (got {len(data)})")
        return data
