"""AM32 firmware flashing: hex parser/image/compatibility and the per-ESC write (FakeFourWay)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from esc_hotline import esc_firmware as fw
from esc_hotline import esc_manager as mgr_mod
from esc_hotline.mcu_profiles import MCU_VARIANTS
from esc_hotline.models import EscInfo, ProbeResult

ARM64K = MCU_VARIANTS[0x3506]


def _rec(addr: int, kind: int, data: bytes = b"") -> str:
    body = bytes([len(data), addr >> 8, addr & 0xFF, kind]) + data
    return ":" + (body + bytes([(-sum(body)) & 0xFF])).hex().upper()


def _hex(start: int, data: bytes) -> str:
    lines = [_rec(0, 4, (0x0800).to_bytes(2, "big"))]
    for i in range(0, len(data), 16):
        lines.append(_rec(start + i, 0, data[i:i + 16]))
    lines.append(_rec(0, 1))
    return "\n".join(lines)


def _firmware_text(name: str = "FLYCOLOR_HV3_G071", end: int = 0xF800) -> str:
    code = bytearray(b"\xAA" * (end - 0x1000))
    code[-32:] = name.encode().ljust(32, b"\x00")
    return _hex(0x1000, bytes(code))


def test_parse_and_image():
    blocks = fw.parse_hex(_firmware_text())
    img = fw.build_image(blocks, 0x08000000)
    assert len(img) == 0xF800 and img[0x1000] == 0xAA and img[0] == 0xFF


def test_bad_checksum_and_missing_eof_rejected():
    good = _firmware_text().splitlines()
    good[3] = good[3][:-2] + ("00" if good[3][-2:] != "00" else "01")
    with pytest.raises(fw.HexError):
        fw.parse_hex("\n".join(good))
    with pytest.raises(fw.HexError):
        fw.parse_hex("\n".join(_firmware_text().splitlines()[:-1]))


def test_load_rejects_image_reaching_settings_block(tmp_path):
    p = tmp_path / "big.hex"
    p.write_text(_firmware_text(end=0xF820), encoding="ascii")
    with pytest.raises(fw.HexError, match="блок налаштувань"):
        fw.load_firmware(str(p), ARM64K)


def test_compatibility_verdicts(tmp_path):
    p = tmp_path / "ok.hex"
    p.write_text(_firmware_text(), encoding="ascii")
    img = fw.load_firmware(str(p), ARM64K)
    assert img.name == "FLYCOLOR_HV3_G071"
    assert fw.compatibility(img, "FLYCOLOR_HV3_G071")[0] == "ok"
    assert fw.compatibility(img, "OTHER_G071")[0] == "warn"
    assert fw.compatibility(img, "OTHER_F051")[0] == "block"
    assert fw.compatibility(img, None)[0] == "warn"


class _FakeFlashFourWay:
    def __init__(self, corrupt: bool = False):
        self.mem: dict[int, int] = {}
        self.corrupt = corrupt
        self.settings_writes: list[bytes] = []

    def init_flash(self, index, retries=10):
        return SimpleNamespace(signature=0x3506, input_pin=4, interface_mode=0)

    def read(self, address, length, retries=10, timeout_s=0.2):
        if address == ARM64K.eeprom_offset:
            return self.settings_writes[-1] if self.settings_writes else b"\x01" * 184
        return bytes(self.mem.get(address + i, 0xFF) for i in range(length))

    def write(self, address, data, retries=10, timeout_s=0.3):
        if address == ARM64K.eeprom_offset:
            self.settings_writes.append(bytes(data))
            return
        for i, b in enumerate(data):
            self.mem[address + i] = b ^ 1 if self.corrupt and i == 0 else b


def _manager(fake, esc_name="FLYCOLOR_HV3_G071"):
    m = mgr_mod.EscManager()
    m._fourway = fake
    m._probe_results = {0: ProbeResult(ok=True, esc_info=EscInfo(mcu_family="ARM64K", firmware_name=esc_name))}
    return m


def test_flash_writes_verifies_and_toggles_boot_byte(tmp_path):
    p = tmp_path / "fw.hex"
    p.write_text(_firmware_text(), encoding="ascii")
    fake = _FakeFlashFourWay()
    m = _manager(fake)
    done = []
    m.firmware_finished.connect(lambda ok, n: done.append((ok, n)))
    m.flash_firmware(str(p), [0])
    assert done == [(1, 1)]
    assert fake.mem[0x1000] == 0xAA and 0x0 not in fake.mem and 0xF800 not in fake.mem
    assert [w[0] for w in fake.settings_writes] == [0x00, 0x01]


def test_flash_refuses_other_board_without_override_and_leaves_flash_untouched(tmp_path):
    p = tmp_path / "fw.hex"
    p.write_text(_firmware_text("OTHER_G071"), encoding="ascii")
    fake = _FakeFlashFourWay()
    m = _manager(fake)
    done = []
    m.firmware_finished.connect(lambda ok, n: done.append((ok, n)))
    m.flash_firmware(str(p), [0])
    assert done == [(0, 1)] and not fake.mem and not fake.settings_writes


def test_failed_verify_keeps_boot_byte_cleared(tmp_path):
    p = tmp_path / "fw.hex"
    p.write_text(_firmware_text(), encoding="ascii")
    fake = _FakeFlashFourWay(corrupt=True)
    m = _manager(fake)
    done = []
    m.firmware_finished.connect(lambda ok, n: done.append((ok, n)))
    m.flash_firmware(str(p), [0])
    assert done == [(0, 1)] and fake.settings_writes[-1][0] == 0x00
