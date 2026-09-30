"""Tests for firmware_image.py (Intel HEX parsing / flash-image assembly for
the experimental "UID Lab" firmware-flashing feature) - no real hardware.

_hex_line() below builds syntactically-correct Intel HEX records with a
correct checksum, so tests exercise the same parsing path parse_intel_hex()
uses on a real file, rather than hand-typing checksums.
"""
from __future__ import annotations

import pytest

from esc_hotline.firmware_image import (
    HexParseError,
    HexRecord,
    build_flash_image,
    parse_intel_hex,
    to_wire_address,
)


def _hex_line(record_type: int, address: int, data: bytes = b"") -> str:
    payload = bytes([len(data), (address >> 8) & 0xFF, address & 0xFF, record_type]) + data
    checksum = (-sum(payload)) & 0xFF
    return ":" + payload.hex().upper() + f"{checksum:02X}"


def _data_record(address: int, data: bytes) -> str:
    return _hex_line(0x00, address, data)


def _eof_record() -> str:
    return _hex_line(0x01, 0x0000)


def _extended_linear_address_record(high_16_bits: int) -> str:
    return _hex_line(0x04, 0x0000, bytes([(high_16_bits >> 8) & 0xFF, high_16_bits & 0xFF]))


# ---- parse_intel_hex ------------------------------------------------


def test_parse_single_data_record():
    text = "\n".join([_data_record(0x1000, bytes([1, 2, 3, 4])), _eof_record()])
    records = parse_intel_hex(text)
    assert len(records) == 1
    assert records[0].address == 0x1000
    assert bytes(records[0].data) == bytes([1, 2, 3, 4])


def test_parse_two_adjacent_records_are_readable_regardless_of_grouping():
    # Whether two back-to-back data lines merge into one HexRecord or stay
    # as two is an internal bookkeeping detail (see firmware_image.py's
    # docstring on the deliberate simplification vs. the reference client's
    # exact quirk) - build_flash_image() must place every byte at its
    # correct absolute address either way.
    text = "\n".join(
        [
            _data_record(0x1000, bytes([0xAA, 0xBB])),
            _data_record(0x1002, bytes([0xCC, 0xDD])),
            _eof_record(),
        ]
    )
    records = parse_intel_hex(text)
    image = build_flash_image(records, flash_offset=0x1000, size=4)
    assert image == bytes([0xAA, 0xBB, 0xCC, 0xDD])


def test_parse_extended_linear_address_offsets_later_records():
    text = "\n".join(
        [
            _extended_linear_address_record(0x0001),  # upper 16 bits = 0x0001
            _data_record(0x0004, bytes([0x42])),  # absolute address = 0x00010004
            _eof_record(),
        ]
    )
    records = parse_intel_hex(text)
    assert len(records) == 1
    assert records[0].address == 0x00010004


def test_parse_rejects_bad_checksum():
    good = _data_record(0x0000, bytes([1, 2, 3]))
    corrupted = good[:-2] + "00"  # stomp the checksum with a wrong value
    text = "\n".join([corrupted, _eof_record()])
    with pytest.raises(HexParseError):
        parse_intel_hex(text)


def test_parse_requires_end_of_file_record():
    text = _data_record(0x0000, bytes([1, 2, 3]))  # no EOF record
    with pytest.raises(HexParseError):
        parse_intel_hex(text)


def test_parse_rejects_malformed_line():
    with pytest.raises(HexParseError):
        parse_intel_hex("not a hex line at all")


def test_parse_handles_trailing_newline_and_crlf():
    text = "\r\n".join([_data_record(0x0000, bytes([9])), _eof_record()]) + "\r\n"
    records = parse_intel_hex(text)
    assert bytes(records[0].data) == bytes([9])


# ---- build_flash_image ------------------------------------------------


def test_build_flash_image_fills_gaps_with_0xff():
    records = [HexRecord(address=0x1002, data=bytearray([0xAA, 0xBB]))]
    image = build_flash_image(records, flash_offset=0x1000, size=6)
    assert image == bytes([0xFF, 0xFF, 0xAA, 0xBB, 0xFF, 0xFF])


def test_build_flash_image_custom_fill_byte():
    image = build_flash_image([], flash_offset=0, size=4, fill=0x00)
    assert image == bytes([0x00] * 4)


def test_build_flash_image_rejects_record_before_flash_offset():
    records = [HexRecord(address=0x0FF0, data=bytearray([0x01]))]  # below flash_offset
    with pytest.raises(ValueError):
        build_flash_image(records, flash_offset=0x1000, size=16)


def test_build_flash_image_rejects_record_past_end():
    records = [HexRecord(address=0x1010, data=bytearray([0x01]))]  # at/past size boundary
    with pytest.raises(ValueError):
        build_flash_image(records, flash_offset=0x1000, size=16)


def test_build_flash_image_real_updater_shape():
    """Mirrors the actual real-hardware validated shape: a bootloader-updater
    .hex whose only record starts at FIRMWARE_RELATIVE_START (0x1000), built
    relative to the chip's overall flash_offset - the same call pattern
    UidLabManager.flash_firmware() uses (image[mcu.firmware_start:] after
    build_flash_image(records, mcu.flash_offset, ...))."""
    firmware_start = 0x1000
    flash_offset = 0x08000000
    payload = bytes(range(16))  # stand-in for real updater bytes
    records = [HexRecord(address=flash_offset + firmware_start, data=bytearray(payload))]
    end_address = flash_offset + firmware_start + len(payload)
    image = build_flash_image(records, flash_offset, end_address - flash_offset)
    assert image[:firmware_start] == b"\xff" * firmware_start
    assert image[firmware_start:] == payload


# ---- to_wire_address ------------------------------------------------


def test_to_wire_address_no_shift_is_passthrough():
    assert to_wire_address(0xF800, address_shift=0) == 0xF800


def test_to_wire_address_applies_shift():
    assert to_wire_address(0x20000, address_shift=2) == 0x8000


def test_to_wire_address_rejects_misaligned_offset():
    with pytest.raises(ValueError):
        to_wire_address(0x20001, address_shift=2)


def test_to_wire_address_rejects_overflow_of_16_bit_range():
    with pytest.raises(ValueError):
        to_wire_address(0x10000, address_shift=0)


def test_to_wire_address_accepts_max_16_bit_value():
    assert to_wire_address(0xFFFF, address_shift=0) == 0xFFFF
