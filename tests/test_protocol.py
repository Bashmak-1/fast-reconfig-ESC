"""Protocol-layer unit tests - run before ever touching real hardware.

CRC test vectors are the standard "123456789" check values from the CRC
RevEng catalogue for CRC-16/XMODEM (0x31C3) and CRC-8/DVB-S2 (0xBC), used
here purely to validate the from-scratch implementations against known
external constants, independent of the AM32/INAV source review.
"""
from __future__ import annotations

import pytest

from fast_reconfig_esc.eeprom_config import ConfigFileError, load_config_file
from fast_reconfig_esc.fourway import (
    DEVICE_START_BYTE,
    FourWayAck,
    FourWayCommand,
    FourWayFramingError,
    crc16_xmodem,
    make_frame,
    parse_frame,
)
from fast_reconfig_esc.mcu_profiles import LAYOUT_SIZE
from fast_reconfig_esc.msp import crc8_dvb_s2, encode_msp_v1


CHECK_VECTOR = b"123456789"


def test_crc16_xmodem_known_check_value():
    assert crc16_xmodem(CHECK_VECTOR) == 0x31C3


def test_crc8_dvb_s2_known_check_value():
    assert crc8_dvb_s2(CHECK_VECTOR) == 0xBC


def test_msp_v1_encode_known_bytes():
    # $ M < len=0 cmd=1 checksum=(0^1)=1
    assert encode_msp_v1(1, b"") == b"$M<\x00\x01\x01"


def test_msp_v1_encode_with_payload_checksum():
    frame = encode_msp_v1(200, b"\x01\x02\x03")
    assert frame[:3] == b"$M<"
    assert frame[3] == 3  # length
    assert frame[4] == 200  # command
    expected_checksum = 3 ^ 200 ^ 0x01 ^ 0x02 ^ 0x03
    assert frame[-1] == expected_checksum


def test_fourway_make_frame_structure():
    frame = make_frame(int(FourWayCommand.DeviceInitFlash), bytes([2]), address=0)
    assert frame[0] == 0x2F
    assert frame[1] == int(FourWayCommand.DeviceInitFlash)
    assert frame[2:4] == b"\x00\x00"  # address hi, lo
    assert frame[4] == 1  # length
    assert frame[5] == 2  # param = esc index
    assert len(frame) == 7 + 1  # header(5) + params(1) + crc(2)


def test_fourway_frame_roundtrip():
    # Simulate an ESC response to DeviceInitFlash: signature=0x1F06 (LE),
    # input=0x04, interfaceMode=0x00, ack=OK.
    params = bytes([0x06, 0x1F, 0x04, 0x00])
    header = bytes(
        [
            DEVICE_START_BYTE,
            int(FourWayCommand.DeviceInitFlash),
            0x00,
            0x00,  # address
            len(params),
        ]
    )
    body = header + params + bytes([int(FourWayAck.ACK_OK)])
    crc = crc16_xmodem(body)
    frame = body + bytes([(crc >> 8) & 0xFF, crc & 0xFF])

    response = parse_frame(frame)
    assert response.command == int(FourWayCommand.DeviceInitFlash)
    assert response.address == 0
    assert response.params == params
    assert response.ack == int(FourWayAck.ACK_OK)


def test_fourway_frame_bad_crc_raises():
    params = bytes([0x06, 0x1F, 0x04, 0x00])
    header = bytes([DEVICE_START_BYTE, int(FourWayCommand.DeviceInitFlash), 0x00, 0x00, len(params)])
    body = header + params + bytes([int(FourWayAck.ACK_OK)])
    frame = body + b"\x00\x00"  # deliberately wrong CRC

    with pytest.raises(FourWayFramingError):
        parse_frame(frame)


def test_fourway_frame_bad_start_byte_raises():
    params = bytes([0x00])
    header = bytes([0x99, int(FourWayCommand.DeviceRead), 0x00, 0x00, len(params)])
    body = header + params + bytes([int(FourWayAck.ACK_OK)])
    crc = crc16_xmodem(body)
    frame = body + bytes([(crc >> 8) & 0xFF, crc & 0xFF])

    with pytest.raises(FourWayFramingError):
        parse_frame(frame)


def test_load_config_file_rejects_wrong_length(tmp_path):
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"\x00" * 10)
    with pytest.raises(ConfigFileError):
        load_config_file(bad)


def test_load_config_file_accepts_correct_length(tmp_path):
    good = tmp_path / "good.bin"
    good.write_bytes(b"\xff" * LAYOUT_SIZE)
    data = load_config_file(good)
    assert len(data) == LAYOUT_SIZE


def test_load_config_file_missing_raises(tmp_path):
    with pytest.raises(ConfigFileError):
        load_config_file(tmp_path / "does_not_exist.bin")
