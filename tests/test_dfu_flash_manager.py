"""Tests for dfu_flash_manager.py - the pure STM32_Programmer_CLI.exe
subprocess wrapper. The real CLI isn't installed on any machine this was
developed on, so every subprocess call is mocked here; `list_devices`'s
line-parsing and `flash_and_verify`'s success-detection logic are the
actual things worth pinning down now, ahead of a real-hardware check (see
dfu_flash_manager.py's own module docstring for that known gap).
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from esc_hotline import dfu_flash_manager


def _fake_completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _FakePopen:
    """Stands in for subprocess.Popen in flash_and_verify()'s streaming
    path (_run_streaming/_stream_reader) - read(1) hands out `output` one
    character at a time, exactly like a real pipe, so the \\r/\\n-splitting
    logic in _stream_reader gets exercised for real rather than bypassed."""

    def __init__(self, output: str = "", returncode: int = 0, hang_s: float = 0.0) -> None:
        self._output = output
        self._pos = 0
        self._hang_s = hang_s
        self.returncode = returncode
        self.stdout = self

    def read(self, _n: int) -> str:
        if self._hang_s:
            time.sleep(self._hang_s)
            return ""
        if self._pos >= len(self._output):
            return ""
        char = self._output[self._pos]
        self._pos += 1
        return char

    def wait(self, timeout=None) -> int:
        return self.returncode

    def kill(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _no_real_delay(monkeypatch):
    # flash_and_verify() sleeps DFU_SETTLE_DELAY_S before writing (real-
    # hardware-confirmed settle delay, see dfu_flash_manager.py) - no test
    # here should actually wait for it.
    monkeypatch.setattr(dfu_flash_manager.time, "sleep", lambda _s: None)


# ---- find_cli_path ------------------------------------------------


def test_find_cli_path_with_valid_override(tmp_path):
    exe = tmp_path / "STM32_Programmer_CLI.exe"
    exe.write_text("")
    assert dfu_flash_manager.find_cli_path(str(exe)) == exe


def test_find_cli_path_with_invalid_override(tmp_path):
    missing = tmp_path / "does_not_exist.exe"
    assert dfu_flash_manager.find_cli_path(str(missing)) is None


def test_find_cli_path_falls_back_to_default_candidates(monkeypatch, tmp_path):
    exe = tmp_path / "STM32_Programmer_CLI.exe"
    exe.write_text("")
    monkeypatch.setattr(dfu_flash_manager, "DEFAULT_CLI_CANDIDATES", (str(exe),))
    assert dfu_flash_manager.find_cli_path(None) == exe


def test_find_cli_path_none_when_nothing_found(monkeypatch, tmp_path):
    monkeypatch.setattr(dfu_flash_manager, "DEFAULT_CLI_CANDIDATES", (str(tmp_path / "nope.exe"),))
    assert dfu_flash_manager.find_cli_path(None) is None


# ---- list_devices ------------------------------------------------


def test_list_devices_calls_cli_with_l_usb_not_bare_l(monkeypatch):
    # The actual bug reported from real hardware: bare `-l` lists RS232
    # serial ports (per UM2237), not USB DFU devices - `-l usb` is the
    # distinct, correct listing mode. Pin the exact args down so this
    # never regresses back to the broken flag.
    seen_args = []

    def fake_run(cmd, **kwargs):
        seen_args.append(cmd)
        return _fake_completed(0, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    dfu_flash_manager.list_devices(Path("fake_cli.exe"))
    assert seen_args[0][1:] == ["-l", "usb"]


def test_list_devices_finds_device_from_realistic_multiline_block(monkeypatch):
    # Shaped after a real captured STM32CubeProgrammer connection log (see
    # dfu_flash_manager.py's module docstring) - no "0483:df11"-style
    # VID:PID text anywhere, just an index token plus informational
    # fields. The earlier VID:PID-substring filter would have found
    # nothing here; this is exactly the regression it's guarding against.
    stdout = (
        "USB1\n"
        "  Manuf. ID   : STMicroelectronics\n"
        "  Product ID  : DFU in FS Mode\n"
        "  SN          : 000000000000\n"
        "  DFU protocol: 1.1\n"
        "  Board       : --\n"
        "  Device ID   : 0x450\n"
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(0, stdout))
    devices = dfu_flash_manager.list_devices(Path("fake_cli.exe"))
    assert len(devices) == 1
    assert devices[0].port_id == "usb1"


def test_list_devices_deduplicates_repeated_index_in_a_block(monkeypatch):
    # The same "usb1" token legitimately showing up on more than one line
    # of a single device's multi-line block must still count as ONE device.
    stdout = "USB1 found\nconnected as usb1\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(0, stdout))
    devices = dfu_flash_manager.list_devices(Path("fake_cli.exe"))
    assert len(devices) == 1


def test_list_devices_two_devices(monkeypatch):
    stdout = "USB1\n  SN: 111\nUSB2\n  SN: 222\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(0, stdout))
    devices = dfu_flash_manager.list_devices(Path("fake_cli.exe"))
    assert {d.port_id for d in devices} == {"usb1", "usb2"}


def test_list_devices_empty_when_no_usb_index_token(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(0, "No STM32 device in DFU mode\n"))
    assert dfu_flash_manager.list_devices(Path("fake_cli.exe")) == []


def test_list_devices_handles_missing_tool(monkeypatch):
    def raise_oserror(*a, **k):
        raise OSError("not found")

    monkeypatch.setattr(subprocess, "run", raise_oserror)
    assert dfu_flash_manager.list_devices(Path("fake_cli.exe")) == []


def test_list_devices_handles_timeout(monkeypatch):
    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="fake", timeout=10)

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    assert dfu_flash_manager.list_devices(Path("fake_cli.exe")) == []


# ---- flash_and_verify ------------------------------------------------


def test_flash_and_verify_missing_firmware_file_never_calls_subprocess(monkeypatch, tmp_path):
    def fail_if_called(*a, **k):
        raise AssertionError("subprocess.Popen should not be called for a missing firmware file")

    monkeypatch.setattr(subprocess, "Popen", fail_if_called)
    result = dfu_flash_manager.flash_and_verify(Path("fake_cli.exe"), "usb1", str(tmp_path / "missing.hex"))
    assert result.success is False
    assert "не знайдено" in result.message


def test_flash_and_verify_success(monkeypatch, tmp_path):
    firmware = tmp_path / "fw.hex"
    firmware.write_text(":0\n")
    monkeypatch.setattr(
        subprocess, "Popen", lambda cmd, **k: _FakePopen("Download complete.\nVerified successfully.\n", 0)
    )
    result = dfu_flash_manager.flash_and_verify(Path("fake_cli.exe"), "usb1", str(firmware))
    assert result.success is True


def test_flash_and_verify_reports_live_progress(monkeypatch, tmp_path):
    # Confirmed real CLI output shape (captured from real hardware): no
    # "\r" redraw at all - one unbroken line per bar, each "N%" step glued
    # to the next by a fill byte, only a real "\n" once the bar hits 100%.
    # "\x8b" below stands in for that fill byte (an OEM-codepage block
    # character in the real log) - irrelevant to parsing, just filler
    # between steps. "Verifying" marks the write->verify stage switch.
    firmware = tmp_path / "fw.hex"
    firmware.write_text(":0\n")
    output = (
        "Download in Progress:\n"
        "\x8b  0%\x8b 50%\x8b100%\n"
        "Verifying...\n"
        "Read progress:\n"
        "\x8b  0%\x8b 50%\x8b100%\n"
        "Verified successfully.\n"
    )
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: _FakePopen(output, 0))
    seen: list[tuple[str, int]] = []
    result = dfu_flash_manager.flash_and_verify(
        Path("fake_cli.exe"), "usb1", str(firmware), progress_callback=lambda stage, pct: seen.append((stage, pct))
    )
    assert result.success is True
    # Every step must arrive as its own event - not just the first one in
    # the unbroken line (the bug this test guards against: the old
    # \r/\n-only splitting would have reported only ("write", 0)).
    assert seen == [("write", 0), ("write", 50), ("write", 100), ("verify", 0), ("verify", 50), ("verify", 100)]
    assert ("verify", 50) in seen
    assert ("verify", 100) in seen
    # the switch to "verify" must not happen before the "Verifying" marker
    assert seen.index(("write", 100)) < seen.index(("verify", 50))


def test_flash_and_verify_bin_file_gets_explicit_address(monkeypatch, tmp_path):
    # Confirmed on real hardware: a raw .bin needs FLASH_BASE_ADDRESS as an
    # explicit extra argument, or the CLI refuses with "You must enter the
    # write address..." - .hex (tested above) needs no such argument.
    firmware = tmp_path / "fw.bin"
    firmware.write_bytes(b"\x00" * 16)
    seen_args = []

    def fake_popen(cmd, **kwargs):
        seen_args.append(cmd)
        return _FakePopen("Verified successfully.\n", 0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    result = dfu_flash_manager.flash_and_verify(Path("fake_cli.exe"), "usb1", str(firmware))
    assert result.success is True
    assert dfu_flash_manager.FLASH_BASE_ADDRESS in seen_args[0]
    # address must come right after the file path, before -v
    w_index = seen_args[0].index(str(firmware))
    assert seen_args[0][w_index + 1] == dfu_flash_manager.FLASH_BASE_ADDRESS


def test_flash_and_verify_hex_file_gets_no_address(monkeypatch, tmp_path):
    firmware = tmp_path / "fw.hex"
    firmware.write_text(":0\n")
    seen_args = []

    def fake_popen(cmd, **kwargs):
        seen_args.append(cmd)
        return _FakePopen("Verified successfully.\n", 0)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    dfu_flash_manager.flash_and_verify(Path("fake_cli.exe"), "usb1", str(firmware))
    assert dfu_flash_manager.FLASH_BASE_ADDRESS not in seen_args[0]


def test_flash_and_verify_fails_on_nonzero_exit_even_with_verified_text(monkeypatch, tmp_path):
    # exit code must agree with the text - never trust one signal alone,
    # same "never trust a single signal" stance as esc_manager.py's own
    # readback-verify.
    firmware = tmp_path / "fw.hex"
    firmware.write_text(":0\n")
    monkeypatch.setattr(
        subprocess, "Popen", lambda cmd, **k: _FakePopen("Verified successfully.\nError: device lost\n", 1)
    )
    result = dfu_flash_manager.flash_and_verify(Path("fake_cli.exe"), "usb1", str(firmware))
    assert result.success is False
    assert "Error" in result.message


def test_flash_and_verify_fails_without_verified_text(monkeypatch, tmp_path):
    firmware = tmp_path / "fw.hex"
    firmware.write_text(":0\n")
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: _FakePopen("Download complete.\n", 0))
    result = dfu_flash_manager.flash_and_verify(Path("fake_cli.exe"), "usb1", str(firmware))
    assert result.success is False


def test_flash_and_verify_timeout(monkeypatch, tmp_path):
    firmware = tmp_path / "fw.hex"
    firmware.write_text(":0\n")
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: _FakePopen(hang_s=2.0))
    result = dfu_flash_manager.flash_and_verify(Path("fake_cli.exe"), "usb1", str(firmware), timeout_s=0.2)
    assert result.success is False


# ---- reset_to_app ------------------------------------------------


def test_reset_to_app_success(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(0))
    assert dfu_flash_manager.reset_to_app(Path("fake_cli.exe"), "usb1") is True


def test_reset_to_app_uses_go_command_not_rst(monkeypatch):
    # Confirmed on real hardware: "-rst" fails over USB DFU ("Reset command
    # is only available with JTAG/SWD debug interface") - the correct
    # command is "-g <address>" (same as STM32CubeProgrammer GUI's own
    # "Run after programming").
    seen_args = []

    def fake_run(cmd, **kwargs):
        seen_args.append(cmd)
        return _fake_completed(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    dfu_flash_manager.reset_to_app(Path("fake_cli.exe"), "usb1")
    assert "-rst" not in seen_args[0]
    assert "-g" in seen_args[0]
    assert dfu_flash_manager.FLASH_BASE_ADDRESS in seen_args[0]


def test_reset_to_app_nonzero_exit_is_failure(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(1))
    assert dfu_flash_manager.reset_to_app(Path("fake_cli.exe"), "usb1") is False


def test_reset_to_app_handles_oserror(monkeypatch):
    def raise_oserror(*a, **k):
        raise OSError("gone")

    monkeypatch.setattr(subprocess, "run", raise_oserror)
    assert dfu_flash_manager.reset_to_app(Path("fake_cli.exe"), "usb1") is False
