"""Unit tests for fc_flash_station.py's pure decisions:
- phase_after_discovery_many: watching MORE THAN ONE ESC slot at once (e.g.
  both halves of a 2-in-1 ESC board) - the multi-slot sibling of
  hotspot_station.py's single-slot phase_after_discovery.
- choose_reenumerated_port: picking which newly-appeared COM port is the FC
  after a DFU flash+reset, when the port NUMBER isn't stable across that
  cycle (confirmed in practice via the AM32 website doing the same DFU
  step - Windows can hand out a different number each time).
"""
from __future__ import annotations

from esc_hotline.fc_flash_station import choose_reenumerated_port, phase_after_discovery_many
from esc_hotline.serial_transport import PortInfo


def test_nothing_found_goes_to_watching():
    assert phase_after_discovery_many({}, [0, 1]) == ("watching", [])


def test_all_found_and_matching_is_ready():
    assert phase_after_discovery_many({0: "ok", 1: "ok"}, [0, 1]) == ("esc_ready", [])


def test_one_differing_flashes_only_that_one():
    assert phase_after_discovery_many({0: "ok", 1: "diff"}, [0, 1]) == ("flashing", [1])


def test_both_differing_flashes_both():
    assert phase_after_discovery_many({0: "diff", 1: "blank"}, [0, 1]) == ("flashing", [0, 1])


def test_flash_target_order_follows_watched_indices_not_dict_order():
    assert phase_after_discovery_many({1: "diff", 0: "blank"}, [0, 1]) == ("flashing", [0, 1])


def test_only_some_watched_indices_responding_keeps_watching_if_all_ok_so_far():
    # ESC 2 hasn't shown up on the board yet this tick - don't declare ready
    # until every watched slot has actually been seen.
    assert phase_after_discovery_many({0: "ok"}, [0, 1]) == ("watching", [])


def test_a_single_watched_index_behaves_like_the_single_slot_case():
    assert phase_after_discovery_many({0: "ok"}, [0]) == ("esc_ready", [])
    assert phase_after_discovery_many({0: "diff"}, [0]) == ("flashing", [0])
    assert phase_after_discovery_many({}, [0]) == ("watching", [])


def _port(device: str, likely_fc: bool = False) -> PortInfo:
    return PortInfo(device=device, description=device, vid=None, likely_fc=likely_fc)


def test_no_new_ports_means_nothing_to_pick():
    assert choose_reenumerated_port([]) is None


def test_exactly_one_new_port_is_picked_regardless_of_its_number():
    # The whole point: COM7 -> COM400+ after a DFU cycle is a real,
    # observed case - the port NUMBER must never matter, only "it's new".
    assert choose_reenumerated_port([_port("COM417")]) == "COM417"


def test_multiple_new_ports_prefers_the_likely_fc_one():
    candidates = [_port("COM5", likely_fc=False), _port("COM417", likely_fc=True)]
    assert choose_reenumerated_port(candidates) == "COM417"


def test_multiple_new_ports_with_none_marked_likely_falls_back_to_first():
    candidates = [_port("COM5"), _port("COM6")]
    assert choose_reenumerated_port(candidates) == "COM5"


def test_freshly_flashed_fc_gets_its_protocol_before_the_esc_stage(monkeypatch):
    """Real-log regression: a board whose ESC never answered stayed on STANDARD because the protocol
    was only written after every ESC was done. Now it is written first, right after the FC reconnects."""
    import time
    from unittest.mock import MagicMock

    from PyQt6.QtWidgets import QApplication

    from esc_hotline import fc_flash_station as fs

    app = QApplication.instance() or QApplication([])  # noqa: F841
    c = fs.FcFlashStationController()
    try:
        c._armed = True
        c._port_device = "COM9"
        c._flash_esc_enabled = True
        c._esc_indices = [0, 1]
        c._fc_flashed_no_protocol = True  # set by the DFU-reset step
        c._write_protocol_requested = MagicMock()
        c._disconnect_requested = MagicMock()
        monkeypatch.setattr(fs, "list_fc_ports", lambda: [])

        c._set_phase("connecting")
        c._on_connection_state_changed(True, 2)  # FC answered
        assert c.phase == "writing_protocol" and c._protocol_stage_early  # NOT "watching"
        c._disconnect_requested.emit.assert_called_once()

        c._on_connection_state_changed(False, 0)  # FourWay confirmed closed -> the write starts
        c._write_protocol_requested.emit.assert_called_once_with("COM9", fs.DEFAULT_BAUD, c._protocol_index)

        c._on_protocol_write_done(True, "ok")
        assert c.phase == "waiting_fc_after_protocol" and not c._fc_flashed_no_protocol

        c._after_protocol_since = time.monotonic() - 10  # reboot settle time has passed
        monkeypatch.setattr(fs, "list_fc_ports", lambda: [PortInfo("COM9", "FC", "", True)])
        c._on_poll_tick()
        assert c.phase == "connecting"  # back to the normal ESC stage via a reconnect
    finally:
        c._armed = False
        c.shutdown()


def test_banner_and_lines_name_the_right_esc_when_they_appear_out_of_order():
    """ESC 2 shows up and is flashed first, ESC 1 later: the banner must name the ESC being flashed /
    waited for, and ESC 2's line must keep saying it was flashed instead of being wiped or relabelled."""
    from unittest.mock import MagicMock

    from PyQt6.QtWidgets import QApplication

    from esc_hotline import fc_flash_station as fs
    from esc_hotline.i18n import tr

    app = QApplication.instance() or QApplication([])  # noqa: F841
    c = fs.FcFlashStationController()
    lines: dict[int, str] = {}
    c.esc_detail_changed.connect(lambda i, st, d: lines.__setitem__(i, d))
    try:
        c._flash_requested = MagicMock()
        c._begin_protocol_write = MagicMock()
        c._armed, c._esc_indices, c._flash_esc_enabled = True, [0, 1], True
        c._set_phase("watching")

        c._tick_seen_states = {}
        c._on_esc_status_changed(1, "diff", "d", None)
        c._on_discovery_finished(1, 2)
        assert c.phase == "flashing" and c.banner_text() == "Прошивка ESC 2..."
        c._on_esc_status_changed(1, "ok", "x", None)
        c._on_flash_finished(1, 1)
        assert c.banner_text() == "ESC 2 готова - чекаю ESC 1..."

        c._tick_seen_states = {}  # next tick: ESC 2 still fine, ESC 1 not there yet
        c._on_esc_status_changed(1, "ok", "config matches", None)
        c._on_discovery_finished(1, 2)
        assert c.phase == "watching" and c.banner_text() == "ESC 2 готова - чекаю ESC 1..."
        assert lines[1] == tr("flashed_verified") and lines[0] == ""  # ESC 2 not wiped / relabelled

        c._tick_seen_states = {}
        c._on_esc_status_changed(0, "diff", "d", None)
        c._on_esc_status_changed(1, "ok", "config matches", None)
        c._on_discovery_finished(2, 2)
        assert c.banner_text() == "Прошивка ESC 1..."  # not "2, 1"
        c._on_esc_status_changed(0, "ok", "x", None)
        c._on_flash_finished(1, 1)
        assert c.banner_text() == "ESC 1, 2 готові"
    finally:
        c._armed = False
        c.shutdown()
