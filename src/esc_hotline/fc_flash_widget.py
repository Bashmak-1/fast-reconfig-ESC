""""Прошивка FC" tab: auto-detects a DFU-mode flight controller, flashes
its firmware via the external STM32CubeProgrammer CLI, verifies the write,
resets it into normal run mode, then watches and flashes one or more
operator-chosen ESC outputs at once (checkable "ESC" menu, defaults to
ESC 1+2 - the common case is a 2-in-1 ESC board wired to two outputs on one
FC) - the same spirit as "Хотспот (лінія)" uses for the ESC side, but
generalized to a small chosen set instead of exactly one slot (see
fc_flash_station.py's docstring for the full two-phase design and why it's
a separate implementation rather than a shared one).

Driven by its own private connections (FcFlashStationController -> its own
ConnectionWorker/EscManager/thread for the ESC phase, plus its own DFU
worker thread for the CLI calls) - nothing shared with the Main/Config/
Хотспот tabs except the usual cross-tab port-busy check.

The motor-protocol dropdown here is informational/persisted only in this
first version, not wired to write anything to the FC - see
fc_flash_station.py's module docstring for why.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .eeprom_config import ConfigFileError, load_config_file
from .esc_manager import MAX_ESC_COUNT
from .fc_flash_station import (
    MAX_CONNECT_ATTEMPTS,
    PHASE_LABEL,
    PHASE_STYLE,
    RETRY_PHASES,
    FcFlashStationController,
)
from .manual_counter_widget import ManualCounterWidget
from .motor_ramp import MOTOR_PROTOCOLS
from .serial_transport import list_fc_ports
from .settings_store import load_int, load_str, save_int, save_str
from .theme import ACCENT_AMBER, ACCENT_ERROR, TEXT_MUTED

_FIRMWARE_PATH_KEY = "fc_flash_firmware_path"
_CONFIG_PATH_KEY = "fc_flash_esc_config_path"
_PORT_KEY = "fc_flash_port"
_ESC_INDICES_KEY = "fc_flash_esc_indices"  # comma-separated 0-based indices, e.g. "0,1"
_PROTOCOL_INDEX_KEY = "fc_flash_protocol_index"
_CLI_PATH_OVERRIDE_KEY = "fc_flash_cli_path_override"
_DEFAULT_ESC_INDICES = (0, 1)  # ESC 1 + ESC 2 - the common case is a 2-in-1 ESC board on one FC

_FIRMWARE_FILTER = "Firmware (*.hex *.bin);;All files (*)"
_CONFIG_FILTER = "Binary config (*.bin);;All files (*)"


class FcFlashPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.controller = FcFlashStationController(self)
        self._config_bytes: Optional[bytes] = None
        self._config_path: str = ""
        self._firmware_path: str = ""
        self._port_busy_checker: Optional[Callable[[str], bool]] = None
        self._last_flashed = 0
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

        self._restore_settings()

    # ---- construction ------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        firmware_row = QHBoxLayout()
        self._load_firmware_btn = QPushButton("Прошивка FC...")
        self._load_firmware_btn.clicked.connect(self._on_load_firmware_clicked)
        firmware_row.addWidget(self._load_firmware_btn)
        self._firmware_label = QLabel("Файл прошивки FC не обрано.")
        self._firmware_label.setWordWrap(True)
        self._firmware_label.setMaximumWidth(700)
        firmware_row.addWidget(self._firmware_label, 1)
        root.addLayout(firmware_row)

        config_row = QHBoxLayout()
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

        self._countdown_label = QLabel()
        self._countdown_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._countdown_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._countdown_label)

        self._retry_btn = QPushButton("Повторити")
        self._retry_btn.clicked.connect(self.controller.retry)
        self._retry_btn.setVisible(False)
        root.addWidget(self._retry_btn)

        self._dfu_detail_label = QLabel()
        self._dfu_detail_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._dfu_detail_label.setWordWrap(True)
        root.addWidget(self._dfu_detail_label)

        self._detail_label = QLabel()
        self._detail_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._detail_label.setWordWrap(True)
        root.addWidget(self._detail_label)

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
        self._manual_counter_label = QLabel("Прошито вручну:")
        self._manual_counter_label.setStyleSheet(f"color: {TEXT_MUTED};")
        counters_row.addWidget(self._manual_counter_label)
        self._manual_counter = ManualCounterWidget()
        counters_row.addWidget(self._manual_counter)
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

    def _restore_settings(self) -> None:
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

        protocol_index = load_int(_PROTOCOL_INDEX_KEY, 0)
        if 0 <= protocol_index < self._protocol_combo.count():
            self._protocol_combo.setCurrentIndex(protocol_index)

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

    def _on_phase_changed(self, _phase: str) -> None:
        self._render_banner()
        self._update_start_stop_button()
        self._update_countdown()

    def _on_esc_detail_changed(self, index: int, state: str, detail: str) -> None:
        self._esc_detail_by_index[index] = f"ESC {index + 1}: {detail}" if detail else f"ESC {index + 1}: -"
        lines = [self._esc_detail_by_index[i] for i in sorted(self._esc_detail_by_index)]
        self._detail_label.setText("\n".join(lines))

    def _on_dfu_detail_changed(self, detail: str) -> None:
        self._dfu_detail_label.setText(detail)

    def _on_power_cycle_hint_changed(self, show: bool) -> None:
        self._power_cycle_hint_shown = show
        self._power_cycle_hint_label.setText("Можливо варто перепідключити ESC по живленню." if show else "")
        self._power_cycle_hint_label.setVisible(show)

    def _on_armed_changed(self, armed: bool) -> None:
        self._load_firmware_btn.setEnabled(not armed)
        self._load_config_btn.setEnabled(not armed)
        self._port_combo.setEnabled(not armed)
        self._refresh_btn.setEnabled(not armed)
        self._esc_menu_btn.setEnabled(not armed)
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
        self._countdown_label.setText(f"Наступна перевірка через {remaining_ms / 1000:.1f}с")

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
            busy_phase = self.controller.phase in ("flashing_fc", "resetting_fc", "flashing")
            self._start_stop_btn.setEnabled(not busy_phase)
            self._set_start_hint("Неможливо зупинити під час прошивки." if busy_phase else "")
            return

        self._start_stop_btn.setText("Старт")
        if self._config_bytes is None:
            self._start_stop_btn.setEnabled(False)
            self._set_start_hint("Оберіть файл конфігу ESC.")
        elif not self._selected_esc_indices():
            self._start_stop_btn.setEnabled(False)
            self._set_start_hint("Оберіть хоча б один ESC-слот.")
        elif not self._selected_port_device():
            self._start_stop_btn.setEnabled(False)
            self._set_start_hint("Оберіть порт (для звичайного режиму FC).")
        elif self._port_busy_checker and self._port_busy_checker(self._selected_port_device()):
            self._start_stop_btn.setEnabled(False)
            self._set_start_hint("Порт вже використовується іншою вкладкою.")
        else:
            self._start_stop_btn.setEnabled(True)
            self._set_start_hint("" if self._firmware_path else "Файл прошивки FC не обрано - DFU-крок буде пропущено.")

    def _set_start_hint(self, text: str) -> None:
        self._start_stop_btn.setToolTip(text)
        self._start_hint_label.setText(text)
        self._start_hint_label.setVisible(bool(text))

    def _update_counters_label(self) -> None:
        flashed, ok, errors = self.controller.counters()
        self._counters_label.setText(f"Прошито: {flashed} | Вже коректно: {ok} | Помилок: {errors}")

    def _on_counters_changed(self, flashed: int, ok: int, errors: int) -> None:
        if flashed > self._last_flashed:
            self._manual_counter.increment(flashed - self._last_flashed)
        self._last_flashed = flashed
        self._update_counters_label()

    def _on_reset_counters_clicked(self) -> None:
        self.controller.reset_counters()
        self._manual_counter.reset()
