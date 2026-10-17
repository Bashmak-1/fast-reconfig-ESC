"""Isolated manager for the "Прогон мотором" motor-spin-test tab: sends live
motor-throttle overrides to the FC over plain MSP (MSP_SET_MOTOR) - a
completely different subsystem from the rest of EscHotline, which only ever
reads/writes ESC config bytes through the FourWay/bootloader-passthrough
channel. This never calls MSP_SET_PASSTHROUGH at all.

Own private connection (own SerialTransport/MspClient, mirroring
hotspot_station.py's HotspotStationController isolation pattern: own
QTimer-driven loop, no shared state with esc_manager.py) - a bug here must
never be able to touch the config-flashing path the other tabs rely on.

Protocol facts below verified against iNavFlight/inav source (not guessed -
getting this wrong means either the motor doesn't respond, or worse,
responds in a way the rate-limiting can't control):

- MSP_SET_MOTOR (214): payload is exactly 8x uint16 little-endian
  (src/main/fc/fc_msp.c, `case MSP_SET_MOTOR: if (dataSize == 8 *
  sizeof(uint16_t))`). Values land in `motor_disarmed[]`, which
  src/main/flight/mixer.c's `motor[i] = isDisarmed ? motor_disarmed[i] :
  motorValueWhenStopped` only ever applies to the real output while the FC
  is disarmed - INAV itself refuses to honour these values once armed, no
  extra guard needed here for that specific case.
- MSP_STATUS_EX (150): response layout is
  u16 cycleTime, u16 i2cErrors, u16 sensorStatus, 4 bytes boxModeFlags,
  u8 configProfile, u16 averageSystemLoadPercent, u16 armingFlags,
  u8 accCalibAxisFlags - so armingFlags is a little-endian u16 at byte
  offset 13 (0+2+2+2+4+1+2 = 13), not 11. Deprecated since INAV 9.1
  (MSP2_INAV_STATUS is the replacement) but not yet removed; kept for
  compatibility with both INAV 7.1.1 and 9.x, matching the two versions
  actually in use here.
- ARMED bit: src/main/fc/runtime_config.h `ARMED = (1 << 2)` i.e. 0x04
  within armingFlags.
- Value range 1000 (stop/0%) - 2000 (100%), the standard MSP PWM-style
  range (confirmed against a real MSP_MOTOR dump captured earlier in this
  project: raw e8 03 == 1000).
- Motor protocol (motor_pwm_protocol - STANDARD/ONESHOT125/MULTISHOT/
  BRUSHED/DSHOT150/300/600, see motor_ramp.MOTOR_PROTOCOLS) is read/written
  through MSP_ADVANCED_CONFIG/MSP_SET_ADVANCED_CONFIG (byte offset 3 of a
  9-byte payload - see msp.py). It is only applied at boot by the
  timer/DMA driver init (src/main/drivers/pwm_output.c) - it cannot be
  hot-swapped, so changing it here always means write -> MSP_EEPROM_WRITE
  -> MSP_REBOOT, and the connection has to be treated as gone afterwards.
"""
from __future__ import annotations

import time
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .motor_ramp import (
    MOTOR_PROTOCOLS,
    MSP_MAX,
    MSP_MIN,
    build_advanced_config_payload,
    count_active_motors,
    msp_to_percent,
    percent_to_msp,
    protocol_name,
)
from .msp import MspClient, MspCommand, MspError
from .ramp_chart_math import MAX_PHASE_DURATION_S, clamp_bound_pair, clamp_duration_s, segment_value
from .serial_transport import SerialTransport

MOTOR_SLOTS = 8  # hard MSP_SET_MOTOR/MSP_MOTOR wire-format limit (for (i = 0; i < 8; i++) in fc_msp.c) -
# separate from FourWay/config world's 12-ESC limit (MAX_ESC_COUNT in esc_manager.py). The number of
# these 8 slots that are actually configured motors (motor_count) is detected per-connection, see connect().
ARMED_BIT = 0x04
TICK_MS = 100

PHASE_IDLE = "idle"
PHASE_UP = "up"
PHASE_HOLD = "hold"  # plateau at the ceiling, between ramp-up and ramp-down - 0-duration is valid (skips straight to DOWN)
PHASE_DOWN = "down"

# Ramp shape: RAMP_LINEAR (straight line, the original and still-default
# behavior) or RAMP_BEZIER (eased start/stop - "Безе", offered as a CHOICE
# alongside linear, never a replacement - see
# ramp_chart_math.segment_value()/motor_ramp.cubic_bezier_ease()).
RAMP_LINEAR = "linear"
RAMP_BEZIER = "bezier"

