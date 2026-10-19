"""Tests for msp.py - specifically MspClient.send()'s command-match check,
the fix for a real-hardware-confirmed desync: right after
esc_manager.py's FourWayClient.exit_interface() recovery retry, a stale
response frame left over from an earlier exchange could get silently
returned as if it answered the NEXT command sent, putting every later
read in that handshake one frame behind what it actually asked for.
"""
from __future__ import annotations

import pytest

from esc_hotline.msp import MspClient, MspCommand, MspError, encode_msp


class _FakeTransport:
    """Bytes-in, bytes-out stand-in for SerialTransport - no real timing,
    read_exact() just slices off the front of a pre-queued byte stream and
    raises TimeoutError once it runs out, regardless of timeout_s."""

    def __init__(self, stream: bytes) -> None:
        self._stream = stream

    def reset_input_buffer(self) -> None:
        pass

    def write(self, data: bytes) -> None:
        pass

    def read_exact(self, n: int, timeout_s: float) -> bytes:
        if len(self._stream) < n:
            raise TimeoutError(f"not enough bytes: wanted {n}, have {len(self._stream)}")
        chunk, self._stream = self._stream[:n], self._stream[n:]
        return chunk


def test_send_discards_stale_mismatched_response_and_returns_matching_one():
    stale = encode_msp(int(MspCommand.MSP_FC_VARIANT), b"INAV")
    real = encode_msp(int(MspCommand.MSP_API_VERSION), b"\x00\x02\x05")
    client = MspClient(_FakeTransport(stale + real))

    response = client.send(MspCommand.MSP_API_VERSION, timeout_s=1.0)

    assert response.command == int(MspCommand.MSP_API_VERSION)
    assert response.payload == b"\x00\x02\x05"


def test_send_fails_if_only_mismatched_frames_ever_arrive():
    stale = encode_msp(int(MspCommand.MSP_FC_VARIANT), b"INAV")
    client = MspClient(_FakeTransport(stale))

    with pytest.raises(MspError):
        client.send(MspCommand.MSP_API_VERSION, timeout_s=0.05, retries=1)


def test_send_returns_immediately_matching_response_unchanged():
    real = encode_msp(int(MspCommand.MSP_BATTERY_STATE), b"\x01\x02")
    client = MspClient(_FakeTransport(real))

    response = client.send(MspCommand.MSP_BATTERY_STATE, timeout_s=1.0)

    assert response.command == int(MspCommand.MSP_BATTERY_STATE)
    assert response.payload == b"\x01\x02"
