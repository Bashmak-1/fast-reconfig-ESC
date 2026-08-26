"""Unit tests for hotspot_station.py's pure phase-transition decisions -
the "watch one operator-chosen ESC slot" logic behind the "Хотспот (лінія)"
tab's HotspotStationController. Not used by hotspot_widget.py (the original
"Хотспот" tab keeps its own, deliberately different full-sweep design).
"""
from __future__ import annotations

from fast_reconfig_esc.hotspot_station import phase_after_discovery, phase_after_flash


def test_not_found_goes_to_watching_regardless_of_last_state():
    assert phase_after_discovery(0, None) == ("watching", False)
    assert phase_after_discovery(0, "ok") == ("watching", False)
    assert phase_after_discovery(0, "diff") == ("watching", False)


def test_found_and_already_matching_needs_no_flash():
    assert phase_after_discovery(1, "ok") == ("esc_ready", False)


def test_found_and_differing_needs_a_flash():
    assert phase_after_discovery(1, "diff") == ("flashing", True)


def test_found_and_blank_needs_a_flash():
    assert phase_after_discovery(1, "blank") == ("flashing", True)


def test_found_with_no_prior_classification_needs_a_flash():
    # Defensive: if a status update was ever missed, don't silently skip
    # flashing an ESC that might actually need it.
    assert phase_after_discovery(1, None) == ("flashing", True)


def test_flash_success_goes_to_ready():
    assert phase_after_flash(success=1, attempted=1) == "esc_ready"


def test_flash_failure_goes_to_error():
    assert phase_after_flash(success=0, attempted=1) == "esc_error"


def test_flash_zero_attempted_is_treated_as_error_not_ready():
    # Degenerate case that should never happen in practice (discover_one
    # already confirmed a write was needed before flash_bytes was called),
    # but 0/0 must not be misread as a successful flash.
    assert phase_after_flash(success=0, attempted=0) == "esc_error"
