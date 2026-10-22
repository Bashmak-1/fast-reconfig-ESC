""""Прогон мотором" tab: a semi-automatic ESC-on-motor bench test. Replaces
manually dragging INAV Configurator's Master output slider (which the user
has twice ramped down too abruptly, burning an FC controller each time) with
a rate-limited automatic 0% -> 100% -> 0% sweep, so an instant power drop is
not physically possible through this tool.

Own private connection (own QThread + MotorTestManager), mirroring
hotspot_station.py's isolation pattern - talks plain MSP to the FC, never
touches esc_manager.py or the FourWay/passthrough world the other tabs use.

Deliberately does not let the operator drag the motor sliders by hand: the
whole point of this feature is that an abrupt manual change is exactly what
already caused damage, so the only two ways to command the motors here are
the rate-limited Start sweep and the rate-limited Stop (button or any key).
"""
from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import QEvent, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .motor_ramp import MOTOR_PROTOCOLS, MSP_MAX, MSP_MIN, msp_to_percent
from .motor_test_manager import (
    PHASE_DOWN,
    PHASE_HOLD,
    PHASE_IDLE,
    PHASE_UP,
    RAMP_BEZIER,
    RAMP_LINEAR,
    TEST_MODE_ALL,
    TEST_MODE_SEQUENTIAL,
    MotorTestManager,
)
from .ramp_chart_math import MIN_PHASE_DURATION_S
from .ramp_chart_widget import RampChartWidget
from .serial_transport import list_fc_ports
from .settings_store import load_float, load_int, load_str, save_float, save_int, save_str
from .theme import ACCENT_AMBER, ACCENT_ERROR, ACCENT_GREEN, TEXT_MUTED

_PORT_KEY = "motor_test_port"
_UP_DURATION_KEY = "motor_test_up_duration_s"
_HOLD_DURATION_KEY = "motor_test_hold_duration_s"
_DOWN_DURATION_KEY = "motor_test_down_duration_s"
_RAMP_MODE_KEY = "motor_test_ramp_mode"
_TEST_MODE_KEY = "motor_test_mode"
_MIN_BOUND_KEY = "motor_test_min_percent"
_MAX_BOUND_KEY = "motor_test_max_percent"
DEFAULT_BAUD = 115200
DEFAULT_UP_DURATION_S = 4.0
DEFAULT_HOLD_DURATION_S = 0.0
DEFAULT_DOWN_DURATION_S = 4.0
DEFAULT_MIN_BOUND = 0
DEFAULT_MAX_BOUND = 100
_MAX_DURATION_S = 60.0
_DURATION_STEP_S = 0.1
_SLIDER_HEIGHT = 70
_SLIDER_WIDTH = 22
_RAMP_MODE_LABELS = {RAMP_LINEAR: "Лінійна", RAMP_BEZIER: "Безе"}
_TEST_MODE_LABELS = {TEST_MODE_ALL: "Всі одночасно", TEST_MODE_SEQUENTIAL: "По черзі"}

_PHASE_LABELS = {
    PHASE_IDLE: "Очікування",
    PHASE_UP: "Розгін...",
    PHASE_HOLD: "Утримання...",
    PHASE_DOWN: "Спад...",
}
_STATUS_COLORS = {"info": TEXT_MUTED, "warning": ACCENT_AMBER, "error": ACCENT_ERROR, "success": ACCENT_GREEN}


class _NoWheelComboBox(QComboBox):
    """The mouse wheel never changes the selection, under any
    circumstance (not even click-to-engage, unlike config_editor_widget.
    py's own _ClickToWheelMixin for its sliders) - both motor-test tabs
    live inside a QScrollArea now, and silently flipping a dropdown's
    selection while the operator is just scrolling past it on the way
    down the page is worse than a slider nudge: the new value is easy to
    not even notice. Confirmed operator request: dropdowns must never
    react to the wheel at all, full stop."""

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


class _NoWheelDoubleSpinBox(QDoubleSpinBox):
    """Same rationale as _NoWheelComboBox - scrolling the page must never
    nudge a duration value just because the cursor happened to pass over
    its spinbox on the way down."""

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


