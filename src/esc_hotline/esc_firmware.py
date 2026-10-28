"""AM32 firmware image for flashing an ESC through the FC's FourWay passthrough.

Protocol and layout follow am32-firmware/am32-configurator (src/flash.ts: parseHex/fillImage,
src/communication/four_way.ts: writeHex/writePages, src/mcu.ts: variants) - not guessed:
the image is built from the .hex starting at the MCU's flash offset, written from `firmware_start` in
256-byte DeviceWrite chunks (the bootloader erases pages itself, no DevicePageErase), with the settings
BOOT_BYTE cleared before and set again after, so an interrupted flash leaves the ESC in its bootloader.
Unlike the configurator, the image may never reach the settings block (`eeprom_offset`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .eeprom_layout import FILENAME_BLOCK_SIZE, parse_name_block
from .mcu_profiles import McuVariant

CHUNK = 256
MCU_FAMILIES = ("A153", "E230", "F031", "F051", "F415", "F421", "G031", "G071", "G431", "L431", "V203")


class HexError(ValueError):
    pass


def parse_hex(text: str) -> list[tuple[int, bytes]]:
    """Intel HEX -> [(absolute address, data)] with every record checksum verified; EOF record required."""
    blocks: list[tuple[int, bytearray]] = []
    upper = 0
    next_addr: Optional[int] = None
    eof = False
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise HexError(f"рядок {n}: немає ':' на початку")
        try:
            rec = bytes.fromhex(line[1:])
        except ValueError:
            raise HexError(f"рядок {n}: не hex-символи") from None
        if len(rec) < 5 or len(rec) != rec[0] + 5 or sum(rec) & 0xFF:
            raise HexError(f"рядок {n}: пошкоджений запис (довжина/контрольна сума)")
        count, addr, kind, data = rec[0], (rec[1] << 8) | rec[2], rec[3], rec[4:4 + rec[0]]
        if kind == 0x00:
            full = upper + addr
            if blocks and next_addr == full:
                blocks[-1][1].extend(data)
            else:
                blocks.append((full, bytearray(data)))
            next_addr = full + count
        elif kind == 0x01:
            eof = True
            break
        elif kind == 0x02:
            if int.from_bytes(data, "big"):
                raise HexError("segment-адреси (запис 02) не підтримуються")
        elif kind == 0x04:
            upper = int.from_bytes(data, "big") << 16
        elif kind not in (0x03, 0x05):
            raise HexError(f"рядок {n}: невідомий тип запису {kind:#04x}")
    if not eof:
        raise HexError("немає завершального запису (EOF) - файл обрізаний")
    if not blocks:
        raise HexError("у файлі немає даних")
    return [(a, bytes(d)) for a, d in blocks]


def build_image(blocks: list[tuple[int, bytes]], flash_offset: int) -> bytes:
    """Flat image from `flash_offset` to the last used byte, gaps 0xFF (erased flash)."""
    start = min(a for a, _ in blocks)
    if start < flash_offset:
        raise HexError(f"адреса {start:#x} нижче початку флешу {flash_offset:#x} - це файл не для цього MCU")
    end = max(a + len(d) for a, d in blocks)
    image = bytearray(b"\xff" * (end - flash_offset))
    for a, d in blocks:
        image[a - flash_offset:a - flash_offset + len(d)] = d
    return bytes(image)


def image_name(image: bytes, eeprom_offset: int) -> tuple[Optional[str], Optional[str]]:
    """(FILE_NAME, MCU type) stored by AM32 in the 32 bytes before the settings block, if the image has them."""
    if len(image) < eeprom_offset:
        return None, None
    return parse_name_block(image[eeprom_offset - FILENAME_BLOCK_SIZE:eeprom_offset])


def family_of(name: Optional[str]) -> Optional[str]:
    return next((t for t in (name or "").split("_") if t in MCU_FAMILIES), None)


@dataclass
class FirmwareImage:
    path: str
    variant: McuVariant
    image: bytes
    name: Optional[str]
    mcu_type: Optional[str]

    @property
    def size(self) -> int:
        return len(self.image) - self.variant.firmware_start


def load_firmware(path: str, variant: McuVariant) -> FirmwareImage:
    """Read + validate a .hex for `variant`; raises HexError with a reason the operator can act on."""
    if variant.firmware_start is None:
        raise HexError(f"запис прошивки для {variant.name} не підтримується")
    try:
        text = open(path, encoding="ascii", errors="strict").read()
    except (OSError, UnicodeDecodeError) as e:
        raise HexError(f"не вдалося прочитати файл: {e}") from None
    image = build_image(parse_hex(text), variant.flash_offset)
    if len(image) <= variant.firmware_start:
        raise HexError("у файлі немає коду прошивки (усе нижче початку застосунку)")
    if len(image) > variant.eeprom_offset:
        raise HexError(
            f"прошивка сягає {len(image):#x}, а блок налаштувань ESC починається з {variant.eeprom_offset:#x} - "
            "запис його б затер. Файл не для цього типу MCU."
        )
    name, mcu_type = image_name(image, variant.eeprom_offset)
    return FirmwareImage(path, variant, image, name, mcu_type)


def compatibility(fw: FirmwareImage, esc_name: Optional[str]) -> tuple[str, str]:
    """('ok'|'warn'|'block', why) for flashing `fw` into an ESC currently reporting `esc_name`."""
    fw_family, esc_family = family_of(fw.name), family_of(esc_name)
    if not fw.name:
        return "warn", "у файлі немає назви плати - не можна звірити з ESC"
    if not esc_name:
        return "warn", "ESC не повідомив назву своєї прошивки - звірити неможливо"
    if fw.name == esc_name:
        return "ok", f"та сама плата: {esc_name}"
    if fw_family and esc_family and fw_family != esc_family:
        return "block", f"інший MCU: у файлі {fw_family} ({fw.name}), в ESC {esc_family} ({esc_name})"
    return "warn", f"інша назва плати: у файлі {fw.name}, в ESC {esc_name}"
