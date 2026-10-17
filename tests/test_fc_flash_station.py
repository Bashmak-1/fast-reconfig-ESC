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
