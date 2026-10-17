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


def test_list_devices_keeps_only_dfu_vid_pid(monkeypatch):
    stdout = (
        "-------- Connection list --------\n"
        "USB1 : ST-LINK (0483:3748)\n"
        "USB2 : STM32 BOOTLOADER (0483:DF11)\n"
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(0, stdout))
    devices = dfu_flash_manager.list_devices(Path("fake_cli.exe"))
    assert len(devices) == 1
    assert devices[0].port_id == "usb2"


def test_list_devices_empty_when_no_dfu_line(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(0, "USB1 : ST-LINK (0483:3748)\n"))
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
