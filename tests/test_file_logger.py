"""Confirms EscManager actually writes detailed technical trace lines to the
persistent log file - not just the GUI panel - since that file is the whole
point of the feature (find-and-read-later debugging).
"""
from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

from fast_reconfig_esc.file_logger import LOG_FILE
from test_esc_manager import _make_manager_with_fake, _settings_bytes


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _read_text() -> str:
    return LOG_FILE.read_text(encoding="utf-8", errors="replace") if LOG_FILE.is_file() else ""


def test_discover_all_writes_hex_dump_and_summary_to_log_file(monkeypatch):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    before_text = _read_text()

    settings = _settings_bytes(0x07)
    mgr, _fake = _make_manager_with_fake(
        monkeypatch, bad_indices={1}, expected_count=2, settings_by_index={0: settings}
    )
    mgr.discover_all()

    new_content = _read_text()[len(before_text):]
    assert "Discovery started" in new_content
    assert "ESC #1: found" in new_content
    assert settings.hex(" ") in new_content  # full settings hex dump present
    assert "ESC #2" in new_content  # the missing one is still logged
    assert "Discovery finished: 1/2 found" in new_content


def test_flash_all_writes_before_after_hex_to_log_file(monkeypatch, tmp_path):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    before_text = _read_text()

    config_bytes = bytes([0x55]) * 184
    config_path = tmp_path / "cfg.bin"
    config_path.write_bytes(config_bytes)

    mgr, _fake = _make_manager_with_fake(monkeypatch, bad_indices=set(), expected_count=1)
    mgr.flash_all(str(config_path), target_indices=[0])

    new_content = _read_text()[len(before_text):]
    assert "Flash started" in new_content
    assert config_bytes.hex(" ") in new_content  # the bytes being written, in full
    assert "write verified OK" in new_content
