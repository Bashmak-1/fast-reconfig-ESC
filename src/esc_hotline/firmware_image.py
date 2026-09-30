"""Intel HEX parsing and flash-image building for the experimental "UID Lab"
firmware-flashing workflow (see uid_lab_manager.py).

Pure functions, no serial/Qt dependency - mirrors am32-configurator's
src/flash.ts (Flash.parseHex / Flash.fillImage) byte-for-byte, since getting
flash-image addressing wrong risks writing the wrong bytes to a real ESC.
Record types and the two's-complement checksum are per the standard Intel
HEX format; the encoding of extended linear address records (type 0x04) and
the flat-image assembly (relative to a caller-given flash_offset) are what's
verified against flash.ts specifically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


class HexParseError(Exception):
    pass


@dataclass
class HexRecord:
    address: int
    data: bytearray = field(default_factory=bytearray)


def parse_intel_hex(text: str) -> list[HexRecord]:
    """Parse an Intel HEX file's text into a list of contiguous data blocks
    (address = absolute 32-bit address of the block's first byte). Raises
    HexParseError on a checksum mismatch, an unterminated file (no end-of-file
    record), or an unsupported segment (record type 0x02 with a non-zero
    segment - the reference client doesn't handle that case either).
    """
    lines = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()

    records: list[HexRecord] = []
    extended_linear_address = 0
    next_address: Optional[int] = None
    end_of_file = False

    for line in lines:
        if not line.startswith(":") or len(line) < 11:
            raise HexParseError(f"malformed HEX line: {line!r}")
        byte_count = int(line[1:3], 16)
        address = int(line[3:7], 16)
        record_type = int(line[7:9], 16)
        content_hex = line[9 : 9 + byte_count * 2]
        checksum = int(line[9 + byte_count * 2 : 9 + byte_count * 2 + 2], 16)

        if record_type == 0x00:  # data record
            if next_address is None or address != next_address:
                records.append(HexRecord(address=extended_linear_address + address))
            next_address = address + byte_count

            crc = byte_count + int(line[3:5], 16) + int(line[5:7], 16) + record_type
            data = bytes.fromhex(content_hex)
            for b in data:
                crc += b
            crc = (-crc) & 0xFF
            if crc != checksum:
                raise HexParseError(f"checksum mismatch in HEX record: {line!r}")
            records[-1].data.extend(data)

        elif record_type == 0x01:  # end of file
            end_of_file = True

        elif record_type == 0x02:  # extended segment address
            if int(content_hex, 16) != 0:
                raise HexParseError("extended segment address record with non-zero segment - not supported")

        elif record_type == 0x03:  # start segment address - irrelevant to flashing
            pass

        elif record_type == 0x04:  # extended linear address
            extended_linear_address = int(content_hex, 16) << 16

        elif record_type == 0x05:  # start linear address - irrelevant to flashing
            pass
        # unknown record types are ignored, matching flash.ts

    if not end_of_file:
        raise HexParseError("HEX file has no end-of-file record")
    return records


def build_flash_image(records: list[HexRecord], flash_offset: int, size: int, fill: int = 0xFF) -> bytes:
    """Flatten parsed HEX records into one contiguous image of `size` bytes,
    addressed relative to flash_offset (so image[0] == the byte at absolute
    address flash_offset), with gaps filled by `fill` (0xFF, matching an
    erased flash cell). Raises ValueError if a record falls outside the
    image bounds - better than silently truncating firmware bytes.
    """
    image = bytearray([fill]) * size
    for record in records:
        offset = record.address - flash_offset
        if offset < 0 or offset >= size:
            raise ValueError(
                f"HEX record at absolute address 0x{record.address:X} is outside the "
                f"image (flash_offset=0x{flash_offset:X}, size=0x{size:X})"
            )
        end = min(offset + len(record.data), size)
        image[offset:end] = record.data[: end - offset]
    return bytes(image)


def to_wire_address(byte_offset: int, address_shift: int = 0) -> int:
    """Convert a physical byte offset (relative to flash_offset) into the
    16-bit CMD_SET_ADDRESS value the bootloader expects, mirroring
    am32-configurator's src/mcu.ts (Mcu.toWireAddress). address_shift is
    always 0 for every MCU currently in mcu_profiles.MCU_VARIANTS (only
    128k+ parts need a non-zero shift, and none of those are supported yet);
    kept as a parameter rather than hardcoded so a future 128k MCU entry
    doesn't need this function rewritten, only called differently.
    """
    if address_shift:
        align = (1 << address_shift) - 1
        if byte_offset & align:
            raise ValueError(f"0x{byte_offset:X} is not aligned to {1 << address_shift} bytes")
    wire = byte_offset >> address_shift
    if wire > 0xFFFF:
        raise ValueError(
            f"0x{byte_offset:X} exceeds the 16-bit wire address range with shift {address_shift}"
        )
    return wire
