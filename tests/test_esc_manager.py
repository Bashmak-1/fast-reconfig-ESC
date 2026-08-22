"""EscManager orchestration tests using a faked FourWayClient - no real hardware.

Verifies the core reliability requirement: one ESC failing to respond must
never abort discovery/flashing of the rest, and the manager never raises out
to the caller for a per-ESC problem.
"""
from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

from fast_reconfig_esc import esc_manager as esc_manager_module
from fast_reconfig_esc.fourway import DeviceInfo, FourWayAckError, FourWayAck
from fast_reconfig_esc.i18n import tr
from fast_reconfig_esc.mcu_profiles import LAYOUT_SIZE


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class FakeFourWay:
    """Stands in for FourWayClient: ESCs at BAD_INDICES raise, others succeed."""

    STM32_SIG = 0x1F06

    def __init__(self, bad_indices: set[int], settings_by_index: dict | None = None):
        self.bad_indices = bad_indices
        self.settings_by_index = settings_by_index or {}
        self.written: dict[int, bytes] = {}

    def init_flash(self, index: int, retries: int = 10) -> DeviceInfo:
        if index in self.bad_indices:
            raise FourWayAckError(int(FourWayAck.ACK_D_GENERAL_ERROR))
        return DeviceInfo(signature=self.STM32_SIG, input_pin=4, interface_mode=0)

    def read(self, address: int, length: int, retries: int = 10, timeout_s: float = 0.2) -> bytes:
        # returns whatever was last "written", or a valid (non-blank) default
        idx = self._last_index
        if idx in self.written:
            return self.written[idx]
        return self.settings_by_index.get(idx, bytes([0x01]) * length)

    def write(self, address: int, data: bytes, retries: int = 10, timeout_s: float = 0.3):
        self.written[self._last_index] = data

    def exit_interface(self) -> None:
        pass

    # helper the test uses to track "current" ESC target across init_flash/read/write
    _last_index = -1


def _make_manager_with_fake(monkeypatch, bad_indices, expected_count, settings_by_index=None):
    mgr = esc_manager_module.EscManager()
    fake = FakeFourWay(bad_indices, settings_by_index)

    real_init_flash = fake.init_flash

    def tracking_init_flash(index, retries=10):
        fake._last_index = index
        return real_init_flash(index, retries)

    monkeypatch.setattr(fake, "init_flash", tracking_init_flash)

    mgr._fourway = fake  # bypass real serial transport entirely
    mgr._expected_count = expected_count
    return mgr, fake


def test_discover_all_continues_past_missing_esc(monkeypatch):
    events = []
    mgr, _fake = _make_manager_with_fake(monkeypatch, bad_indices={2}, expected_count=5)
    mgr.esc_status_changed.connect(lambda i, s, d: events.append((i, s)))

    finished = []
    mgr.discovery_finished.connect(lambda found, expected: finished.append((found, expected)))

    mgr.discover_all()  # must not raise

    states_by_index = {i: s for i, s in events if s != "probing"}
    assert states_by_index[0] == "ok"
    assert states_by_index[1] == "ok"
    assert states_by_index[2] == "error"  # the missing one - isolated, not fatal
    assert states_by_index[3] == "ok"
    assert states_by_index[4] == "ok"
    assert finished == [(4, 5)]


def test_flash_all_continues_past_failing_esc(monkeypatch, tmp_path):
    config_path = tmp_path / "cfg.bin"
    config_path.write_bytes(bytes([0x42]) * LAYOUT_SIZE)

    mgr, fake = _make_manager_with_fake(monkeypatch, bad_indices={1}, expected_count=3)

    events = []
    mgr.esc_status_changed.connect(lambda i, s, d: events.append((i, s)))
    finished = []
    mgr.flash_finished.connect(lambda success, attempted: finished.append((success, attempted)))

    mgr.flash_all(str(config_path), target_indices=[0, 1, 2])  # must not raise

    states_by_index = {i: s for i, s in events if s != "probing"}
    assert states_by_index[0] == "ok"
    assert states_by_index[1] == "error"
    assert states_by_index[2] == "ok"
    assert finished == [(2, 3)]
    assert fake.written[0] == bytes([0x42]) * LAYOUT_SIZE
    assert fake.written[2] == bytes([0x42]) * LAYOUT_SIZE
    assert 1 not in fake.written


def test_flash_all_detects_readback_mismatch(monkeypatch, tmp_path):
    config_path = tmp_path / "cfg.bin"
    config_path.write_bytes(bytes([0x42]) * LAYOUT_SIZE)

    mgr, fake = _make_manager_with_fake(monkeypatch, bad_indices=set(), expected_count=1)

    # Force read() to return something different from what was written.
    monkeypatch.setattr(fake, "read", lambda *a, **k: bytes([0x99]) * LAYOUT_SIZE)

    events = []
    mgr.esc_status_changed.connect(lambda i, s, d: events.append((i, s, d)))

    mgr.flash_all(str(config_path), target_indices=[0])

    error_events = [e for e in events if e[1] == "error"]
    assert len(error_events) == 1
    assert "readback mismatch" in error_events[0][2]


def test_discover_all_flags_config_match_vs_diff(monkeypatch, tmp_path):
    matching = bytes([0x11]) * LAYOUT_SIZE
    different = bytes([0x22]) * LAYOUT_SIZE
    config_path = tmp_path / "cfg.bin"
    config_path.write_bytes(matching)

    mgr, _fake = _make_manager_with_fake(
        monkeypatch,
        bad_indices=set(),
        expected_count=2,
        settings_by_index={0: matching, 1: different},
    )
    mgr.set_config_path(str(config_path))

    events = []
    mgr.esc_status_changed.connect(lambda i, s, d: events.append((i, s, d)))
    mgr.discover_all()

    final = {i: (s, d) for i, s, d in events if s != "probing"}
    assert final[0] == ("ok", tr("config_matches", mcu="STM32F051"))
    assert final[1] == ("diff", tr("config_differs", mcu="STM32F051"))


def test_set_config_path_reevaluates_without_a_new_scan(monkeypatch, tmp_path):
    matching = bytes([0x11]) * LAYOUT_SIZE
    other = bytes([0x33]) * LAYOUT_SIZE

    mgr, _fake = _make_manager_with_fake(
        monkeypatch, bad_indices=set(), expected_count=1, settings_by_index={0: matching}
    )
    mgr.discover_all()  # no config loaded yet -> plain "ok"

    events = []
    mgr.esc_status_changed.connect(lambda i, s, d: events.append((i, s, d)))

    config_path = tmp_path / "cfg.bin"
    config_path.write_bytes(matching)
    mgr.set_config_path(str(config_path))  # should re-emit immediately, no discover_all() call

    assert events == [(0, "ok", tr("config_matches", mcu="STM32F051"))]

    config_path.write_bytes(other)
    events.clear()
    mgr.set_config_path(str(config_path))
    assert events == [(0, "diff", tr("config_differs", mcu="STM32F051"))]


def test_power_cycle_hint_after_repeated_empty_scans(monkeypatch):
    mgr, _fake = _make_manager_with_fake(monkeypatch, bad_indices={0, 1}, expected_count=2)

    warnings = []
    mgr.log_message.connect(lambda level, text: warnings.append(text) if level == "warning" else None)

    mgr.discover_all()
    assert warnings == []  # first empty scan: no hint yet

    mgr.discover_all()
    assert warnings == [tr("power_cycle_hint")]  # second consecutive empty scan: hint fires

    warnings.clear()
    mgr.discover_all()
    assert warnings == [tr("power_cycle_hint")]  # keeps firing while still empty
