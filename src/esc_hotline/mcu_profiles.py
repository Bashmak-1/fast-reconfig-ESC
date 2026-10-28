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
    firmware_start: int | None = None  # byte offset of the application; None = firmware flashing not supported


MCU_VARIANTS: dict[int, McuVariant] = {
    0x1F06: McuVariant("STM32F051", eeprom_offset=0x7C00, flash_offset=0x08000000, page_size=1024, firmware_start=0x1000),
    0x3506: McuVariant("ARM64K", eeprom_offset=0xF800, flash_offset=0x08000000, page_size=1024, firmware_start=0x1000),
    0x1506: McuVariant("NXP ESC_8KB_PAGE", eeprom_offset=0xE000, flash_offset=0x08000000, page_size=1024),
}

LAYOUT_SIZE = 0xB8  # 184 bytes - the whole settings block, no embedded checksum
