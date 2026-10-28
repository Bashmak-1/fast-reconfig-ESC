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


def explain_serial_error(e: BaseException) -> tuple[str, bool]:
    """(i18n key of a human explanation, transient?) for a failed port open / handshake. The raw
    pyserial/Windows text ("could not open port ... PermissionError(13)") means nothing to an operator;
    "transient" = normally clears by itself within seconds (e.g. right after an FC reboot)."""
    winerror = getattr(e, "winerror", None) or getattr(e, "errno", None)
    text = str(e).lower()
    if isinstance(e, serial.SerialTimeoutException) or "write timeout" in text:
        return "err_fc_no_response", False
    if isinstance(e, FileNotFoundError) or winerror in (2, 433) or "does not exist" in text:
        return "err_port_not_ready", True
    if isinstance(e, PermissionError) or winerror in (5, 31):
        return "err_port_busy", True
    if winerror == 121 or "semaphore" in text:
        return "err_port_dead", False
    return "", False


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


READ_SLICE_S = 0.02  # one blocking read waits at most this long, so the deadline is honoured within ~20 ms


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
        self._serial.timeout = READ_SLICE_S
        self._serial.write_timeout = 1.0
        self._serial.open()

    @property
    def is_open(self) -> bool:
        return self._serial.is_open

    def close(self) -> None:
        if self._serial.is_open:
            self._serial.close()

    def write(self, data: bytes) -> None:
        # no flush(): on Windows it is FlushFileBuffers, which some USB-serial drivers answer only after ~100+ ms
        # (real log: every MSP exchange took 140 ms instead of 10 ms) - write() already waits for the driver to take the data
        self._serial.write(data)

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
        # The port timeout is fixed (set once in __init__) and the deadline is enforced here: assigning
        # `serial.timeout` makes pyserial re-run SetCommState, which usbser.sys turns into a USB control
        # transfer (~25 ms) - four reads per MSP response were the whole 105 ms round trip seen in real logs.
        deadline = time.monotonic() + max(timeout_s, 0.0)
        data = b""
        while True:
            data += self._serial.read(n - len(data))
            if len(data) >= n:
                return data
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out reading {n} bytes (got {len(data)})")
