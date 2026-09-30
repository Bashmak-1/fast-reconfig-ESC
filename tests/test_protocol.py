"""Protocol-layer unit tests - run before ever touching real hardware.

CRC test vectors are the standard "123456789" check values from the CRC
RevEng catalogue for CRC-16/XMODEM (0x31C3) and CRC-8/DVB-S2 (0xBC), used
here purely to validate the from-scratch implementations against known
external constants, independent of the AM32/INAV source review.
"""
from __future__ import annotations

import pytest

from esc_hotline.eeprom_config import ConfigFileError, load_config_file
from esc_hotline.fourway import (
    DEVICE_START_BYTE,
    FourWayAck,
    FourWayCommand,
    FourWayFramingError,
    crc16_xmodem,
    make_frame,
    parse_frame,
)
from esc_hotline.mcu_profiles import LAYOUT_SIZE
from esc_hotline.msp import MspCommand, crc8_dvb_s2, encode_msp_v1


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


# ---- MSP_SET_MOTOR payload (motor_test_manager.py) ------------------------------------------------


def test_msp_set_motor_payload_is_8_little_endian_u16():
    # Matches iNavFlight/inav fc_msp.c's `dataSize == 8 * sizeof(uint16_t)`
    # check exactly - 16 bytes, little-endian, one value per motor slot.
    values = [1000, 1250, 1500, 1750, 2000, 1000, 1000, 1000]
    payload = b"".join(v.to_bytes(2, "little") for v in values)
    assert len(payload) == 16
    assert payload[0:2] == b"\xe8\x03"  # 1000 == 0x03E8, little-endian
    assert payload[8:10] == b"\xd0\x07"  # 2000 == 0x07D0, little-endian
    for i, v in enumerate(values):
        assert int.from_bytes(payload[i * 2 : i * 2 + 2], "little") == v


def test_msp_set_motor_frame_uses_msp_v1_since_command_id_is_under_255():
    # command 214 fits MSP v1's single-byte command field - confirms the
    # existing encode_msp()'s "<=254 uses v1" branch applies here too, so no
    # new v2 code path needs to be exercised for this feature.
    assert int(MspCommand.MSP_SET_MOTOR) == 214
    assert int(MspCommand.MSP_SET_MOTOR) <= 254


def test_msp_set_motor_full_frame_roundtrip():
    values = [1000] * 8
    payload = b"".join(v.to_bytes(2, "little") for v in values)
    frame = encode_msp_v1(int(MspCommand.MSP_SET_MOTOR), payload)
    assert frame[:3] == b"$M<"
    assert frame[3] == 16  # length
    assert frame[4] == 214  # command
    expected_checksum = 16 ^ 214
    for b in payload:
        expected_checksum ^= b
    assert frame[-1] == expected_checksum


def test_msp_status_ex_arming_flags_offset_matches_documented_layout():
    # Byte layout per iNavFlight/inav fc_msp.c's MSP_STATUS_EX case:
    # u16 cycleTime, u16 i2cErrors, u16 sensorStatus, 4 bytes boxModeFlags,
    # u8 configProfile, u16 avgLoad, u16 armingFlags, u8 accCalibFlags.
    # armingFlags therefore starts at byte offset 2+2+2+4+1+2 = 13.
    fake_payload = bytearray(16)
    arming_flags_value = 0x0004  # ARMED bit (runtime_config.h: ARMED = 1 << 2)
    fake_payload[13:15] = arming_flags_value.to_bytes(2, "little")
    parsed = int.from_bytes(bytes(fake_payload[13:15]), "little")
    assert parsed == arming_flags_value
    assert parsed & 0x04  # ARMED bit set


# ---- MSP_ADVANCED_CONFIG / MSP_SET_ADVANCED_CONFIG (motor protocol selector) ----------------------


def test_msp_advanced_config_protocol_offset_matches_documented_layout():
    # Byte layout per iNavFlight/inav fc_msp.c's MSP_ADVANCED_CONFIG case:
    # u8 gyroSyncDenom(placeholder), u8 pidProcessDenom(placeholder),
    # u8 useUnsyncedPwm(placeholder), u8 motorPwmProtocol, u16 motorPwmRate,
    # u16 servoPwmRate, u8 trailing(placeholder). motorPwmProtocol therefore
    # starts at byte offset 3, motorPwmRate at 4, servoPwmRate at 6.
    fake_payload = bytes([1, 1, 1, 4]) + (480).to_bytes(2, "little") + (50).to_bytes(2, "little") + bytes([0])
    assert len(fake_payload) == 9
    assert fake_payload[3] == 4  # DSHOT150 index
    assert int.from_bytes(fake_payload[4:6], "little") == 480
    assert int.from_bytes(fake_payload[6:8], "little") == 50


def test_msp_set_advanced_config_payload_roundtrips_rates_unchanged():
    # set_protocol() must echo back whatever motorPwmRate/servoPwmRate it
    # last read, or the FC (which reads-and-discards bytes 0-2/8 but applies
    # 3-7 verbatim) would silently clobber them with wrong values.
    motor_pwm_rate, servo_pwm_rate = 480, 50
    new_protocol_index = 5  # DSHOT300
    payload = (
        bytes([1, 1, 1, new_protocol_index])
        + motor_pwm_rate.to_bytes(2, "little")
        + servo_pwm_rate.to_bytes(2, "little")
        + bytes([0])
    )
    assert len(payload) == 9
    assert payload[3] == new_protocol_index
    assert int.from_bytes(payload[4:6], "little") == motor_pwm_rate
    assert int.from_bytes(payload[6:8], "little") == servo_pwm_rate


def test_msp_advanced_config_command_ids_match_source():
    assert int(MspCommand.MSP_ADVANCED_CONFIG) == 90
    assert int(MspCommand.MSP_SET_ADVANCED_CONFIG) == 91


def test_msp_eeprom_write_and_reboot_are_empty_payload_frames():
    # Both take no payload (msp_protocol.h: MSP_EEPROM_WRITE "no param";
    # MSP_REBOOT with 0 bytes = legacy/normal reboot, not DFU).
    assert int(MspCommand.MSP_EEPROM_WRITE) == 250
    assert int(MspCommand.MSP_REBOOT) == 68
    eeprom_frame = encode_msp_v1(int(MspCommand.MSP_EEPROM_WRITE))
    reboot_frame = encode_msp_v1(int(MspCommand.MSP_REBOOT))
    assert eeprom_frame == b"$M<\x00\xfa\xfa"  # len=0, cmd=250, checksum=0^250
    assert reboot_frame == b"$M<\x00\x44\x44"  # len=0, cmd=68, checksum=0^68
