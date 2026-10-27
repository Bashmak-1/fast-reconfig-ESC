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


def test_freshly_flashed_fc_goes_straight_to_the_esc_stage_then_one_full_reboot(monkeypatch):
    """Flow: DFU flash (no protocol write / reboot) -> ESCs -> ONE protocol write + full FC reboot at the end."""
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
        c._discover_many_requested = MagicMock()
        monkeypatch.setattr(fs, "list_fc_ports", lambda: [])

        c._set_phase("connecting")
        c._on_connection_state_changed(True, 2)  # FC answered
        assert c.phase == "watching" and not c._write_protocol_requested.emit.called  # straight to the ESCs

        c._begin_protocol_write()  # every ESC done
        c._on_connection_state_changed(False, 0)  # FourWay confirmed closed -> the write starts
        c._write_protocol_requested.emit.assert_called_once_with("COM9", fs.DEFAULT_BAUD, c._protocol_index, True, True)
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


def _run_protocol_worker(monkeypatch, current_index: int, wanted_index: int, allow_write: bool):
    """Drive _ProtocolWorker against a fake FC; returns (done_args, list of MSP commands sent)."""
    from PyQt6.QtWidgets import QApplication

    from esc_hotline import fc_flash_station as fs
    from esc_hotline.msp import MspCommand

    app = QApplication.instance() or QApplication([])  # noqa: F841
    sent: list = []

    class FakeTransport:
        def __init__(self, port, baud): pass
        def close(self): pass

    class Resp:
        def __init__(self, payload): self.payload = payload

    class FakeMsp:
        def __init__(self, transport): pass

        def send(self, cmd, payload=b"", **kw):
            sent.append(cmd)
            if cmd == MspCommand.MSP_ADVANCED_CONFIG:
                return Resp(bytes([0, 0, 0, current_index]) + (16000).to_bytes(2, "little") + (50).to_bytes(2, "little") + b"\0")
            if cmd == MspCommand.MSP_STATUS_EX:
                return Resp(bytes(16))  # disarmed
            return Resp(b"")

    monkeypatch.setattr(fs, "SerialTransport", FakeTransport)
    monkeypatch.setattr(fs, "MspClient", FakeMsp)
    worker = fs._ProtocolWorker()
    got: list = []
    worker.done.connect(lambda *a: got.append(a))
    worker.write_protocol_and_reboot("COM1", 115200, wanted_index, allow_write)
    return got[0], sent


def test_protocol_already_correct_is_skipped_without_a_reboot(monkeypatch):
    from esc_hotline.msp import MspCommand

    done, sent = _run_protocol_worker(monkeypatch, current_index=5, wanted_index=5, allow_write=True)
    assert done[0] is True and done[2] is False and "вже встановлено" in done[1]
    assert MspCommand.MSP_SET_ADVANCED_CONFIG not in sent and MspCommand.MSP_REBOOT not in sent


def test_wrong_protocol_is_only_reported_when_auto_write_is_off(monkeypatch):
    from esc_hotline.msp import MspCommand

    done, sent = _run_protocol_worker(monkeypatch, current_index=0, wanted_index=5, allow_write=False)
    assert done[0] is True and done[2] is False and "УВАГА" in done[1] and "STANDARD" in done[1]
    assert MspCommand.MSP_SET_ADVANCED_CONFIG not in sent and MspCommand.MSP_REBOOT not in sent


def test_wrong_protocol_is_written_and_the_fc_rebooted(monkeypatch):
    from esc_hotline.msp import MspCommand

    done, sent = _run_protocol_worker(monkeypatch, current_index=0, wanted_index=5, allow_write=True)
    assert done[0] is True and done[2] is True and "неправильний" in done[1]
    assert sent.index(MspCommand.MSP_SET_ADVANCED_CONFIG) < sent.index(MspCommand.MSP_EEPROM_WRITE) < sent.index(MspCommand.MSP_REBOOT)


def test_no_reboot_means_no_waiting_for_the_fc_to_come_back(monkeypatch):
    from unittest.mock import MagicMock

    from PyQt6.QtWidgets import QApplication

    from esc_hotline import fc_flash_station as fs

    app = QApplication.instance() or QApplication([])  # noqa: F841
    c = fs.FcFlashStationController()
    try:
        c._armed, c._port_device, c._motor_test_enabled = True, "COM9", True
        c._motor_connect_requested = MagicMock()
        c._set_phase("writing_protocol")
        c._on_protocol_write_done(True, "ok", False)  # protocol was already right: FC not rebooted
        assert c.phase == "connecting_motor_test"
        c._motor_connect_requested.emit.assert_called_once_with("COM9", fs.DEFAULT_BAUD)

        c._set_phase("writing_protocol")
        c._protocol_stage_early = True
        c._on_protocol_write_done(True, "ok", False)
        assert c.phase == "connecting"  # straight back to the ESC stage
    finally:
        c._armed = False
        c.shutdown()


def test_motor_test_is_cancelled_with_a_clear_reason_on_the_wrong_protocol():
    from PyQt6.QtWidgets import QApplication

    from esc_hotline import fc_flash_station as fs

    app = QApplication.instance() or QApplication([])  # noqa: F841
    c = fs.FcFlashStationController()
    details: list[str] = []
    c.dfu_detail_changed.connect(details.append)
    try:
        c._armed, c._protocol_index, c._motor_detected_protocol = True, 5, 0  # FC reads STANDARD, DSHOT300 wanted
        c._set_phase("checking_motor_armed")
        c._on_motor_armed_check_result(True, "")
        assert c.phase == "motor_test_error" and "STANDARD" in details[-1] and "DSHOT300" in details[-1]
    finally:
        c._armed = False
        c.shutdown()


