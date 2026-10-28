"""ID2BIT telemetry: payload parser, MSPv2 response framing, probe on a stock FC, and the per-run CSV."""
from __future__ import annotations

import csv
import struct
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

from esc_hotline import esc_telemetry as et
from esc_hotline.motor_ramp import MSP_MIN
from esc_hotline.motor_test_manager import PHASE_UP, MotorTestManager
from esc_hotline.msp import MspError, MspResponse, _read_msp_response, crc8_dvb_s2


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication([])


def _payload(rpms=(1000, 0), frames=(10, 11), age=0) -> bytes:
    out = struct.pack(et.HEADER_FMT, 1, len(rpms), 100, 5, 6, 0)
    for i, rpm in enumerate(rpms):
        out += struct.pack(et.ESC_FMT, i, 1, age, 27, 2324, 12, 6, rpm // 7, rpm, frames[i], 30690, 13)
    return out


def test_parse_matches_guide_layout():
    assert (et.HEADER_SIZE, et.ESC_SIZE) == (18, 26)
    header, recs = et.parse_telemetry(_payload())
    assert (header.motor_count, header.valid, header.crc_errors) == (2, 100, 5)
    assert recs[0].voltage_v == 23.24 and recs[0].current_a == 0.12 and recs[0].rpm == 1000
    assert recs[1].id == 1 and not recs[0].stale


def test_parse_rejects_short_or_unknown_version():
    with pytest.raises(ValueError):
        et.parse_telemetry(_payload()[:-1])
    with pytest.raises(ValueError):
        et.parse_telemetry(b"\x02" + _payload()[1:])


def test_age_255_is_stale():
    assert et.parse_telemetry(_payload(age=255))[1][0].stale


class _Transport:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read_exact(self, n, timeout):
        chunk, self._data = self._data[:n], self._data[n:]
        if len(chunk) < n:
            raise TimeoutError
        return chunk


def _v2_frame(cmd: int, payload: bytes, ok: bool = True) -> bytes:
    body = bytes([0]) + cmd.to_bytes(2, "little") + len(payload).to_bytes(2, "little") + payload
    return (b"$X>" if ok else b"$X!") + body + bytes([crc8_dvb_s2(body)])


def test_msp_v2_response_is_read():
    import time
    resp = _read_msp_response(_Transport(_v2_frame(0x2F00, b"abc")), time.monotonic() + 1)
    assert resp == MspResponse(0x2F00, b"abc")


def test_msp_v2_error_and_bad_crc_raise():
    import time
    from esc_hotline.msp import MspFramingError
    with pytest.raises(MspFramingError):
        _read_msp_response(_Transport(_v2_frame(0x2F00, b"", ok=False)), time.monotonic() + 1)
    bad = bytearray(_v2_frame(0x2F00, b"abc"))
    bad[-1] ^= 1
    with pytest.raises(MspFramingError):
        _read_msp_response(_Transport(bytes(bad)), time.monotonic() + 1)


def test_probe_false_on_stock_fc_and_true_on_id2bit():
    def stock(*a, **k):
        raise MspError("MSP error response")
    assert et.probe(SimpleNamespace(send=stock))[0] is False
    ok, detail = et.probe(SimpleNamespace(send=lambda *a, **k: MspResponse(0x2F00, _payload())))
    assert ok and "ESC: 2" in detail


def test_run_records_planned_vs_sent_and_summarises(tmp_path, monkeypatch):
    monkeypatch.setattr(et, "TELEMETRY_DIR", tmp_path)
    mgr = MotorTestManager()
    mgr._motor_count = 2
    mgr._msp = SimpleNamespace(send=lambda *a, **k: MspResponse(0x2F00, _payload()))
    logs = []
    mgr.log_message.connect(lambda lvl, txt: logs.append(txt))
    mgr._telemetry.set_available(True)
    mgr._phase, mgr._target, mgr._current = PHASE_UP, 2000, 1500
    mgr._leg_table = [1600] * 100  # planned is ahead of the slew-limited value actually sent
    mgr._telemetry._poll()
    mgr._telemetry._poll()
    mgr._phase = "idle"  # run over -> the next poll closes the CSV and logs the summary
    mgr._telemetry._poll()
    files = list(tmp_path.glob("telemetry_*.csv"))
    assert len(files) == 1
    rows = list(csv.DictReader(files[0].open(encoding="utf-8")))
    assert len(rows) == 4 and rows[0]["planned_msp"] == "1600" and rows[0]["sent_msp"] == "1500"
    assert rows[0]["plan_minus_sent_pct"] == "10.00" and rows[0]["rpm"] == "1000"
    assert rows[0]["new_sample"] == "1" and rows[2]["new_sample"] == "0"  # same frames counter -> repeated sample
    assert any("підсумок прогону" in t and "макс. RPM: ESC1 1000" in t for t in logs)


def test_no_poll_without_firmware_or_run():
    mgr = MotorTestManager()
    mgr._telemetry.ensure_running()  # not available -> stays off
    assert not mgr._telemetry._timer.isActive()
    assert mgr._command_snapshot() is None


def test_lag_estimate_finds_a_known_delay():
    # RPM = the sent throttle delayed by 10 samples of 30 ms
    sent = [min(100.0, i * 4.0) for i in range(60)] + [100.0 - i * 4.0 for i in range(25)]
    sent = [max(0.0, v) for v in sent] + [0.0] * 30
    rpm = [0] * 10 + [int(v * 100) for v in sent[:-10]]
    series = [(i * 0.03, sent[i], rpm[i]) for i in range(len(sent))]
    assert et.estimate_lag_ms(series) == "~300 мс"
    assert et.estimate_lag_ms([(i * 0.03, 50.0, 0) for i in range(60)]) == "немає руху для оцінки"
    assert et.estimate_lag_ms(series[:5]) == "мало даних"


def test_poller_moves_with_the_manager_to_its_worker_thread():
    # regression: the poller used to have no parent, stayed in the GUI thread and its timer never ran
    from PyQt6.QtCore import QThread
    thread = QThread()
    mgr = MotorTestManager()
    mgr.moveToThread(thread)
    assert mgr._telemetry.thread() is thread and mgr._telemetry._timer.thread() is thread


def test_read_exact_never_reconfigures_the_port(monkeypatch):
    from esc_hotline import serial_transport as st

    class FakeSerial:
        def __init__(self):
            self.timeout_sets = 0
            self.chunks = [b"ab", b"", b"cd"]

        def read(self, n):
            import time
            time.sleep(0.005)  # a blocking read slice with no data
            return self.chunks.pop(0)[:n] if self.chunks else b""

        def __setattr__(self, k, v):
            if k == "timeout" and "chunks" in self.__dict__:
                self.__dict__["timeout_sets"] += 1
            object.__setattr__(self, k, v)

    t = st.SerialTransport.__new__(st.SerialTransport)
    t._serial = FakeSerial()
    assert t.read_exact(4, 1.0) == b"abcd" and t._serial.timeout_sets == 0
    t._serial.chunks = [b""] * 1000
    with pytest.raises(TimeoutError):
        t.read_exact(2, 0.05)


def test_glitch_frame_flagged():
    from esc_hotline.esc_telemetry import EscRecord
    base = dict(id=0, active=True, age=0, temp_c=0, voltage_v=0.0, current_a=0.0, consumption_mah=0, erpm=35072,
                frames=1, dt_us=764519, raw_current=0)
    assert EscRecord(rpm=4294532096, **base).glitch
    assert not EscRecord(rpm=10514, **base).glitch
