"""Tests for dfu_flash_manager.py - the pure STM32_Programmer_CLI.exe
subprocess wrapper. The real CLI isn't installed on any machine this was
developed on, so every subprocess call is mocked here; `list_devices`'s
line-parsing and `flash_and_verify`'s success-detection logic are the
actual things worth pinning down now, ahead of a real-hardware check (see
dfu_flash_manager.py's own module docstring for that known gap).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from esc_hotline import dfu_flash_manager


def _fake_completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


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
        raise AssertionError("subprocess.run should not be called for a missing firmware file")

    monkeypatch.setattr(subprocess, "run", fail_if_called)
    result = dfu_flash_manager.flash_and_verify(Path("fake_cli.exe"), "usb1", str(tmp_path / "missing.hex"))
    assert result.success is False
    assert "не знайдено" in result.message


def test_flash_and_verify_success(monkeypatch, tmp_path):
    firmware = tmp_path / "fw.hex"
    firmware.write_text(":0\n")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _fake_completed(0, "Download complete.\nVerified successfully.\n")
    )
    result = dfu_flash_manager.flash_and_verify(Path("fake_cli.exe"), "usb1", str(firmware))
    assert result.success is True


def test_flash_and_verify_fails_on_nonzero_exit_even_with_verified_text(monkeypatch, tmp_path):
    # exit code must agree with the text - never trust one signal alone,
    # same "never trust a single signal" stance as esc_manager.py's own
    # readback-verify.
    firmware = tmp_path / "fw.hex"
    firmware.write_text(":0\n")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _fake_completed(1, "Verified successfully.\n", "Error: device lost")
    )
    result = dfu_flash_manager.flash_and_verify(Path("fake_cli.exe"), "usb1", str(firmware))
    assert result.success is False
    assert "Error" in result.message


def test_flash_and_verify_fails_without_verified_text(monkeypatch, tmp_path):
    firmware = tmp_path / "fw.hex"
    firmware.write_text(":0\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(0, "Download complete.\n"))
    result = dfu_flash_manager.flash_and_verify(Path("fake_cli.exe"), "usb1", str(firmware))
    assert result.success is False


def test_flash_and_verify_timeout(monkeypatch, tmp_path):
    firmware = tmp_path / "fw.hex"
    firmware.write_text(":0\n")

    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="fake", timeout=1)

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    result = dfu_flash_manager.flash_and_verify(Path("fake_cli.exe"), "usb1", str(firmware), timeout_s=1.0)
    assert result.success is False


# ---- reset_to_app ------------------------------------------------


def test_reset_to_app_success(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(0))
    assert dfu_flash_manager.reset_to_app(Path("fake_cli.exe"), "usb1") is True


def test_reset_to_app_nonzero_exit_is_failure(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(1))
    assert dfu_flash_manager.reset_to_app(Path("fake_cli.exe"), "usb1") is False


def test_reset_to_app_handles_oserror(monkeypatch):
    def raise_oserror(*a, **k):
        raise OSError("gone")

    monkeypatch.setattr(subprocess, "run", raise_oserror)
    assert dfu_flash_manager.reset_to_app(Path("fake_cli.exe"), "usb1") is False
