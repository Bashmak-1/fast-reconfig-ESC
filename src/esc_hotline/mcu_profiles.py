"""Per-MCU flash/EEPROM layout table.

Values taken verbatim from am32-configurator's src/mcu.ts (signature ->
eeprom_offset/flash_offset/page_size) and Mcu.LAYOUT_SIZE. eeprom_offset must
always be resolved per-ESC from that ESC's own DeviceInitFlash signature -
never hardcoded globally, since different ESCs on the same aircraft can use
different MCUs.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class McuVariant:
    name: str
    eeprom_offset: int
    flash_offset: int
    page_size: int
    # Byte offset (from flash_offset) where the application/firmware image
    # starts - i.e. where the bootloader ends. Verified against
    # am32-configurator's src/mcu.ts (Mcu.variants[].firmware_start). Not
    # used by the existing config read/write path (only eeprom_offset is),
    # only by firmware_image.py / FourWayClient.write_image() for the
    # experimental "UID Lab" bootloader-updater flashing flow.
    firmware_start: int = 0x1000


MCU_VARIANTS: dict[int, McuVariant] = {
    0x1F06: McuVariant("STM32F051", eeprom_offset=0x7C00, flash_offset=0x08000000, page_size=1024, firmware_start=0x1000),
    0x3506: McuVariant("ARM64K", eeprom_offset=0xF800, flash_offset=0x08000000, page_size=1024, firmware_start=0x1000),
    # flash_offset is 0x0 here (not 0x08000000) per mcu.ts: "the MCXA153
    # flash really is at address 0" - previously left at the same default as
    # the other two, which was harmless only because nothing read this field
    # until now (only eeprom_offset, a separate value, is used by the
    # existing config read/write path).
    0x1506: McuVariant("NXP ESC_8KB_PAGE", eeprom_offset=0xE000, flash_offset=0x0, page_size=1024, firmware_start=0x4000),
}

LAYOUT_SIZE = 0xB8  # 184 bytes - the whole settings block, no embedded checksum