def test_motor_test_connect_waits_for_the_rebooting_fc_and_retries(monkeypatch):
    """Real log: the station reconnected 150 ms after MSP_REBOOT, hit the still-dying FC, and went straight
    to 'motor test error' (and the manual retry failed the same way because of a stale disconnect reply)."""
    import time
    from unittest.mock import MagicMock

    from PyQt6.QtWidgets import QApplication

    from esc_hotline import fc_flash_station as fs
    from esc_hotline.serial_transport import PortInfo

    app = QApplication.instance() or QApplication([])  # noqa: F841
    c = fs.FcFlashStationController()
    try:
        c._armed, c._port_device, c._motor_test_enabled = True, "COM9", True
        c._motor_connect_requested = MagicMock()
        monkeypatch.setattr(fs, "list_fc_ports", lambda: [PortInfo("COM9", "FC", "", True)])
        monkeypatch.setattr(fs.QTimer, "singleShot", staticmethod(lambda ms, fn: None))  # no real waiting

        c._set_phase("writing_protocol")
        c._on_protocol_write_done(True, "ok", True)  # FC rebooted
        assert c.phase == "waiting_fc_for_motor_test"
        c._on_poll_tick()  # same port is still listed, but the FC has only just been told to reboot
        assert c.phase == "waiting_fc_for_motor_test" and not c._motor_connect_requested.emit.called

        c._after_protocol_since = time.monotonic() - 10
        c._on_poll_tick()
        assert c.phase == "waiting_motor_init" and not c._motor_connect_requested.emit.called  # motors left alone while they initialise
        c._on_poll_tick()
        assert c.phase == "waiting_motor_init"
        c._motor_init_since = time.monotonic() - c._motor_init_wait_s - 1
        c._on_poll_tick()
        assert c.phase == "connecting_motor_test" and c._motor_connect_requested.emit.call_count == 1

        for attempt in range(1, fs.MOTOR_CONNECT_ATTEMPTS):  # failed connects are retried...
            c._on_motor_connection_state_changed(False)
            assert c.phase == "connecting_motor_test"
            c._retry_motor_connect()
            assert c._motor_connect_attempts == attempt + 1
        c._on_motor_connection_state_changed(False)  # ...until the budget is used up
        assert c.phase == "motor_test_error"

        c.retry()  # the manual retry must not be undone by a stale "disconnected" reply
        assert c.phase == "connecting_motor_test" and c._motor_connect_attempts == 1
    finally:
        c._armed = False
        c.shutdown()


def test_watchdog_moves_stuck_phase_to_error():
    from PyQt6.QtWidgets import QApplication

    from esc_hotline import fc_flash_station as fs

    app = QApplication.instance() or QApplication([])  # noqa: F841
    c = fs.FcFlashStationController()
    try:
        c._armed = True
        c._set_phase("resetting_fc")
        c._phase_since -= 1000
        c._on_poll_tick()
        assert c._phase == "fc_flash_error"
    finally:
        c.shutdown()


def test_protocol_reboot_runs_even_with_esc_flash_and_motor_test_both_off(monkeypatch):
    from unittest.mock import MagicMock

    from PyQt6.QtWidgets import QApplication

    from esc_hotline import fc_flash_station as fs

    app = QApplication.instance() or QApplication([])  # noqa: F841
    c = fs.FcFlashStationController()
    try:
        c._armed, c._port_device = True, "COM9"
        c._flash_esc_enabled = c._motor_test_enabled = False
        c._begin_protocol_write = MagicMock()
        c._set_phase("connecting")
        c._on_connection_state_changed(True, 0)
        c._begin_protocol_write.assert_called_once()  # no shortcut to all_done: the FC is still rebooted with the protocol

        # after the reboot: wait for the FC to come back, then finish (no motor test, no motor-init wait)
        monkeypatch.setattr(fs, "list_fc_ports", lambda: [fs.PortInfo("COM9", "FC", 0x0483, True)])
        c._set_phase("writing_protocol")
        c._on_protocol_write_done(True, "ok", True)
        assert c.phase == "waiting_fc_for_motor_test"
        c._after_protocol_since -= 10
        c._on_poll_tick()
        assert c.phase == "waiting_motor_init"  # the motor-init wait stays, also without a motor test
        c._motor_init_since -= 999
        c._on_poll_tick()
        assert c.phase == "all_done"
    finally:
        c._armed = False
        c.shutdown()


def test_station_motor_test_uses_the_exact_fractional_durations_and_bounds(monkeypatch):
    """Both tabs share one set of settings; the station used to read the seconds as integers (4.5 -> default 4)
    and the bounds rounded to whole percent, so its test differed from the chart the operator saw."""
    from unittest.mock import MagicMock

    from PyQt6.QtWidgets import QApplication

    from esc_hotline import fc_flash_station as fs

    app = QApplication.instance() or QApplication([])  # noqa: F841
    stored = {
        fs._MOTOR_UP_DURATION_KEY: 4.55, fs._MOTOR_HOLD_DURATION_KEY: 0.25, fs._MOTOR_DOWN_DURATION_KEY: 3.35,
        fs._MOTOR_MIN_BOUND_KEY: 12.35, fs._MOTOR_MAX_BOUND_KEY: 87.65,
    }
    monkeypatch.setattr(fs, "load_float", lambda key, default=0.0: stored[key])
    c = fs.FcFlashStationController()
    try:
        c._armed = True
        c._set_phase("running_motor_test")
        c._motor_start_sweep_requested = MagicMock()
        c._start_motor_sweep()
        c._motor_start_sweep_requested.emit.assert_called_once_with(4.55, 0.25, 3.35)
    finally:
        c._armed = False
        c.shutdown()