# Test mode: TEST_MODE_ALL (every real motor moves together, the original
# and still-default behavior) or TEST_MODE_SEQUENTIAL (one motor at a
# time, a full up->hold->down cycle each, advancing automatically through
# every motor once and then stopping) - added because a bench lab power
# supply often can't source enough current for two motors at once.
TEST_MODE_ALL = "all"
TEST_MODE_SEQUENTIAL = "sequential"


class MotorTestManager(QObject):
    log_message = pyqtSignal(str, str)  # level, text
    connection_state_changed = pyqtSignal(bool)
    motor_count_detected = pyqtSignal(int)  # how many of the 8 MSP_MOTOR slots are real, right after connect
    armed_check_result = pyqtSignal(bool, str)  # disarmed_confirmed, detail
    value_changed = pyqtSignal(list)  # `motor_count` raw MSP values (1000-2000), one per real motor slot
    phase_changed = pyqtSignal(str)  # PHASE_IDLE / PHASE_UP / PHASE_HOLD / PHASE_DOWN
    protocol_detected = pyqtSignal(int, str)  # index, display name - -1/"" means unknown (right after connect)
    protocol_change_result = pyqtSignal(bool, str)  # success, detail - success implies the FC is rebooting

    def __init__(self) -> None:
        super().__init__()
        self._transport: Optional[SerialTransport] = None
        self._msp: Optional[MspClient] = None
        self._motor_count = 0
        self._current = MSP_MIN
        self._target = MSP_MIN
        self._up_duration_s = 4.0
        self._hold_duration_s = 0.0  # 0 is a real, meaningful value - no plateau, straight up->down
        self._down_duration_s = 4.0
        self._min_target = MSP_MIN  # configurable sweep floor (operator-draggable on the ramp chart, idle-only)
        self._max_target = MSP_MAX  # configurable sweep ceiling
        self._phase = PHASE_IDLE
        self._ramp_mode = RAMP_LINEAR
        self._test_mode = TEST_MODE_ALL
        self._active_motor_index: Optional[int] = None  # which single motor is running, TEST_MODE_SEQUENTIAL only
        self._manual_stop_requested = False  # Stop/panic-key pressed - suppress auto-advance to the next motor
        self._phase_start_time = 0.0  # monotonic() when the current phase (up/hold/down) began
        self._phase_start_value = MSP_MIN  # self._current at that same moment
        self._protocol_index: Optional[int] = None
        self._motor_pwm_rate = 0  # cached from the last MSP_ADVANCED_CONFIG read - must be echoed back
        self._servo_pwm_rate = 0  # unchanged on write, see set_protocol()

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._on_tick)

    def _log(self, level: str, text: str) -> None:
        self.log_message.emit(level, text)

    # ---- lifecycle ------------------------------------------------

    def connect(self, port: str, baud: int) -> None:
        try:
            transport = SerialTransport(port, baud)
        except Exception as e:
            self._log("error", f"Не вдалося відкрити {port}: {e}")
            self.connection_state_changed.emit(False)
            return

        msp = MspClient(transport)
        try:
            try:
                msp.send(MspCommand.MSP_API_VERSION)
            except MspError:
                # FC may be latched in FourWay-relay mode from an earlier
                # EscHotline session on another tab - matches
                # esc_manager.py's own recovery for the same situation.
                from .fourway import FourWayClient

                FourWayClient(transport).exit_interface()
                time.sleep(1.0)
                msp.send(MspCommand.MSP_API_VERSION)
        except Exception as e:
            self._log("error", f"Не вдалося підключитись: {e}")
            transport.close()
            self.connection_state_changed.emit(False)
            return

        self._transport = transport
        self._msp = msp
        self._current = MSP_MIN
        self._target = MSP_MIN
        self._phase = PHASE_IDLE
        self._active_motor_index = None
        self._manual_stop_requested = False
        self._log("info", "Підключено (звичайний MSP, без passthrough).")

        try:
            motor_resp = msp.send(MspCommand.MSP_MOTOR, timeout_s=0.3, retries=3)
            raw_values = [
                int.from_bytes(motor_resp.payload[i : i + 2], "little")
                for i in range(0, min(len(motor_resp.payload), MOTOR_SLOTS * 2), 2)
            ]
            self._motor_count = count_active_motors(raw_values)
        except Exception as e:
            self._log("warning", f"Не вдалось визначити кількість моторів (MSP_MOTOR): {e}")
            self._motor_count = 0
        self._log("info", f"FC повідомляє {self._motor_count} мотор(ів).")

        try:
            adv_resp = msp.send(MspCommand.MSP_ADVANCED_CONFIG, timeout_s=0.3, retries=3)
            if len(adv_resp.payload) < 8:
                raise MspError(f"занадто коротка відповідь MSP_ADVANCED_CONFIG ({len(adv_resp.payload)} байт)")
            self._protocol_index = adv_resp.payload[3]
            self._motor_pwm_rate = int.from_bytes(adv_resp.payload[4:6], "little")
            self._servo_pwm_rate = int.from_bytes(adv_resp.payload[6:8], "little")
            self._log("info", f"Поточний протокол мотора: {protocol_name(self._protocol_index)}.")
        except Exception as e:
            self._log("warning", f"Не вдалось визначити протокол мотора (MSP_ADVANCED_CONFIG): {e}")
            self._protocol_index = None

        self.connection_state_changed.emit(True)
        self.motor_count_detected.emit(self._motor_count)
        self.protocol_detected.emit(
            self._protocol_index if self._protocol_index is not None else -1,
            protocol_name(self._protocol_index) if self._protocol_index is not None else "",
        )

    def disconnect(self) -> None:
        self._stop_timer_and_settle()
        if self._transport is not None:
            self._transport.close()
        self._transport = None
        self._msp = None
        self._motor_count = 0
        self._protocol_index = None
        self.connection_state_changed.emit(False)
        self.motor_count_detected.emit(0)
        self.protocol_detected.emit(-1, "")
        self._log("info", "Відключено.")

    def _stop_timer_and_settle(self) -> None:
        """Immediate (non-ramped) stop, used only on disconnect/shutdown -
        never during normal operation, where every stop goes through the
        ramp. At this point the connection is going away regardless, so
        leaving motors at a known-safe command matters more than a graceful
        ramp we won't be able to see through anyway."""
        self._timer.stop()
        self._phase = PHASE_IDLE
        self.phase_changed.emit(PHASE_IDLE)
        if self._msp is not None:
            try:
                self._send_all(MSP_MIN)
            except Exception:
                pass

    # ---- safety check ------------------------------------------------

    def _read_disarmed_state(self) -> tuple[bool, str]:
        """Read MSP_STATUS_EX and report whether the FC is confirmed
        disarmed. Fail-safe: any read/parse problem reports "not confirmed
        disarmed" rather than assuming it's safe. Shared by check_disarmed()
        (Start button) and set_protocol() (which must never write/save/
        reboot while armed)."""
        if self._msp is None:
            return False, "не підключено"
        try:
            resp = self._msp.send(MspCommand.MSP_STATUS_EX, timeout_s=0.5, retries=3)
            if len(resp.payload) < 15:
                return False, f"занадто коротка відповідь MSP_STATUS_EX ({len(resp.payload)} байт)"
            arming_flags = int.from_bytes(resp.payload[13:15], "little")
            if arming_flags & ARMED_BIT:
                return False, "FC заармлений (ARMED)"
            return True, ""
        except Exception as e:
            return False, f"не вдалось перевірити стан arm: {e}"

    def check_disarmed(self) -> None:
        disarmed, detail = self._read_disarmed_state()
        if disarmed:
            self.armed_check_result.emit(True, "")
        else:
            self.armed_check_result.emit(False, detail or "не вдалось підтвердити стан disarmed")

    def set_protocol(self, index: int) -> None:
        """Write a new motor protocol, save it to EEPROM, and reboot the FC
        so the change actually takes effect - motorPwmProtocol is only
        applied at boot by the timer/DMA driver init
        (src/main/drivers/pwm_output.c), it cannot be hot-swapped. The FC
        drops the connection as part of rebooting; the operator has to
        reconnect afterwards, same as unplugging/replugging it by hand."""
        if self._msp is None:
            self.protocol_change_result.emit(False, "Не підключено.")
            return
        if self._phase != PHASE_IDLE:
            self.protocol_change_result.emit(False, "Неможливо змінити протокол під час прогону.")
            return
        if not (0 <= index < len(MOTOR_PROTOCOLS)):
            self.protocol_change_result.emit(False, f"Невідомий індекс протоколу: {index}.")
            return
        disarmed, detail = self._read_disarmed_state()
        if not disarmed:
            self.protocol_change_result.emit(
                False, f"Зміна протоколу заблокована: {detail or 'не вдалось підтвердити стан disarmed'}"
            )
            return

        name = MOTOR_PROTOCOLS[index]
        try:
            payload = build_advanced_config_payload(index, self._motor_pwm_rate, self._servo_pwm_rate)
            self._msp.send(MspCommand.MSP_SET_ADVANCED_CONFIG, payload, timeout_s=0.3, retries=2)
            self._log("info", f"Протокол записано: {name}. Зберігаю в EEPROM...")
            self._msp.send(MspCommand.MSP_EEPROM_WRITE, timeout_s=1.0, retries=2)
            self._log("info", "Збережено. Перезавантажую FC...")
            self._msp.send(MspCommand.MSP_REBOOT, timeout_s=0.3, retries=1)
        except Exception as e:
            self.protocol_change_result.emit(False, f"Не вдалось змінити протокол: {e}")
            return

        # The FC is rebooting - this connection is dead regardless of
        # whether the OS has noticed the port drop yet, so tear down
        # exactly like disconnect() and let the operator reconnect.
        self._stop_timer_and_settle()
        if self._transport is not None:
            self._transport.close()
        self._transport = None
        self._msp = None
        self._motor_count = 0
        self._protocol_index = None
        self.connection_state_changed.emit(False)
        self.motor_count_detected.emit(0)
        self.protocol_detected.emit(-1, "")
        self.protocol_change_result.emit(True, f"Протокол змінено на {name}. FC перезавантажується - підключіться знову.")

    # ---- ramp control ------------------------------------------------

    def set_ramp_mode(self, mode: str) -> None:
        """RAMP_LINEAR (default) or RAMP_BEZIER - safe to call any time,
        including mid-sweep (takes effect from the next tick, restarting
        the eased curve's own elapsed-time baseline at the CURRENT value
        rather than jumping, if switched to RAMP_BEZIER mid-move)."""
        self._ramp_mode = mode if mode in (RAMP_LINEAR, RAMP_BEZIER) else RAMP_LINEAR
        self._phase_start_time = time.monotonic()
        self._phase_start_value = self._current

    def set_speed_bounds(self, min_percent: float, max_percent: float) -> None:
        """Configurable sweep floor/ceiling - operator-draggable on the
        ramp chart, idle-only (the chart itself only allows dragging while
        PHASE_IDLE, but this guards independently rather than trusting the
        caller). Takes effect on the NEXT sweep start, never retroactively
        mid-sweep."""
        if self._phase != PHASE_IDLE:
            self._log("warning", "Зміна межі розгону під час прогону ігнорується.")
            return
        min_p, max_p = clamp_bound_pair(min_percent, max_percent)
        self._min_target = percent_to_msp(min_p)
        self._max_target = percent_to_msp(max_p)
        self._log("info", f"Межі розгону: {min_p:.0f}%–{max_p:.0f}%.")

    def set_test_mode(self, mode: str) -> None:
        """TEST_MODE_ALL (default) or TEST_MODE_SEQUENTIAL - safe to call
        any time; takes effect on the next Start, never retroactively
        mid-sweep."""
        self._test_mode = mode if mode in (TEST_MODE_ALL, TEST_MODE_SEQUENTIAL) else TEST_MODE_ALL

    def start_sweep(self, up_duration_s: float, hold_duration_s: float, down_duration_s: float) -> None:
        if self._msp is None:
            self._log("error", "Не підключено.")
            return
        if self._motor_count == 0:
            self._log("error", "FC не повідомляє жодного мотора - нема що прогонити.")
            return
        if self._phase != PHASE_IDLE:
            return  # already running - Start is a no-op mid-sweep
        self._up_duration_s = clamp_duration_s(up_duration_s)
        self._hold_duration_s = max(0.0, min(MAX_PHASE_DURATION_S, hold_duration_s))  # 0 is valid - no plateau
        self._down_duration_s = clamp_duration_s(down_duration_s)
        self._manual_stop_requested = False
        self._active_motor_index = 0 if self._test_mode == TEST_MODE_SEQUENTIAL else None
        self._current = self._min_target
        self._target = self._max_target
        self._phase = PHASE_UP
        self._begin_leg()
        self.phase_changed.emit(PHASE_UP)
        shape = "Безе" if self._ramp_mode == RAMP_BEZIER else "лінійний"
        mode_text = f"по черзі, мотор 1 з {self._motor_count}" if self._test_mode == TEST_MODE_SEQUENTIAL else "всі одночасно"
        self._log(
            "info",
            f"Старт розгону ({shape}, {mode_text}): розгін {self._up_duration_s:.1f}с, "
            f"утримання {self._hold_duration_s:.1f}с, спад {self._down_duration_s:.1f}с.",
        )
        self._timer.start()

    def stop_sweep(self) -> None:
        """Used by both the Stop button and the any-key panic handler -
        deliberately identical behaviour for both (no separate "faster"
        emergency path): switch to ramping down to the configured sweep
        floor (_min_target - MSP_MIN unless the operator narrowed the
        bounds), from whatever the current value actually is right now.
        Also suppresses TEST_MODE_SEQUENTIAL's auto-advance to the next
        motor once this down-leg finishes - a manual Stop halts the whole
        run, it never just skips ahead to the next motor."""
        if self._phase == PHASE_IDLE:
            return
        self._manual_stop_requested = True
        self._target = self._min_target
        if self._phase != PHASE_DOWN:
            self._phase = PHASE_DOWN
            self._begin_leg()
            self.phase_changed.emit(PHASE_DOWN)
            self._log("info", f"Зупинка: плавний спад до {msp_to_percent(self._min_target):.0f}%.")

    def _begin_leg(self) -> None:
        """Mark the start of a new phase (up/hold/down) - position is
        always computed purely from elapsed time since THIS moment, so
        every phase change needs a fresh time/value baseline."""
        self._phase_start_time = time.monotonic()
        self._phase_start_value = self._current

    def _on_tick(self) -> None:
        now = time.monotonic()
        elapsed_s = now - self._phase_start_time

        if self._phase == PHASE_HOLD:
            self._current = self._target
        else:
            duration_s = self._up_duration_s if self._phase == PHASE_UP else self._down_duration_s
            start_percent = msp_to_percent(self._phase_start_value)
            target_percent = msp_to_percent(self._target)
            use_bezier = self._ramp_mode == RAMP_BEZIER
            value_percent = segment_value(use_bezier, elapsed_s, duration_s, start_percent, target_percent)
            self._current = percent_to_msp(value_percent)

        try:
            self._send_all(self._current)
        except Exception as e:
            self._log("error", f"Помилка запису MSP_SET_MOTOR: {e} - зупиняю.")
            self._stop_timer_and_settle()
            return

        if self._phase == PHASE_HOLD:
            if elapsed_s >= self._hold_duration_s:
                self._phase = PHASE_DOWN
                self._target = self._min_target
                self._begin_leg()
                self.phase_changed.emit(PHASE_DOWN)
        elif self._phase == PHASE_UP and self._current == self._target:
            if self._hold_duration_s > 0:
                self._phase = PHASE_HOLD
                self._begin_leg()
                self.phase_changed.emit(PHASE_HOLD)
            else:
                self._phase = PHASE_DOWN
                self._target = self._min_target
                self._begin_leg()
                self.phase_changed.emit(PHASE_DOWN)
        elif self._phase == PHASE_DOWN and self._current == self._target:
            self._advance_or_stop()

    def _advance_or_stop(self) -> None:
        """Called once a down-leg completes: in TEST_MODE_SEQUENTIAL
        (and not manually stopped), moves on to the next motor's own
        up->hold->down cycle; otherwise this is the end of the whole run."""
        if (
            not self._manual_stop_requested
            and self._test_mode == TEST_MODE_SEQUENTIAL
            and self._active_motor_index is not None
        ):
            next_index = self._active_motor_index + 1
            if next_index < self._motor_count:
                self._active_motor_index = next_index
                self._current = self._min_target
                self._target = self._max_target
                self._phase = PHASE_UP
                self._begin_leg()
                self.phase_changed.emit(PHASE_UP)
                self._log("info", f"Перехід до мотора {next_index + 1} з {self._motor_count}.")
                return

        self._timer.stop()
        self._phase = PHASE_IDLE
        self._active_motor_index = None
        self._manual_stop_requested = False
        self.phase_changed.emit(PHASE_IDLE)
        self._log("success", f"Прогон завершено, мотори на {msp_to_percent(self._min_target):.0f}%.")

    def _send_all(self, value: int) -> None:
        # MSP_SET_MOTOR's wire format always needs exactly 8 values
        # (fc_msp.c rejects any other dataSize) regardless of how many are
        # real - slots beyond motor_count go out as MSP_MIN (a harmless,
        # already-idle command) and are never reported to the UI.
        assert self._msp is not None
        if self._test_mode == TEST_MODE_SEQUENTIAL and self._active_motor_index is not None:
            # Only the active motor moves - every other real slot stays at
            # a harmless stop command, so a lab power supply that can't
            # source enough current for two motors at once never has to.
            values = [MSP_MIN] * self._motor_count
            if 0 <= self._active_motor_index < self._motor_count:
                values[self._active_motor_index] = value
        else:
            values = [value] * self._motor_count
        payload = b"".join(v.to_bytes(2, "little") for v in values)
        payload += b"".join(MSP_MIN.to_bytes(2, "little") for _ in range(MOTOR_SLOTS - self._motor_count))
        self._msp.send(MspCommand.MSP_SET_MOTOR, payload, timeout_s=0.3, retries=2)
        self.value_changed.emit(values)