def _make_duration_spinbox(min_value: float) -> QDoubleSpinBox:
    """Tenths-of-a-second duration input - a standard QDoubleSpinBox
    (decimals=1, step=0.1s) replaces the old whole-seconds-only
    ManualCounterWidget stepper, matching what ramp_chart_math.py's
    duration math (and dragging a chart point) already supported
    internally all along."""
    box = _NoWheelDoubleSpinBox()
    box.setDecimals(1)
    box.setSingleStep(_DURATION_STEP_S)
    box.setRange(min_value, _MAX_DURATION_S)
    box.setFixedWidth(80)
    return box


class MotorTestPage(QWidget):
    # GUI -> worker thread. Must be emitted, never called directly on
    # self.manager - it lives on self._thread, see uid_lab_widget.py's
    # (experimental/uid-lab branch) identical comment for why a direct call
    # would block the GUI thread on the manager's own blocking serial I/O.
    _connect_requested = pyqtSignal(str, int)
    _disconnect_requested = pyqtSignal()
    _check_disarmed_requested = pyqtSignal()
    _start_sweep_requested = pyqtSignal(float, float, float)
    _stop_sweep_requested = pyqtSignal()
    _set_protocol_requested = pyqtSignal(int)
    _set_ramp_mode_requested = pyqtSignal(str)
    _set_test_mode_requested = pyqtSignal(str)
    _set_speed_bounds_requested = pyqtSignal(float, float)

    # GUI-thread-affinity signal for MainWindow's cross-tab port-collision guard.
    connection_changed = pyqtSignal(bool)

    # Fires whenever this tab changes the shared ramp settings (duration,
    # curve mode, test mode, or speed bounds) - MainWindow wires this to
    # the sibling "Прошивка FC" tab's apply_external_ramp_settings() so
    # both tabs' charts/controls stay in sync live, not just via the
    # shared settings_store keys at next restart. Order: up_s, hold_s,
    # down_s, ramp_mode, test_mode, min_percent, max_percent.
    ramp_settings_changed = pyqtSignal(float, float, float, str, str, float, float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self._thread = QThread()
        self._thread.setObjectName("MotorTestManagerThread")
        self.manager = MotorTestManager()
        self.manager.moveToThread(self._thread)
        self._thread.start()

        self._connected = False
        self._protocol_known = False
        self._port_busy_checker: Optional[Callable[[str], bool]] = None

        self._build_ui()
        self._wire_signals()
        self._refresh_ports()
        self._restore_settings()

        # App-level filter, not a keyPressEvent override: a keypress is
        # delivered to whichever child widget currently has focus first
        # (Старт/Стоп/Підключити button, port combo, rate stepper), and
        # QPushButton in particular consumes Enter/Space itself as a click
        # instead of letting it bubble up to this page's own keyPressEvent.
        # An app-level filter runs before any widget's own handling, so
        # "any key" is genuinely any key, no matter what has focus.
        QApplication.instance().installEventFilter(self)

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

        warning = QLabel(
            "⚠ Перед стартом: гвинт ЗНЯТО, мотор надійно закріплений на стенді. "
            "Керування йде напряму через MSP, в обхід звичайного RC-арму."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(f"color: {ACCENT_AMBER}; font-weight: 600;")
        root.addWidget(warning)

        conn_row = QHBoxLayout()
        conn_row.addWidget(QLabel("Порт:"))
        self._port_combo = _NoWheelComboBox()
        self._port_combo.setMinimumWidth(220)
        conn_row.addWidget(self._port_combo)
        self._refresh_btn = QPushButton("↻")
        self._refresh_btn.setFixedSize(36, 30)
        self._refresh_btn.clicked.connect(self._refresh_ports)
        conn_row.addWidget(self._refresh_btn)
        conn_row.addStretch(1)
        self._connect_btn = QPushButton("Підключити")
        self._connect_btn.setObjectName("connectButton")
        self._connect_btn.clicked.connect(self._on_connect_toggle_clicked)
        conn_row.addWidget(self._connect_btn)
        root.addLayout(conn_row)

        # Motor protocol (STANDARD/ONESHOT125/MULTISHOT/BRUSHED/DSHOT150/
        # 300/600 - motor_ramp.MOTOR_PROTOCOLS) is FC-level config, read
        # fresh from MSP_ADVANCED_CONFIG on every connect (see
        # _on_protocol_detected) - the FC itself is the persistent store,
        # nothing to cache app-side. Changing it writes + saves to EEPROM +
        # reboots the FC (see MotorTestManager.set_protocol), so it's
        # gated behind an explicit confirmation, not applied on selection.
        protocol_row = QHBoxLayout()
        protocol_row.addWidget(QLabel("Протокол мотора:"))
        self._protocol_combo = _NoWheelComboBox()
        self._protocol_combo.addItems(MOTOR_PROTOCOLS)
        self._protocol_combo.setEnabled(False)
        protocol_row.addWidget(self._protocol_combo)
        self._apply_protocol_btn = QPushButton("Застосувати")
        self._apply_protocol_btn.setEnabled(False)
        self._apply_protocol_btn.clicked.connect(self._on_apply_protocol_clicked)
        protocol_row.addWidget(self._apply_protocol_btn)
        protocol_row.addStretch(1)
        root.addLayout(protocol_row)

        # Individual motor indicators - display-only (setEnabled(False), see
        # module docstring for why manual dragging is deliberately not
        # offered), vertical "fader" style matching INAV Configurator's own
        # Outputs tab. Built dynamically by _rebuild_motor_sliders() once
        # motor_test_manager.py detects how many of MSP_MOTOR's 8 slots are
        # real (see motor_ramp.count_active_motors) - nothing here until
        # then, not a fixed 8 with unused ones just greyed out.
        self._motor_sliders: list[QSlider] = []
        self._motor_labels: list[QLabel] = []
        self._motor_row = QHBoxLayout()
        self._motor_row.setSpacing(10)
        root.addLayout(self._motor_row)
        self._rebuild_motor_sliders(0)

        master_row = QHBoxLayout()
        master_label = QLabel("Master:")
        master_label.setStyleSheet("font-weight: 700;")
        master_row.addWidget(master_label)
        self._master_progress = QProgressBar()
        self._master_progress.setRange(0, 100)
        master_row.addWidget(self._master_progress, 1)
        self._master_pct_label = QLabel("0%")
        self._master_pct_label.setFixedWidth(50)
        self._master_pct_label.setStyleSheet("font-weight: 700; font-size: 16px;")
        master_row.addWidget(self._master_pct_label)
        root.addLayout(master_row)

        self._phase_label = QLabel(_PHASE_LABELS[PHASE_IDLE])
        self._phase_label.setStyleSheet(f"color: {TEXT_MUTED};")
        root.addWidget(self._phase_label)

        rate_row = QHBoxLayout()
        rate_row.addWidget(QLabel("Графік рампи:"))
        self._ramp_mode_combo = _NoWheelComboBox()
        self._ramp_mode_combo.addItem(_RAMP_MODE_LABELS[RAMP_LINEAR], RAMP_LINEAR)
        self._ramp_mode_combo.addItem(_RAMP_MODE_LABELS[RAMP_BEZIER], RAMP_BEZIER)
        self._ramp_mode_combo.setToolTip(
            "Лінійна - стала швидкість (як і раніше). Безе - плавний розгін/спад без різкого старту й зупинки."
        )
        self._ramp_mode_combo.currentIndexChanged.connect(self._on_ramp_mode_changed)
        rate_row.addWidget(self._ramp_mode_combo)
        rate_row.addSpacing(24)
        rate_row.addWidget(QLabel("Режим тесту:"))
        self._test_mode_combo = _NoWheelComboBox()
        self._test_mode_combo.addItem(_TEST_MODE_LABELS[TEST_MODE_ALL], TEST_MODE_ALL)
        self._test_mode_combo.addItem(_TEST_MODE_LABELS[TEST_MODE_SEQUENTIAL], TEST_MODE_SEQUENTIAL)
        self._test_mode_combo.setToolTip(
            "«По черзі» прогонить кожен мотор окремо (розгін-утримання-спад), один раз по черзі, й зупиниться - "
            "корисно, якщо лабораторного блока живлення не вистачає на кілька моторів одразу."
        )
        self._test_mode_combo.currentIndexChanged.connect(self._on_test_mode_changed)
        rate_row.addWidget(self._test_mode_combo)
        rate_row.addStretch(1)
        root.addLayout(rate_row)

        duration_row = QHBoxLayout()
        duration_row.addWidget(QLabel("Розгін, с:"))
        self._up_duration_stepper = _make_duration_spinbox(MIN_PHASE_DURATION_S)
        self._up_duration_stepper.valueChanged.connect(self._on_duration_changed)
        duration_row.addWidget(self._up_duration_stepper)
        duration_row.addSpacing(16)
        duration_row.addWidget(QLabel("Утримання, с:"))
        self._hold_duration_stepper = _make_duration_spinbox(0.0)
        self._hold_duration_stepper.valueChanged.connect(self._on_duration_changed)
        duration_row.addWidget(self._hold_duration_stepper)
        duration_row.addSpacing(16)
        duration_row.addWidget(QLabel("Спад, с:"))
        self._down_duration_stepper = _make_duration_spinbox(MIN_PHASE_DURATION_S)
        self._down_duration_stepper.valueChanged.connect(self._on_duration_changed)
        duration_row.addWidget(self._down_duration_stepper)
        duration_row.addStretch(1)
        root.addLayout(duration_row)

        self._ramp_chart = RampChartWidget()
        self._ramp_chart.setMinimumHeight(180)
        root.addWidget(self._ramp_chart)

        btn_row = QHBoxLayout()
        self._start_btn = QPushButton("Старт")
        self._start_btn.setObjectName("connectButton")
        self._start_btn.setEnabled(False)
        self._start_btn.clicked.connect(self._on_start_clicked)
        btn_row.addWidget(self._start_btn)
        self._stop_btn = QPushButton("Стоп")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop_clicked)
        btn_row.addWidget(self._stop_btn)
        root.addLayout(btn_row)

        self._hint_label = QLabel(
            "Будь-яка клавіша (поки ця вкладка у фокусі) = аварійна зупинка - той самий плавний спад, що й Стоп."
        )
        self._hint_label.setStyleSheet(f"color: {TEXT_MUTED};")
        root.addWidget(self._hint_label)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        root.addWidget(self._status_label)

        root.addStretch(1)

    def _rebuild_motor_sliders(self, count: int) -> None:
        """Tear down whatever motor indicators are currently shown and
        build exactly `count` vertical fader-style ones (or a placeholder
        if 0) - called on connect (once motor_test_manager.py has read
        MSP_MOTOR and detected the real count) and on disconnect (count=0).
        Each column is a genuine QWidget (not a bare nested layout) so
        clearing is just deleteLater() on the container - Qt's parent/child
        ownership cascades the delete to its slider+labels automatically.
        """
        while self._motor_row.count():
            item = self._motor_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # hide() first: deleteLater() only schedules actual C++
                # destruction for the next event-loop iteration, so a
                # widget merely removed from the layout via takeAt() stays
                # visible, painted at its old position, until then -
                # confirmed by an actual screenshot showing the previous
                # placeholder text overlapping the freshly-built "ESC N"
                # labels. hide() takes effect immediately, deleteLater()
                # still reclaims memory once Qt gets around to it.
                widget.hide()
                widget.deleteLater()
        self._motor_sliders.clear()
        self._motor_labels.clear()

        if count == 0:
            placeholder = QLabel("Немає даних — підключіться, щоб побачити мотори FC.")
            placeholder.setStyleSheet(f"color: {TEXT_MUTED};")
            self._motor_row.addWidget(placeholder)
            self._motor_row.addStretch(1)
            return

        for i in range(count):
            column = QWidget()
            col_layout = QVBoxLayout(column)
            col_layout.setContentsMargins(0, 0, 0, 0)
            col_layout.setSpacing(2)
            name = QLabel(f"ESC {i + 1}")
            name.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col_layout.addWidget(name)
            slider = QSlider(Qt.Orientation.Vertical)
            # theme.py's base `QSlider { min-height/max-height: 20px }` rule
            # targets horizontal sliders' thickness, but for a vertical
            # slider height is the travel axis - it silently clamped every
            # vertical fader's usable travel to 20px regardless of
            # setFixedHeight() below, so all handles rendered at the same
            # spot no matter the value. #motorFader overrides it back open.
            slider.setObjectName("motorFader")
            slider.setRange(MSP_MIN, MSP_MAX)
            slider.setValue(MSP_MIN)
            slider.setEnabled(False)
            slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            slider.setFixedHeight(_SLIDER_HEIGHT)
            slider.setFixedWidth(_SLIDER_WIDTH)
            col_layout.addWidget(slider, alignment=Qt.AlignmentFlag.AlignHCenter)
            value_label = QLabel("0%")
            value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col_layout.addWidget(value_label)
            self._motor_row.addWidget(column)
            self._motor_sliders.append(slider)
            self._motor_labels.append(value_label)
        self._motor_row.addStretch(1)

    def _wire_signals(self) -> None:
        self._connect_requested.connect(self.manager.connect)
        self._disconnect_requested.connect(self.manager.disconnect)
        self._check_disarmed_requested.connect(self.manager.check_disarmed)
        self._start_sweep_requested.connect(self.manager.start_sweep)
        self._stop_sweep_requested.connect(self.manager.stop_sweep)
        self._set_protocol_requested.connect(self.manager.set_protocol)
        self._set_ramp_mode_requested.connect(self.manager.set_ramp_mode)
        self._set_test_mode_requested.connect(self.manager.set_test_mode)
        self._set_speed_bounds_requested.connect(self.manager.set_speed_bounds)

        self._ramp_chart.profile_changed.connect(self._on_chart_profile_changed)
        self._ramp_mode_combo.currentIndexChanged.connect(
            lambda _i: self._ramp_chart.set_curve_mode(self._ramp_mode_combo.currentData())
        )

        self.manager.connection_state_changed.connect(self._on_connection_state_changed)
        self.manager.motor_count_detected.connect(self._rebuild_motor_sliders)
        self.manager.log_message.connect(self._on_log_message)
        self.manager.armed_check_result.connect(self._on_armed_check_result)
        self.manager.value_changed.connect(self._on_value_changed)
        self.manager.phase_changed.connect(self._on_phase_changed)
        self.manager.protocol_detected.connect(self._on_protocol_detected)
        self.manager.protocol_change_result.connect(self._on_protocol_change_result)

    # ---- port picker ------------------------------------------------

    def _refresh_ports(self) -> None:
        current = self._port_combo.currentData()
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

    def _selected_port(self) -> Optional[str]:
        data = self._port_combo.currentData()
        return str(data) if data else None

    def _restore_settings(self) -> None:
        port = load_str(_PORT_KEY)
        if port:
            idx = self._port_combo.findData(port)
            if idx >= 0:
                self._port_combo.setCurrentIndex(idx)
        self._apply_durations(
            load_float(_UP_DURATION_KEY, DEFAULT_UP_DURATION_S),
            load_float(_HOLD_DURATION_KEY, DEFAULT_HOLD_DURATION_S),
            load_float(_DOWN_DURATION_KEY, DEFAULT_DOWN_DURATION_S),
        )
        ramp_mode = load_str(_RAMP_MODE_KEY, RAMP_LINEAR)
        idx = self._ramp_mode_combo.findData(ramp_mode if ramp_mode in _RAMP_MODE_LABELS else RAMP_LINEAR)
        if idx >= 0:
            self._ramp_mode_combo.setCurrentIndex(idx)
        self._set_ramp_mode_requested.emit(self._ramp_mode_combo.currentData())

        test_mode = load_str(_TEST_MODE_KEY, TEST_MODE_ALL)
        idx = self._test_mode_combo.findData(test_mode if test_mode in _TEST_MODE_LABELS else TEST_MODE_ALL)
        if idx >= 0:
            self._test_mode_combo.setCurrentIndex(idx)
        self._set_test_mode_requested.emit(self._test_mode_combo.currentData())

        # Push the restored curve mode into the chart explicitly -
        # setCurrentIndex() above only fires currentIndexChanged when the
        # index actually CHANGES, so a restored value that happens to
        # match the combo's just-constructed default would otherwise
        # never reach the freshly-built RampChartWidget (durations were
        # already pushed by _apply_durations() above).
        self._ramp_chart.set_curve_mode(self._ramp_mode_combo.currentData())
        min_pct = load_int(_MIN_BOUND_KEY, DEFAULT_MIN_BOUND)
        max_pct = load_int(_MAX_BOUND_KEY, DEFAULT_MAX_BOUND)
        self._ramp_chart.set_bounds(min_pct, max_pct)
        self._set_speed_bounds_requested.emit(min_pct, max_pct)

    def _current_ramp_settings_tuple(self) -> tuple[float, float, float, str, str, float, float]:
        return (
            self._up_duration_stepper.value(),
            self._hold_duration_stepper.value(),
            self._down_duration_stepper.value(),
            self._ramp_mode_combo.currentData(),
            self._test_mode_combo.currentData(),
            float(load_int(_MIN_BOUND_KEY, DEFAULT_MIN_BOUND)),
            float(load_int(_MAX_BOUND_KEY, DEFAULT_MAX_BOUND)),
        )

    def apply_external_ramp_settings(
        self, up_s: float, hold_s: float, down_s: float, ramp_mode: str, test_mode: str, min_percent: float, max_percent: float
    ) -> None:
        """Called by MainWindow when the sibling "Прошивка FC" tab's own
        chart/controls changed these same shared settings - mirrors them
        here too (that tab already saved settings_store itself, so this
        only updates this tab's own live widgets/manager, never re-emits
        ramp_settings_changed - that would ping-pong the two tabs forever)."""
        self._apply_durations(up_s, hold_s, down_s)
        idx = self._ramp_mode_combo.findData(ramp_mode if ramp_mode in _RAMP_MODE_LABELS else RAMP_LINEAR)
        if idx >= 0:
            self._ramp_mode_combo.blockSignals(True)
            self._ramp_mode_combo.setCurrentIndex(idx)
            self._ramp_mode_combo.blockSignals(False)
        self._ramp_chart.set_curve_mode(self._ramp_mode_combo.currentData())
        self._set_ramp_mode_requested.emit(self._ramp_mode_combo.currentData())
        idx = self._test_mode_combo.findData(test_mode if test_mode in _TEST_MODE_LABELS else TEST_MODE_ALL)
        if idx >= 0:
            self._test_mode_combo.blockSignals(True)
            self._test_mode_combo.setCurrentIndex(idx)
            self._test_mode_combo.blockSignals(False)
        self._set_test_mode_requested.emit(self._test_mode_combo.currentData())
        self._ramp_chart.set_bounds(min_percent, max_percent)
        self._set_speed_bounds_requested.emit(min_percent, max_percent)

    # ---- port-collision guard (same real-hardware-confirmed risk as the
    # two Хотспот tabs - see main_window.py's _is_port_busy_for_* methods) --

    def set_port_busy_checker(self, checker: Callable[[str], bool]) -> None:
        self._port_busy_checker = checker

    def recheck_availability(self) -> None:
        if not self._connected:
            port = self._selected_port()
            busy = bool(port and self._port_busy_checker and self._port_busy_checker(port))
            self._connect_btn.setEnabled(not busy)
            self._connect_btn.setToolTip(f"{port} вже використовується іншою вкладкою." if busy else "")

    def connected_port(self) -> Optional[str]:
        return self._selected_port() if self._connected else None

    # ---- connect / disconnect ------------------------------------------------

    def _on_connect_toggle_clicked(self) -> None:
        if self._connected:
            self._connect_btn.setEnabled(False)
            self._disconnect_requested.emit()
            return
        port = self._selected_port()
        if not port:
            self._on_log_message("error", "COM-порт не обрано.")
            return
        if self._port_busy_checker and self._port_busy_checker(port):
            self._on_log_message("error", f"{port} вже використовується іншою вкладкою.")
            return
        save_str(_PORT_KEY, port)
        self._connect_btn.setEnabled(False)
        self._on_log_message("info", f"Підключення до {port} @ {DEFAULT_BAUD}...")
        self._connect_requested.emit(port, DEFAULT_BAUD)

    def _on_connection_state_changed(self, connected: bool) -> None:
        self._connected = connected
        self._connect_btn.setEnabled(True)
        self._connect_btn.setText("Відключити" if connected else "Підключити")
        self._start_btn.setEnabled(connected)
        self._stop_btn.setEnabled(False)
        if not connected:
            # motor_count_detected(0), also emitted by manager.disconnect(),
            # already resets the slider row back to the placeholder.
            self._master_progress.setValue(0)
            self._master_pct_label.setText("0%")
            self._phase_label.setText(_PHASE_LABELS[PHASE_IDLE])
            self._ramp_chart.set_phase(PHASE_IDLE)
        self.connection_changed.emit(connected)

    # ---- ramp mode / sweep bounds ------------------------------------------------

    def _on_ramp_mode_changed(self, _index: int) -> None:
        mode = self._ramp_mode_combo.currentData()
        save_str(_RAMP_MODE_KEY, mode)
        self._set_ramp_mode_requested.emit(mode)
        self.ramp_settings_changed.emit(*self._current_ramp_settings_tuple())

    def _on_test_mode_changed(self, _index: int) -> None:
        mode = self._test_mode_combo.currentData()
        save_str(_TEST_MODE_KEY, mode)
        self._set_test_mode_requested.emit(mode)
        self.ramp_settings_changed.emit(*self._current_ramp_settings_tuple())

    def _on_duration_changed(self, _value: float) -> None:
        save_float(_UP_DURATION_KEY, self._up_duration_stepper.value())
        save_float(_HOLD_DURATION_KEY, self._hold_duration_stepper.value())
        save_float(_DOWN_DURATION_KEY, self._down_duration_stepper.value())
        self._ramp_chart.set_durations(
            self._up_duration_stepper.value(), self._hold_duration_stepper.value(), self._down_duration_stepper.value()
        )
        self.ramp_settings_changed.emit(*self._current_ramp_settings_tuple())

    def _apply_durations(self, up_s: float, hold_s: float, down_s: float) -> None:
        """Set all three steppers at once with their own valueChanged
        signals blocked, then save/push once - setting them one at a time
        WITHOUT blocking would fire _on_duration_changed() after the
        first stepper alone, which reads the other two steppers' still-
        not-yet-updated values and saves them prematurely, corrupting
        settings_store with a stale intermediate value (confirmed: this
        actually happened during development - restoring up_duration
        first caused hold/down's then-current constructor defaults to get
        saved over whatever was really in settings_store)."""
        for stepper, value in (
            (self._up_duration_stepper, up_s),
            (self._hold_duration_stepper, hold_s),
            (self._down_duration_stepper, down_s),
        ):
            stepper.blockSignals(True)
            stepper.setValue(round(value, 1))
            stepper.blockSignals(False)
        save_float(_UP_DURATION_KEY, self._up_duration_stepper.value())
        save_float(_HOLD_DURATION_KEY, self._hold_duration_stepper.value())
        save_float(_DOWN_DURATION_KEY, self._down_duration_stepper.value())
        self._ramp_chart.set_durations(
            self._up_duration_stepper.value(), self._hold_duration_stepper.value(), self._down_duration_stepper.value()
        )

    def _on_chart_profile_changed(
        self, up_s: float, hold_s: float, down_s: float, min_percent: float, max_percent: float
    ) -> None:
        # Dragging a chart point moves BOTH a duration and a bound at once
        # (see ramp_chart_widget.py's module docstring) - reflect the new
        # durations back into the steppers, snapped to one decimal (tenths
        # of a second, matching the steppers' own precision), and push the
        # new bounds to the manager, same as before this feature.
        self._apply_durations(up_s, hold_s, down_s)
        save_int(_MIN_BOUND_KEY, round(min_percent))
        save_int(_MAX_BOUND_KEY, round(max_percent))
        self._set_speed_bounds_requested.emit(min_percent, max_percent)
        self.ramp_settings_changed.emit(*self._current_ramp_settings_tuple())

    # ---- start / stop ------------------------------------------------

    def _on_start_clicked(self) -> None:
        self._start_btn.setEnabled(False)
        self._on_log_message("info", "Перевірка стану FC (має бути disarmed)...")
        self._check_disarmed_requested.emit()

    def _on_armed_check_result(self, disarmed_confirmed: bool, detail: str) -> None:
        if disarmed_confirmed:
            self._start_sweep_requested.emit(
                self._up_duration_stepper.value(),
                self._hold_duration_stepper.value(),
                self._down_duration_stepper.value(),
            )
        else:
            self._on_log_message("error", f"Старт заблоковано: {detail or 'не вдалось підтвердити стан disarmed'}")
            self._start_btn.setEnabled(self._connected)

    def _on_stop_clicked(self) -> None:
        self._stop_sweep_requested.emit()

    # ---- motor protocol ------------------------------------------------

    def _on_protocol_detected(self, index: int, name: str) -> None:
        self._protocol_known = index >= 0
        self._protocol_combo.setEnabled(self._protocol_known and self._connected)
        self._apply_protocol_btn.setEnabled(self._protocol_known and self._connected)
        if self._protocol_known:
            idx = self._protocol_combo.findText(name)
            if idx >= 0:
                self._protocol_combo.setCurrentIndex(idx)

    def _on_apply_protocol_clicked(self) -> None:
        name = self._protocol_combo.currentText()
        if not name:
            return
        reply = QMessageBox.question(
            self,
            "Зміна протоколу мотора",
            f"Змінити протокол на {name}?\n\n"
            "FC збереже це в EEPROM і одразу перезавантажиться - "
            "з'єднання розірветься, після перезавантаження підключіться знову.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._protocol_combo.setEnabled(False)
        self._apply_protocol_btn.setEnabled(False)
        self._on_log_message("info", f"Зміна протоколу на {name}...")
        self._set_protocol_requested.emit(MOTOR_PROTOCOLS.index(name))

    def _on_protocol_change_result(self, success: bool, detail: str) -> None:
        # On success the manager has already emitted connection_state_changed(False)
        # /motor_count_detected(0)/protocol_detected(-1, "") - this only surfaces the message.
        self._on_log_message("success" if success else "error", detail)
        if not success:
            self._protocol_combo.setEnabled(self._protocol_known and self._connected)
            self._apply_protocol_btn.setEnabled(self._protocol_known and self._connected)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt override
        # isVisible() is False whenever this tab isn't the active one
        # (QTabWidget hides inactive pages) or the window itself is hidden -
        # so this only fires while the operator is actually looking at this
        # tab, matching the "app-focused, no OS-global-hotkey" panic stop.
        if event.type() == QEvent.Type.KeyPress and self._connected and self.isVisible():
            self._stop_sweep_requested.emit()
            return True
        return super().eventFilter(obj, event)

    # ---- manager signal handlers ------------------------------------------------

    def _on_phase_changed(self, phase: str) -> None:
        self._phase_label.setText(_PHASE_LABELS.get(phase, phase))
        self._ramp_chart.set_phase(phase)
        if phase == PHASE_IDLE:
            self._start_btn.setEnabled(self._connected)
            self._stop_btn.setEnabled(False)
            self._protocol_combo.setEnabled(self._protocol_known and self._connected)
            self._apply_protocol_btn.setEnabled(self._protocol_known and self._connected)
        else:
            self._start_btn.setEnabled(False)
            self._stop_btn.setEnabled(True)
            self._protocol_combo.setEnabled(False)
            self._apply_protocol_btn.setEnabled(False)

    def _on_value_changed(self, values: list) -> None:
        # values can legitimately be empty (motor_count == 0, e.g. settling
        # a connection to an FC that reports no motors on disconnect) -
        # nothing to show on the master indicator either in that case.
        if not values:
            return
        pct = msp_to_percent(values[0])
        self._master_progress.setValue(round(pct))
        self._master_pct_label.setText(f"{pct:.0f}%")
        self._ramp_chart.set_current_value(values[0])
        for slider, label, v in zip(self._motor_sliders, self._motor_labels, values):
            slider.setValue(v)
            label.setText(f"{msp_to_percent(v):.0f}%")

    def _on_log_message(self, level: str, text: str) -> None:
        self._status_label.setStyleSheet(f"color: {_STATUS_COLORS.get(level, TEXT_MUTED)};")
        self._status_label.setText(text)

    # ---- lifecycle ------------------------------------------------

    def shutdown(self) -> None:
        """Stop this tab's private connection/thread - call on app close."""
        QApplication.instance().removeEventFilter(self)
        if self._connected:
            self.manager.disconnect()
        self._thread.quit()
        self._thread.wait(2000)
