""""Прошивка FC" tab: auto-detects a DFU-mode flight controller, flashes
its firmware via the external STM32CubeProgrammer CLI, verifies the write,
resets it into normal run mode, then watches and flashes one or more
operator-chosen ESC outputs at once (checkable "ESC" menu, defaults to
ESC 1+2 - the common case is a 2-in-1 ESC board wired to two outputs on one
FC) - the same spirit as "Хотспот (лінія)" uses for the ESC side, but
generalized to a small chosen set instead of exactly one slot. Once every
watched ESC slot is confirmed good, the motor-protocol dropdown here
(defaults to DSHOT300) is written to the FC for real (MSP_SET_ADVANCED_
CONFIG -> EEPROM -> reboot, "Save and Reboot") and the FC reboots - a
one-shot full board-provisioning run, not an open-ended hot-swap line
(see fc_flash_station.py's docstring for the full state machine and why
it's a separate implementation rather than a shared one).

Driven by its own private connections (FcFlashStationController -> its own
ConnectionWorker/EscManager/thread for the ESC phase, its own DFU worker
thread for the CLI calls, and its own protocol-write worker thread for the
final MSP step) - nothing shared with the Main/Config/Хотспот tabs except
the usual cross-tab port-busy check.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .eeprom_config import ConfigFileError, load_config_file
from .esc_manager import MAX_ESC_COUNT
from .fc_flash_station import (
    MAX_CONNECT_ATTEMPTS,
    MAX_POLL_INTERVAL_MS,
    MIN_POLL_INTERVAL_MS,
    POLL_INTERVAL_STEP_MS,
    PHASE_LABEL,
    PHASE_STYLE,
    RETRY_PHASES,
    FcFlashStationController,
)
from .manual_counter_widget import ManualCounterWidget
from .motor_ramp import MOTOR_PROTOCOLS
from .motor_test_manager import (
    PHASE_IDLE as MOTOR_PHASE_IDLE,
    RAMP_BEZIER,
    RAMP_LINEAR,
    TEST_MODE_ALL,
    TEST_MODE_SEQUENTIAL,
)
from .motor_test_widget import _NoWheelComboBox as QComboBox
from .motor_test_widget import _make_duration_spinbox
from .ramp_chart_math import MIN_PHASE_DURATION_S
from .ramp_chart_widget import RampChartWidget
from .serial_transport import list_fc_ports
from .settings_store import load_float, load_int, load_str, save_float, save_int, save_str
from .theme import ACCENT_AMBER, ACCENT_ERROR, TEXT_MUTED

# Same persisted keys "Прогон мотором" (motor_test_widget.py) owns -
# deliberately the SAME keys, not a separate set, so changing the ramp
# config from either tab affects both: fc_flash_station.py reads these
# fresh right before it runs the automatic post-provision motor test, and
# MainWindow wires each tab's ramp_settings_changed signal to the other's
# apply_external_ramp_settings() for live sync while both are open.
_MOTOR_UP_DURATION_KEY = "motor_test_up_duration_s"
_MOTOR_HOLD_DURATION_KEY = "motor_test_hold_duration_s"
_MOTOR_DOWN_DURATION_KEY = "motor_test_down_duration_s"
_MOTOR_RAMP_MODE_KEY = "motor_test_ramp_mode"
_MOTOR_TEST_MODE_KEY = "motor_test_mode"
_MOTOR_MIN_BOUND_KEY = "motor_test_min_percent"
_MOTOR_MAX_BOUND_KEY = "motor_test_max_percent"
_DEFAULT_MOTOR_UP_DURATION_S = 4.0
_DEFAULT_MOTOR_HOLD_DURATION_S = 0.0
_DEFAULT_MOTOR_DOWN_DURATION_S = 4.0
_DEFAULT_MOTOR_MIN_BOUND = 0
_DEFAULT_MOTOR_MAX_BOUND = 100
_RAMP_MODE_LABELS = {RAMP_LINEAR: "Лінійна", RAMP_BEZIER: "Безе"}
_TEST_MODE_LABELS = {TEST_MODE_ALL: "Всі одночасно", TEST_MODE_SEQUENTIAL: "По черзі"}

_FIRMWARE_PATH_KEY = "fc_flash_firmware_path"
_CONFIG_PATH_KEY = "fc_flash_esc_config_path"
_PORT_KEY = "fc_flash_port"
_ESC_INDICES_KEY = "fc_flash_esc_indices"  # comma-separated 0-based indices, e.g. "0,1"
_PROTOCOL_INDEX_KEY = "fc_flash_protocol_index"
_CLI_PATH_OVERRIDE_KEY = "fc_flash_cli_path_override"
_POLL_INTERVAL_KEY = "fc_flash_poll_interval_ms"
_FLASH_FC_ENABLED_KEY = "fc_flash_stage_fc_enabled"
_FLASH_ESC_ENABLED_KEY = "fc_flash_stage_esc_enabled"
_MOTOR_TEST_ENABLED_KEY = "fc_flash_stage_motor_test_enabled"
_DEFAULT_ESC_INDICES = (0, 1)  # ESC 1 + ESC 2 - the common case is a 2-in-1 ESC board on one FC

_FIRMWARE_FILTER = "Firmware (*.hex *.bin);;All files (*)"
_CONFIG_FILTER = "Binary config (*.bin);;All files (*)"
_DEFAULT_PROTOCOL_INDEX = MOTOR_PROTOCOLS.index("DSHOT300")

# Phases that are genuinely doing something but have no real percentage to
# report - shown as an indeterminate "busy" bar. "flashing_fc" is handled
# separately (real 0-100% from dfu_flash_progress_changed); idle/all_done/
# *_error phases are left out entirely - the progress bar hides for those.
_BUSY_PHASES_NO_PERCENT = frozenset({
    "waiting_device", "resetting_fc", "waiting_fc_normal", "connecting", "watching", "flashing", "esc_ready",
    "writing_protocol", "waiting_fc_for_motor_test", "connecting_motor_test",
    "checking_motor_armed", "running_motor_test",
})


class FcFlashPage(QWidget):
    # Mirrors motor_test_widget.MotorTestPage's own identical signal -
    # MainWindow wires each tab's ramp_settings_changed to the other's
    # apply_external_ramp_settings() so both stay live-synced.
    # Order: up_s, hold_s, down_s, ramp_mode, test_mode, min_percent, max_percent.
    ramp_settings_changed = pyqtSignal(float, float, float, str, str, float, float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.controller = FcFlashStationController(self)
        self._config_bytes: Optional[bytes] = None
        self._config_path: str = ""
        self._firmware_path: str = ""
        self._port_busy_checker: Optional[Callable[[str], bool]] = None
        self._power_cycle_hint_shown = False
        self._esc_detail_by_index: dict[int, str] = {}

        self._build_ui()

        # Same purely-cosmetic faster-than-the-controller countdown refresh
        # as hotspot_line_widget.py - an impatient operator staring at a
        # static banner for a few seconds easily mistakes "still polling"
        # for "stuck".
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(150)
        self._countdown_timer.timeout.connect(self._update_countdown)

        self.controller.phase_changed.connect(self._on_phase_changed)
        self.controller.esc_detail_changed.connect(self._on_esc_detail_changed)
        self.controller.dfu_detail_changed.connect(self._on_dfu_detail_changed)
        self.controller.counters_changed.connect(self._on_counters_changed)
        self.controller.armed_changed.connect(self._on_armed_changed)
        self.controller.power_cycle_hint_changed.connect(self._on_power_cycle_hint_changed)
        self.controller.port_changed.connect(self._on_port_auto_changed)
        self.controller.motor_test_phase_changed.connect(self._on_motor_test_phase_changed)
        self.controller.motor_test_value_changed.connect(self._on_motor_test_value_changed)
        self.controller.dfu_flash_progress_changed.connect(self._on_dfu_flash_progress)
        self.controller.fc_flash_succeeded.connect(lambda: self._fc_counter.increment())
        self.controller.esc_flash_succeeded.connect(lambda: self._esc_counter.increment())
        self.controller.motor_test_succeeded.connect(lambda: self._motor_counter.increment())

        self._restore_settings()

    # ---- construction ------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        container = QWidget()
        scroll.setWidget(container)
        root = QVBoxLayout(container)

        firmware_row = QHBoxLayout()
        # Stage toggle - unchecked skips the whole DFU search/flash/reset
        # phase and auto-detects the FC already in normal mode instead
        # (see fc_flash_station.py's set_flash_fc_enabled() docstring).
        self._flash_fc_checkbox = QCheckBox("Прошивка FC")
        self._flash_fc_checkbox.setChecked(True)
        self._flash_fc_checkbox.toggled.connect(self._on_flash_fc_toggled)
        firmware_row.addWidget(self._flash_fc_checkbox)
        self._load_firmware_btn = QPushButton("Прошивка FC...")
        self._load_firmware_btn.clicked.connect(self._on_load_firmware_clicked)
        firmware_row.addWidget(self._load_firmware_btn)
        self._firmware_label = QLabel("Файл прошивки FC не обрано.")
        self._firmware_label.setWordWrap(True)
        self._firmware_label.setMaximumWidth(700)
        firmware_row.addWidget(self._firmware_label, 1)
        root.addLayout(firmware_row)

        config_row = QHBoxLayout()
        # Stage toggle - unchecked skips the ESC watch/flash loop AND the
        # protocol write that follows it, straight to the motor test (see
        # fc_flash_station.py's set_flash_esc_enabled() docstring).
        self._flash_esc_checkbox = QCheckBox("Прошивка ESC")
        self._flash_esc_checkbox.setChecked(True)
        self._flash_esc_checkbox.toggled.connect(self._on_flash_esc_toggled)
        config_row.addWidget(self._flash_esc_checkbox)
        self._load_config_btn = QPushButton("Конфіг ESC...")
        self._load_config_btn.clicked.connect(self._on_load_config_clicked)
        config_row.addWidget(self._load_config_btn)
        self._config_label = QLabel("Файл конфігу ESC не обрано.")
        self._config_label.setWordWrap(True)
        self._config_label.setMaximumWidth(700)
        config_row.addWidget(self._config_label, 1)
        root.addLayout(config_row)

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("Порт (звичайний режим):"))
        self._port_combo = QComboBox()
        self._port_combo.setMinimumWidth(200)
        self._port_combo.currentIndexChanged.connect(self._on_port_changed)
        port_row.addWidget(self._port_combo)
        self._refresh_btn = QPushButton("↻")
        self._refresh_btn.setFixedSize(36, 30)
        self._refresh_btn.setStyleSheet("font-size: 16px; padding: 0px;")
        self._refresh_btn.clicked.connect(self._refresh_ports)
        port_row.addWidget(self._refresh_btn)
        port_row.addWidget(QLabel("ESC:"))
        self._esc_menu_btn = QPushButton()
        self._esc_menu = QMenu(self._esc_menu_btn)
        self._esc_actions: list[QAction] = []
        for i in range(MAX_ESC_COUNT):
            action = QAction(f"ESC {i + 1}", self._esc_menu)
            action.setCheckable(True)
            action.toggled.connect(self._on_esc_selection_changed)
            self._esc_menu.addAction(action)
            self._esc_actions.append(action)
        self._esc_menu_btn.setMenu(self._esc_menu)
        port_row.addWidget(self._esc_menu_btn)
        port_row.addStretch(1)
        self._start_stop_btn = QPushButton("Старт")
        self._start_stop_btn.setObjectName("connectButton")
        self._start_stop_btn.setMinimumWidth(120)
        self._start_stop_btn.clicked.connect(self._on_start_stop_clicked)
        port_row.addWidget(self._start_stop_btn)
        root.addLayout(port_row)

        protocol_row = QHBoxLayout()
        protocol_row.addWidget(QLabel("Протокол (для довідки, не записується автоматично):"))
        self._protocol_combo = QComboBox()
        self._protocol_combo.addItems(MOTOR_PROTOCOLS)
        self._protocol_combo.currentIndexChanged.connect(self._on_protocol_changed)
        protocol_row.addWidget(self._protocol_combo)
        protocol_row.addStretch(1)
        root.addLayout(protocol_row)

        self._start_hint_label = QLabel()
        self._start_hint_label.setStyleSheet(f"color: {ACCENT_AMBER};")
        self._start_hint_label.setVisible(False)
        root.addWidget(self._start_hint_label)

        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setMinimumHeight(90)
        self._banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._banner)

        # ONE progress indicator for every busy phase, right under the
        # banner - real 0-100% during the DFU flash/verify itself (the
        # only step with genuine percentage data, parsed live from the
        # external STM32CubeProgrammer CLI's own output - see
        # dfu_flash_manager.py), a generic indeterminate "busy" animation
        # (range 0,0 - Qt's own marquee) for every other phase that's
        # doing something but has no percentage to report, hidden
        # entirely at idle/done/error. Replaces staring at a blank banner
        # with no visible sign anything is happening.
        self._progress_bar = QProgressBar()
        self._progress_bar.setVisible(False)
        root.addWidget(self._progress_bar)

        self._countdown_label = QLabel()
        self._countdown_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._countdown_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._countdown_label)

        # Grouped with the countdown above (the thing it controls), not
        # with the port/ESC/config row - like hotspot_line_widget.py's
        # identical control, this stays enabled while armed (see
        # _on_armed_changed): it only touches QTimer.setInterval(), no
        # protocol/connection-state implication, so an operator watching a
        # slow-booting ESC can raise it live without stopping and losing
        # the current watch.
        poll_interval_row = QHBoxLayout()
        poll_interval_row.addStretch(1)
        poll_interval_row.addWidget(QLabel("Інтервал опиту ESC, мс:"))
        self._poll_interval_stepper = ManualCounterWidget(
            min_value=MIN_POLL_INTERVAL_MS, max_value=MAX_POLL_INTERVAL_MS, step=POLL_INTERVAL_STEP_MS
        )
        self._poll_interval_stepper.setToolTip(
            "Як часто перевіряти, чи відповідає ESC. Збільшіть, якщо ESC довго прокидається "
            "після підключення живлення - занадто часті спроби можуть заклинити bootloader."
        )
        self._poll_interval_stepper.value_changed.connect(self._on_poll_interval_changed)
        poll_interval_row.addWidget(self._poll_interval_stepper)
        self._poll_interval_seconds_label = QLabel()
        self._poll_interval_seconds_label.setStyleSheet(f"color: {TEXT_MUTED};")
        poll_interval_row.addWidget(self._poll_interval_seconds_label)
        poll_interval_row.addStretch(1)
        root.addLayout(poll_interval_row)

        self._retry_btn = QPushButton("Повторити")
        self._retry_btn.clicked.connect(self.controller.retry)
        self._retry_btn.setVisible(False)
        root.addWidget(self._retry_btn)

        self._dfu_detail_label = QLabel()
        self._dfu_detail_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._dfu_detail_label.setWordWrap(True)
        root.addWidget(self._dfu_detail_label)

        self._lab_power_hint_label = QLabel(
            "⚡ Підключіть лабораторний блок живлення (силові) — без напруги на силові ESC не будуть видимі."
        )
        self._lab_power_hint_label.setStyleSheet(f"color: {ACCENT_AMBER}; font-weight: 600;")
        self._lab_power_hint_label.setWordWrap(True)
        self._lab_power_hint_label.setVisible(False)
        root.addWidget(self._lab_power_hint_label)

        self._detail_label = QLabel()
        self._detail_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._detail_label.setWordWrap(True)
        root.addWidget(self._detail_label)

        # Ramp config for the final automatic motor-test step this tab runs
        # on its own (see fc_flash_station.py) - SAME shared settings as
        # "Прогон мотором" (not a separate copy), kept in sync live via
        # ramp_settings_changed/apply_external_ramp_settings (wired in
        # main_window.py). Dragging stays blocked automatically while a
        # test is actually running - RampChartWidget.set_phase() only
        # allows it in PHASE_IDLE, same safeguard "Прогон мотором" has.
        motor_rate_row = QHBoxLayout()
        # Stage toggle - unchecked skips the final automatic motor-spin
        # step entirely, whatever earlier stages ran still happen as
        # configured (see fc_flash_station.py's set_motor_test_enabled()
        # docstring).
        self._motor_test_checkbox = QCheckBox("Тест моторів")
        self._motor_test_checkbox.setChecked(True)
        self._motor_test_checkbox.toggled.connect(self._on_motor_test_toggled)
        motor_rate_row.addWidget(self._motor_test_checkbox)
        motor_rate_row.addSpacing(24)
        motor_rate_row.addWidget(QLabel("Графік рампи:"))
        self._motor_ramp_mode_combo = QComboBox()
        self._motor_ramp_mode_combo.addItem(_RAMP_MODE_LABELS[RAMP_LINEAR], RAMP_LINEAR)
        self._motor_ramp_mode_combo.addItem(_RAMP_MODE_LABELS[RAMP_BEZIER], RAMP_BEZIER)
        self._motor_ramp_mode_combo.currentIndexChanged.connect(self._on_motor_ramp_mode_changed)
        motor_rate_row.addWidget(self._motor_ramp_mode_combo)
        motor_rate_row.addSpacing(24)
        motor_rate_row.addWidget(QLabel("Режим тесту:"))
        self._motor_test_mode_combo = QComboBox()
        self._motor_test_mode_combo.addItem(_TEST_MODE_LABELS[TEST_MODE_ALL], TEST_MODE_ALL)
        self._motor_test_mode_combo.addItem(_TEST_MODE_LABELS[TEST_MODE_SEQUENTIAL], TEST_MODE_SEQUENTIAL)
        self._motor_test_mode_combo.currentIndexChanged.connect(self._on_motor_test_mode_changed)
        motor_rate_row.addWidget(self._motor_test_mode_combo)
        motor_rate_row.addStretch(1)
        root.addLayout(motor_rate_row)

        motor_duration_row = QHBoxLayout()
        motor_duration_row.addWidget(QLabel("Розгін, с:"))
        self._motor_up_stepper = _make_duration_spinbox(MIN_PHASE_DURATION_S)
        self._motor_up_stepper.valueChanged.connect(self._on_motor_duration_changed)
        motor_duration_row.addWidget(self._motor_up_stepper)
        motor_duration_row.addSpacing(16)
        motor_duration_row.addWidget(QLabel("Утримання, с:"))
        self._motor_hold_stepper = _make_duration_spinbox(0.0)
        self._motor_hold_stepper.valueChanged.connect(self._on_motor_duration_changed)
        motor_duration_row.addWidget(self._motor_hold_stepper)
        motor_duration_row.addSpacing(16)
        motor_duration_row.addWidget(QLabel("Спад, с:"))
        self._motor_down_stepper = _make_duration_spinbox(MIN_PHASE_DURATION_S)
        self._motor_down_stepper.valueChanged.connect(self._on_motor_duration_changed)
        motor_duration_row.addWidget(self._motor_down_stepper)
        motor_duration_row.addStretch(1)
        root.addLayout(motor_duration_row)

        self._motor_chart = RampChartWidget()
        self._motor_chart.setMinimumHeight(180)
        self._motor_chart.profile_changed.connect(self._on_motor_chart_profile_changed)
        root.addWidget(self._motor_chart)

        self._power_cycle_hint_label = QLabel()
        self._power_cycle_hint_label.setStyleSheet(f"color: {ACCENT_AMBER};")
        self._power_cycle_hint_label.setWordWrap(True)
        self._power_cycle_hint_label.setVisible(False)
        root.addWidget(self._power_cycle_hint_label)
        root.addStretch(1)

        counters_row = QHBoxLayout()
        self._counters_label = QLabel()
        self._counters_label.setStyleSheet(f"color: {TEXT_MUTED};")
        counters_row.addWidget(self._counters_label, 1)
        # Per-stage running totals (same ManualCounterWidget "Хотспот"'s own
        # "Всього перероблено" counter uses) - each one auto +1 exactly once
        # per board, the moment its own stage finishes successfully (see
        # fc_flash_station.py's fc_flash_succeeded/esc_flash_succeeded/
        # motor_test_succeeded signals), and freely correctable by hand
        # with +/- or typing a number, same as the automatic flashed/ok/
        # errors breakdown above can't always cover (a re-seated ESC
        # re-flashed by hand, a miscount, a bench restart mid-batch). A
        # stage whose checkbox is off never fires its signal in the first
        # place, so nothing special is needed here to respect that.
        fc_counter_label = QLabel("FC:")
        fc_counter_label.setStyleSheet(f"color: {TEXT_MUTED};")
        counters_row.addWidget(fc_counter_label)
        self._fc_counter = ManualCounterWidget()
        counters_row.addWidget(self._fc_counter)

        esc_counter_label = QLabel("ESC:")
        esc_counter_label.setStyleSheet(f"color: {TEXT_MUTED};")
        counters_row.addWidget(esc_counter_label)
        self._esc_counter = ManualCounterWidget()
        counters_row.addWidget(self._esc_counter)

        motor_counter_label = QLabel("Мотори:")
        motor_counter_label.setStyleSheet(f"color: {TEXT_MUTED};")
        counters_row.addWidget(motor_counter_label)
        self._motor_counter = ManualCounterWidget()
        counters_row.addWidget(self._motor_counter)
        self._reset_counters_btn = QPushButton("Скинути")
        self._reset_counters_btn.clicked.connect(self._on_reset_counters_clicked)
        counters_row.addWidget(self._reset_counters_btn)
        root.addLayout(counters_row)

        self._update_counters_label()
        self._render_banner()
        self._update_start_stop_button()

    # ---- firmware / config / port / ESC-slot / protocol pickers ---------

    def _on_load_firmware_clicked(self) -> None:
        downloads = str(Path.home() / "Downloads")
        path, _ = QFileDialog.getOpenFileName(self, "Прошивка FC", downloads, _FIRMWARE_FILTER)
        if not path:
            return
        self._apply_firmware_path(path)
        save_str(_FIRMWARE_PATH_KEY, path)

    def _apply_firmware_path(self, path: str) -> None:
        self._firmware_path = path
        self._firmware_label.setText(path)
        self.controller.set_firmware_path(path)
        self._update_start_stop_button()

    def _on_load_config_clicked(self) -> None:
        downloads = str(Path.home() / "Downloads")
        path, _ = QFileDialog.getOpenFileName(self, "Конфіг ESC", downloads, _CONFIG_FILTER)
        if not path:
            return
        try:
            config_bytes = load_config_file(path)
        except ConfigFileError as e:
            self._config_label.setText(str(e))
            self._config_label.setStyleSheet(f"color: {ACCENT_ERROR};")
            return
        self._apply_config(path, config_bytes)
        save_str(_CONFIG_PATH_KEY, path)

    def _apply_config(self, path: str, config_bytes: bytes) -> None:
        self._config_path = path
        self._config_bytes = config_bytes
        self._config_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._config_label.setText(path)
        self.controller.set_config(path, config_bytes)
        self._update_start_stop_button()

    def _refresh_ports(self) -> None:
        current = self._port_combo.currentData()
        self._port_combo.blockSignals(True)
        self._port_combo.clear()
        for info in list_fc_ports():
            label = f"{info.device} - {info.description}"
            if info.likely_fc:
                label += "  ★"
            self._port_combo.addItem(label, info.device)
        if current:
            idx = self._port_combo.findData(current)
            if idx >= 0:
                self._port_combo.setCurrentIndex(idx)
        self._port_combo.blockSignals(False)
        self.controller.set_port(self._selected_port_device())
        self._update_start_stop_button()

    def _selected_port_device(self) -> Optional[str]:
        data = self._port_combo.currentData()
        return str(data) if data else None

    def _selected_esc_indices(self) -> list[int]:
        return [i for i, action in enumerate(self._esc_actions) if action.isChecked()]

    def _on_port_changed(self, *_args) -> None:
        port = self._selected_port_device()
        self.controller.set_port(port)
        if port:
            save_str(_PORT_KEY, port)
        self._update_start_stop_button()

    def _on_port_auto_changed(self, new_port: str) -> None:
        """The controller itself switched to a different COM port - it
        detected the FC re-enumerated under a new number after a DFU
        flash+reset (confirmed to happen in practice, sometimes climbing
        past COM400+ - see fc_flash_station.py's module docstring). Reflect
        that in the combo and remember it, so a later manual Старт reuses
        the port that's actually there instead of a now-stale one."""
        idx = self._port_combo.findData(new_port)
        if idx < 0:
            self._port_combo.addItem(new_port, new_port)
            idx = self._port_combo.findData(new_port)
        self._port_combo.blockSignals(True)
        self._port_combo.setCurrentIndex(idx)
        self._port_combo.blockSignals(False)
        save_str(_PORT_KEY, new_port)
        # Signals were blocked above specifically to avoid re-triggering
        # _on_port_changed() (which would otherwise redundantly call
        # controller.set_port() right back) - but that also means the
        # Start/Stop button's enabled-state and hint text, which only
        # _on_port_changed()/_update_start_stop_button() ever refresh,
        # are never recalculated here. Confirmed in practice: if Start was
        # disabled (e.g. "Порт вже використовується іншою вкладкою.")
        # because of whatever port was selected BEFORE this auto-switch,
        # it stayed disabled with that stale hint even once the FC
        # re-enumerated onto a perfectly good, free port - this call is
        # what was missing.
        self._update_start_stop_button()

    def _on_esc_selection_changed(self, *_args) -> None:
        indices = self._selected_esc_indices()
        self.controller.set_esc_indices(indices)
        save_str(_ESC_INDICES_KEY, ",".join(str(i) for i in indices))
        self._update_esc_menu_button_text()
        self._update_start_stop_button()

    def _update_esc_menu_button_text(self) -> None:
        indices = self._selected_esc_indices()
        label = ", ".join(str(i + 1) for i in indices) if indices else "не обрано"
        self._esc_menu_btn.setText(f"ESC: {label}")

    def _on_protocol_changed(self, index: int) -> None:
        save_int(_PROTOCOL_INDEX_KEY, index)
        self.controller.set_protocol_index(index)

    def _on_poll_interval_changed(self, value: int) -> None:
        self.controller.set_poll_interval_ms(value)
        save_int(_POLL_INTERVAL_KEY, value)
        self._update_poll_interval_seconds_label()

    def _on_flash_fc_toggled(self, checked: bool) -> None:
        save_int(_FLASH_FC_ENABLED_KEY, 1 if checked else 0)
        self.controller.set_flash_fc_enabled(checked)
        self._load_firmware_btn.setEnabled(checked)
        self._update_start_stop_button()

    def _on_flash_esc_toggled(self, checked: bool) -> None:
        save_int(_FLASH_ESC_ENABLED_KEY, 1 if checked else 0)
        self.controller.set_flash_esc_enabled(checked)
        self._load_config_btn.setEnabled(checked)
        self._esc_menu_btn.setEnabled(checked)
        self._update_start_stop_button()

    def _on_motor_test_toggled(self, checked: bool) -> None:
        save_int(_MOTOR_TEST_ENABLED_KEY, 1 if checked else 0)
        self.controller.set_motor_test_enabled(checked)
        self._update_start_stop_button()

    def _update_poll_interval_seconds_label(self) -> None:
        seconds = self._poll_interval_stepper.value() / 1000
        self._poll_interval_seconds_label.setText(f"{seconds:.1f}с")

    def _restore_settings(self) -> None:
        flash_fc_enabled = load_int(_FLASH_FC_ENABLED_KEY, 1) != 0
        self._flash_fc_checkbox.setChecked(flash_fc_enabled)
        self.controller.set_flash_fc_enabled(flash_fc_enabled)
        self._load_firmware_btn.setEnabled(flash_fc_enabled)

        flash_esc_enabled = load_int(_FLASH_ESC_ENABLED_KEY, 1) != 0
        self._flash_esc_checkbox.setChecked(flash_esc_enabled)
        self.controller.set_flash_esc_enabled(flash_esc_enabled)
        self._load_config_btn.setEnabled(flash_esc_enabled)
        self._esc_menu_btn.setEnabled(flash_esc_enabled)

        motor_test_enabled = load_int(_MOTOR_TEST_ENABLED_KEY, 1) != 0
        self._motor_test_checkbox.setChecked(motor_test_enabled)
        self.controller.set_motor_test_enabled(motor_test_enabled)

        firmware_path = load_str(_FIRMWARE_PATH_KEY)
        if firmware_path and Path(firmware_path).is_file():
            self._apply_firmware_path(firmware_path)

        config_path = load_str(_CONFIG_PATH_KEY)
        if config_path and Path(config_path).is_file():
            try:
                config_bytes = load_config_file(config_path)
            except ConfigFileError:
                config_bytes = None
            if config_bytes is not None:
                self._apply_config(config_path, config_bytes)

        cli_override = load_str(_CLI_PATH_OVERRIDE_KEY)
        self.controller.set_cli_path_override(cli_override or None)

        self._refresh_ports()
        remembered_port = load_str(_PORT_KEY)
        if remembered_port:
            idx = self._port_combo.findData(remembered_port)
            if idx >= 0:
                self._port_combo.setCurrentIndex(idx)
                self.controller.set_port(self._selected_port_device())

        raw_indices = load_str(_ESC_INDICES_KEY)
        if raw_indices:
            try:
                indices = sorted({int(x) for x in raw_indices.split(",") if x.strip() != ""})
            except ValueError:
                indices = list(_DEFAULT_ESC_INDICES)
        else:
            indices = list(_DEFAULT_ESC_INDICES)
        indices = [i for i in indices if 0 <= i < MAX_ESC_COUNT]
        for action in self._esc_actions:
            action.blockSignals(True)
        for i, action in enumerate(self._esc_actions):
            action.setChecked(i in indices)
        for action in self._esc_actions:
            action.blockSignals(False)
        self.controller.set_esc_indices(self._selected_esc_indices())
        self._update_esc_menu_button_text()
        # Without this, _refresh_ports() above already ran
        # _update_start_stop_button() once BEFORE these ESC-slot
        # checkboxes were restored - at that moment _selected_esc_indices()
        # was still empty (every QAction starts unchecked), so with "Прошивка
        # ESC" enabled (the saved/default state) Start got disabled with
        # "Оберіть хоча б один ESC-слот..." and NOTHING afterward ever
        # refreshed it again, even though the indices are now correctly
        # restored - confirmed real-world: this is exactly the stale-disabled
        # Start button operators had to work around by clicking the port
        # refresh (↻) button once at every app startup, which only "fixed"
        # it by incidentally re-running _update_start_stop_button() too.
        self._update_start_stop_button()

        protocol_index = load_int(_PROTOCOL_INDEX_KEY, _DEFAULT_PROTOCOL_INDEX)
        if 0 <= protocol_index < self._protocol_combo.count():
            self._protocol_combo.setCurrentIndex(protocol_index)
        # Push explicitly - setCurrentIndex() above only fires
        # currentIndexChanged (and _on_protocol_changed's controller push)
        # when the index actually CHANGES, same reasoning as the ESC-slot/
        # bounds restores elsewhere in this method.
        self.controller.set_protocol_index(self._protocol_combo.currentIndex())

        self._poll_interval_stepper.set_value(load_int(_POLL_INTERVAL_KEY, self.controller.poll_interval_ms()))
        self.controller.set_poll_interval_ms(self._poll_interval_stepper.value())
        self._update_poll_interval_seconds_label()

        self._apply_motor_durations(
            load_float(_MOTOR_UP_DURATION_KEY, _DEFAULT_MOTOR_UP_DURATION_S),
            load_float(_MOTOR_HOLD_DURATION_KEY, _DEFAULT_MOTOR_HOLD_DURATION_S),
            load_float(_MOTOR_DOWN_DURATION_KEY, _DEFAULT_MOTOR_DOWN_DURATION_S),
        )
        ramp_mode = load_str(_MOTOR_RAMP_MODE_KEY, RAMP_LINEAR)
        idx = self._motor_ramp_mode_combo.findData(ramp_mode if ramp_mode in _RAMP_MODE_LABELS else RAMP_LINEAR)
        if idx >= 0:
            self._motor_ramp_mode_combo.setCurrentIndex(idx)
        self._motor_chart.set_curve_mode(self._motor_ramp_mode_combo.currentData())

        test_mode = load_str(_MOTOR_TEST_MODE_KEY, TEST_MODE_ALL)
        idx = self._motor_test_mode_combo.findData(test_mode if test_mode in _TEST_MODE_LABELS else TEST_MODE_ALL)
        if idx >= 0:
            self._motor_test_mode_combo.setCurrentIndex(idx)

        min_pct = load_int(_MOTOR_MIN_BOUND_KEY, _DEFAULT_MOTOR_MIN_BOUND)
        max_pct = load_int(_MOTOR_MAX_BOUND_KEY, _DEFAULT_MOTOR_MAX_BOUND)
        self._motor_chart.set_bounds(min_pct, max_pct)

    # ---- start/stop ------------------------------------------------

    def set_port_busy_checker(self, checker: Callable[[str], bool]) -> None:
        self._port_busy_checker = checker

    def recheck_availability(self) -> None:
        if not self.controller.armed:
            self._update_start_stop_button()

    def _on_start_stop_clicked(self) -> None:
        if self.controller.armed:
            self.controller.stop()
            return
        port = self._selected_port_device()
        if port and self._port_busy_checker and self._port_busy_checker(port):
            self._set_start_hint(f"{port} вже використовується іншою вкладкою.")
            return
        self.controller.start()

    def shutdown(self) -> None:
        self.controller.shutdown()

    # ---- rendering ------------------------------------------------

    def _on_phase_changed(self, phase: str) -> None:
        self._render_banner()
        self._update_start_stop_button()
        self._update_countdown()
        self._lab_power_hint_label.setVisible(phase in ("watching", "esc_error"))
        self._update_progress_bar(phase)

    def _update_progress_bar(self, phase: str) -> None:
        if phase == "flashing_fc":
            self._progress_bar.setVisible(True)
            self._progress_bar.setRange(0, 100)
            self._progress_bar.setValue(0)
            self._progress_bar.setFormat("Прошивка: %p%")
        elif phase in _BUSY_PHASES_NO_PERCENT:
            self._progress_bar.setVisible(True)
            self._progress_bar.setRange(0, 0)  # Qt's own indeterminate/marquee animation
            self._progress_bar.setFormat("")
        else:
            self._progress_bar.setVisible(False)

    def _on_dfu_flash_progress(self, stage: str, percent: int) -> None:
        if self.controller.phase != "flashing_fc":
            return
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(percent)
        self._progress_bar.setFormat(f"{'Запис' if stage == 'write' else 'Перевірка'}: %p%")

    def _on_motor_test_phase_changed(self, phase: str) -> None:
        self._motor_chart.set_phase(phase)

    def _on_motor_test_value_changed(self, values: list) -> None:
        if values:
            self._motor_chart.set_current_value(values[0])

    # ---- ramp settings (shared with "Прогон мотором" - see module-level
    # key constants above) ------------------------------------------------

    def _current_motor_ramp_settings_tuple(self) -> tuple[float, float, float, str, str, float, float]:
        return (
            self._motor_up_stepper.value(),
            self._motor_hold_stepper.value(),
            self._motor_down_stepper.value(),
            self._motor_ramp_mode_combo.currentData(),
            self._motor_test_mode_combo.currentData(),
            float(load_int(_MOTOR_MIN_BOUND_KEY, _DEFAULT_MOTOR_MIN_BOUND)),
            float(load_int(_MOTOR_MAX_BOUND_KEY, _DEFAULT_MOTOR_MAX_BOUND)),
        )

    def _on_motor_ramp_mode_changed(self, _index: int) -> None:
        mode = self._motor_ramp_mode_combo.currentData()
        save_str(_MOTOR_RAMP_MODE_KEY, mode)
        self._motor_chart.set_curve_mode(mode)
        self.ramp_settings_changed.emit(*self._current_motor_ramp_settings_tuple())

    def _on_motor_test_mode_changed(self, _index: int) -> None:
        mode = self._motor_test_mode_combo.currentData()
        save_str(_MOTOR_TEST_MODE_KEY, mode)
        self.ramp_settings_changed.emit(*self._current_motor_ramp_settings_tuple())

    def _on_motor_duration_changed(self, _value: float) -> None:
        save_float(_MOTOR_UP_DURATION_KEY, self._motor_up_stepper.value())
        save_float(_MOTOR_HOLD_DURATION_KEY, self._motor_hold_stepper.value())
        save_float(_MOTOR_DOWN_DURATION_KEY, self._motor_down_stepper.value())
        self._motor_chart.set_durations(
            self._motor_up_stepper.value(), self._motor_hold_stepper.value(), self._motor_down_stepper.value()
        )
        self.ramp_settings_changed.emit(*self._current_motor_ramp_settings_tuple())

    def _apply_motor_durations(self, up_s: float, hold_s: float, down_s: float) -> None:
        # Same blockSignals-during-batch-set pattern as motor_test_widget.
        # MotorTestPage._apply_durations() - see that method's docstring
        # for why setting the three steppers one at a time WITHOUT
        # blocking corrupts settings_store with a stale intermediate value.
        for stepper, value in (
            (self._motor_up_stepper, up_s),
            (self._motor_hold_stepper, hold_s),
            (self._motor_down_stepper, down_s),
        ):
            stepper.blockSignals(True)
            stepper.setValue(round(value, 1))
            stepper.blockSignals(False)
        save_float(_MOTOR_UP_DURATION_KEY, self._motor_up_stepper.value())
        save_float(_MOTOR_HOLD_DURATION_KEY, self._motor_hold_stepper.value())
        save_float(_MOTOR_DOWN_DURATION_KEY, self._motor_down_stepper.value())
        self._motor_chart.set_durations(
            self._motor_up_stepper.value(), self._motor_hold_stepper.value(), self._motor_down_stepper.value()
        )

    def _on_motor_chart_profile_changed(
        self, up_s: float, hold_s: float, down_s: float, min_percent: float, max_percent: float
    ) -> None:
        self._apply_motor_durations(up_s, hold_s, down_s)
        save_int(_MOTOR_MIN_BOUND_KEY, round(min_percent))
        save_int(_MOTOR_MAX_BOUND_KEY, round(max_percent))
        self.ramp_settings_changed.emit(*self._current_motor_ramp_settings_tuple())

    def apply_external_ramp_settings(
        self, up_s: float, hold_s: float, down_s: float, ramp_mode: str, test_mode: str, min_percent: float, max_percent: float
    ) -> None:
        """Called by MainWindow when the sibling "Прогон мотором" tab
        changed these same shared settings - mirrors them here too,
        without re-emitting ramp_settings_changed (that tab already saved
        settings_store itself - see main_window.py's wiring)."""
        self._apply_motor_durations(up_s, hold_s, down_s)
        idx = self._motor_ramp_mode_combo.findData(ramp_mode if ramp_mode in _RAMP_MODE_LABELS else RAMP_LINEAR)
        if idx >= 0:
            self._motor_ramp_mode_combo.blockSignals(True)
            self._motor_ramp_mode_combo.setCurrentIndex(idx)
            self._motor_ramp_mode_combo.blockSignals(False)
        self._motor_chart.set_curve_mode(self._motor_ramp_mode_combo.currentData())
        idx = self._motor_test_mode_combo.findData(test_mode if test_mode in _TEST_MODE_LABELS else TEST_MODE_ALL)
        if idx >= 0:
            self._motor_test_mode_combo.blockSignals(True)
            self._motor_test_mode_combo.setCurrentIndex(idx)
            self._motor_test_mode_combo.blockSignals(False)
        self._motor_chart.set_bounds(min_percent, max_percent)

    def _on_esc_detail_changed(self, index: int, state: str, detail: str) -> None:
        self._esc_detail_by_index[index] = f"ESC {index + 1}: {detail}" if detail else f"ESC {index + 1}: -"
        lines = [self._esc_detail_by_index[i] for i in sorted(self._esc_detail_by_index)]
        self._detail_label.setText("\n".join(lines))

    def _on_dfu_detail_changed(self, detail: str) -> None:
        self._dfu_detail_label.setText(detail)

    def _on_power_cycle_hint_changed(self, show: bool) -> None:
        self._power_cycle_hint_shown = show
        self._power_cycle_hint_label.setText(
            "Досі не бачу ESC — перевірте, що лабораторний блок живлення підключено й увімкнено, "
            "або спробуйте перепідключити ESC по живленню." if show else ""
        )
        self._power_cycle_hint_label.setVisible(show)

    def _on_armed_changed(self, armed: bool) -> None:
        self._flash_fc_checkbox.setEnabled(not armed)
        self._flash_esc_checkbox.setEnabled(not armed)
        self._motor_test_checkbox.setEnabled(not armed)
        self._load_firmware_btn.setEnabled(not armed and self._flash_fc_checkbox.isChecked())
        self._load_config_btn.setEnabled(not armed and self._flash_esc_checkbox.isChecked())
        self._port_combo.setEnabled(not armed)
        self._refresh_btn.setEnabled(not armed)
        self._esc_menu_btn.setEnabled(not armed and self._flash_esc_checkbox.isChecked())
        self._protocol_combo.setEnabled(not armed)
        if armed:
            self._countdown_timer.start()
        else:
            self._countdown_timer.stop()
            self._esc_detail_by_index = {}
            self._detail_label.setText("")
            self._dfu_detail_label.setText("")
            self._countdown_label.setText("")
            self._on_power_cycle_hint_changed(False)
            self._lab_power_hint_label.setVisible(False)
            self._motor_chart.set_phase(MOTOR_PHASE_IDLE)
        self._update_start_stop_button()

    def _update_countdown(self) -> None:
        phase = self.controller.phase
        if phase == "connecting":
            self._countdown_label.setText(
                f"Спроба підключення {self.controller.connect_attempts()} з {MAX_CONNECT_ATTEMPTS}"
            )
            return
        remaining_ms = self.controller.poll_timer_remaining_ms()
        if phase in ("flashing", "flashing_fc", "resetting_fc") or remaining_ms < 0:
            self._countdown_label.setText("")
            return
        self._countdown_label.setText(f"Наступна перевірка через {remaining_ms / 1000:.3f}с")

    def _render_banner(self) -> None:
        bg, fg = PHASE_STYLE[self.controller.phase]
        self._banner.setStyleSheet(
            f"background-color: {bg}; color: {fg}; font-size: 22px; font-weight: 700;"
            "padding: 24px; border-radius: 8px;"
        )
        self._banner.setText(PHASE_LABEL[self.controller.phase])
        self._retry_btn.setVisible(self.controller.phase in RETRY_PHASES)

    def _update_start_stop_button(self) -> None:
        if self.controller.armed:
            self._start_stop_btn.setText("Стоп")
            busy_phase = self.controller.phase in (
                "flashing_fc", "resetting_fc", "flashing", "writing_protocol",
                "connecting_motor_test", "checking_motor_armed", "running_motor_test",
            )
            self._start_stop_btn.setEnabled(not busy_phase)
            self._set_start_hint("Неможливо зупинити під час прошивки." if busy_phase else "")
            return

        self._start_stop_btn.setText("Старт")
        flash_fc = self._flash_fc_checkbox.isChecked()
        flash_esc = self._flash_esc_checkbox.isChecked()
        if flash_esc and self._config_bytes is None:
            self._start_stop_btn.setEnabled(False)
            self._set_start_hint("Оберіть файл конфігу ESC (або зніміть галочку «Прошивка ESC»).")
        elif flash_esc and not self._selected_esc_indices():
            self._start_stop_btn.setEnabled(False)
            self._set_start_hint("Оберіть хоча б один ESC-слот (або зніміть галочку «Прошивка ESC»).")
        elif self._selected_port_device() and self._port_busy_checker and self._port_busy_checker(self._selected_port_device()):
            self._start_stop_btn.setEnabled(False)
            self._set_start_hint("Порт вже використовується іншою вкладкою.")
        else:
            # A pre-selected normal-mode port is NOT required to start: the
            # DFU reset (flash_fc on) and "waiting_fc_normal" (flash_fc
            # off) phases both auto-detect the FC's port themselves (see
            # fc_flash_station.py's module docstring) - while the FC sits
            # in DFU mode it naturally shows up as no normal COM port at
            # all, so requiring one here would make Start impossible to
            # press exactly when this feature is meant to be used.
            self._start_stop_btn.setEnabled(True)
            hint = ""
            if flash_fc and not self._firmware_path:
                hint = "Файл прошивки FC не обрано - DFU-крок буде пропущено."
            elif not self._selected_port_device():
                hint = "Порт не обрано - FC буде знайдено автоматично."
            self._set_start_hint(hint)

    def _set_start_hint(self, text: str) -> None:
        self._start_stop_btn.setToolTip(text)
        self._start_hint_label.setText(text)
        self._start_hint_label.setVisible(bool(text))

    def _update_counters_label(self) -> None:
        flashed, ok, errors = self.controller.counters()
        self._counters_label.setText(f"Прошито: {flashed} | Вже коректно: {ok} | Помилок: {errors}")

    def _on_counters_changed(self, flashed: int, ok: int, errors: int) -> None:
        self._update_counters_label()

    def _on_reset_counters_clicked(self) -> None:
        self.controller.reset_counters()
        self._fc_counter.reset()
        self._esc_counter.reset()
        self._motor_counter.reset()
