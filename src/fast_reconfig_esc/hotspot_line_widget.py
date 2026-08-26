"""A second, independent "Хотспот" tab, deliberately DIFFERENT from
hotspot_widget.py's "Хотспот": one port picker, one ESC-slot picker
("ESC 1".."ESC 12"), one Старт/Стоп button, watching and flashing exactly
ONE operator-chosen ESC output forever - see hotspot_station.py's docstring
for the full design rationale of that workflow. Driven by its own private
connection (HotspotStationController -> its own ConnectionWorker/
EscManager/thread) instead of the shared EscManager the Main/Config/
Хотспот tabs use.

Exists because the user explicitly wanted a second, separate "Хотспот" tab
with this different single-slot workflow, not a change to the first one's
full-sweep behavior - see docs/PROTOCOL_NOTES.md 2026-08-26 (an earlier
version of this tab supported multiple simultaneous stations with
add/remove buttons, then briefly the single-slot redesign was mistakenly
also applied to hotspot_widget.py itself - both corrected the same day).

Because the connection here is fully private, there is nothing shared with
the Main/Config/Хотспот tabs to protect - no mutual-exclusion wiring is
needed, and this tab's own config picker never needs to sync with
EscManager._loaded_config_bytes elsewhere (HotspotStationController.set_config
already keeps its own private EscManager's classification bytes and the
flashed bytes as one and the same call, so the "flashed ESC shows as
different on the next round" bug class from 2026-08-25 cannot occur here).

Port, ESC slot, and config path are all remembered independently of every
other tab (settings_store.py's hotspot_line_* keys) - this tab's whole
point is to be usable on a different bench, watching a different slot,
with a different config, than the first "Хотспот" tab.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import i18n
from .eeprom_config import ConfigFileError, load_config_file
from .esc_manager import MAX_ESC_COUNT
from .hotspot_station import HotspotStationController, PHASE_BANNER_KEY, PHASE_STYLE, RETRY_PHASES
from .serial_transport import list_fc_ports
from .settings_store import load_int, load_str, save_int, save_str
from .theme import ACCENT_AMBER, ACCENT_ERROR, TEXT_MUTED

_PORT_KEY = "hotspot_line_port"
_ESC_INDEX_KEY = "hotspot_line_esc_index"
_CONFIG_PATH_KEY = "hotspot_line_config_path"


class HotspotLinePage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.controller = HotspotStationController(self)
        self._config_bytes: Optional[bytes] = None
        self._config_path: str = ""
        self._port_busy_checker: Optional[Callable[[str], bool]] = None

        self._build_ui()

        self.controller.phase_changed.connect(self._on_phase_changed)
        self.controller.esc_detail_changed.connect(self._on_esc_detail_changed)
        self.controller.counters_changed.connect(lambda *_: self._update_counters_label())
        self.controller.armed_changed.connect(self._on_armed_changed)

        i18n.signals.language_changed.connect(self._retranslate)
        self._restore_last_config()
        self._restore_esc_index()
        self._retranslate()

    # ---- construction ------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        top = QHBoxLayout()
        self._load_config_btn = QPushButton()
        self._load_config_btn.clicked.connect(self._on_load_config_clicked)
        top.addWidget(self._load_config_btn)
        self._config_label = QLabel()
        self._config_label.setWordWrap(True)
        self._config_label.setMaximumWidth(700)
        top.addWidget(self._config_label, 1)
        root.addLayout(top)

        port_row = QHBoxLayout()
        self._port_label = QLabel()
        port_row.addWidget(self._port_label)
        self._port_combo = QComboBox()
        self._port_combo.setMinimumWidth(220)
        self._port_combo.currentIndexChanged.connect(self._on_port_changed)
        port_row.addWidget(self._port_combo)
        self._refresh_btn = QPushButton("↻")
        self._refresh_btn.setFixedSize(36, 30)
        self._refresh_btn.setStyleSheet("font-size: 16px; padding: 0px;")
        self._refresh_btn.clicked.connect(self._refresh_ports)
        port_row.addWidget(self._refresh_btn)
        self._esc_label = QLabel()
        port_row.addWidget(self._esc_label)
        self._esc_combo = QComboBox()
        for i in range(MAX_ESC_COUNT):
            self._esc_combo.addItem(f"ESC {i + 1}", i)
        self._esc_combo.currentIndexChanged.connect(self._on_esc_index_changed)
        port_row.addWidget(self._esc_combo)
        port_row.addStretch(1)
        self._start_stop_btn = QPushButton()
        self._start_stop_btn.setObjectName("connectButton")
        self._start_stop_btn.setMinimumWidth(120)
        self._start_stop_btn.clicked.connect(self._on_start_stop_clicked)
        port_row.addWidget(self._start_stop_btn)
        root.addLayout(port_row)

        self._start_hint_label = QLabel()
        self._start_hint_label.setStyleSheet(f"color: {ACCENT_AMBER};")
        self._start_hint_label.setVisible(False)
        root.addWidget(self._start_hint_label)

        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setMinimumHeight(90)
        self._banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._banner)

        self._retry_btn = QPushButton()
        self._retry_btn.clicked.connect(self.controller.retry)
        self._retry_btn.setVisible(False)
        root.addWidget(self._retry_btn)

        self._detail_label = QLabel()
        self._detail_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._detail_label.setWordWrap(True)
        root.addWidget(self._detail_label)
        root.addStretch(1)

        counters_row = QHBoxLayout()
        self._counters_label = QLabel()
        self._counters_label.setStyleSheet(f"color: {TEXT_MUTED};")
        counters_row.addWidget(self._counters_label, 1)
        self._reset_counters_btn = QPushButton()
        self._reset_counters_btn.clicked.connect(self.controller.reset_counters)
        counters_row.addWidget(self._reset_counters_btn)
        root.addLayout(counters_row)

        self._restore_port()

    # ---- i18n ------------------------------------------------

    def _retranslate(self, *_args) -> None:
        self._load_config_btn.setText(i18n.tr("hotspot_load_config"))
        if self._config_bytes is None:
            self._config_label.setText(i18n.tr("hotspot_no_config"))
        else:
            self._config_label.setText(i18n.tr("hotspot_config_loaded", path=self._config_path))
        self._port_label.setText(i18n.tr("hotspot_port_label"))
        self._refresh_btn.setToolTip(i18n.tr("refresh_tooltip"))
        self._esc_label.setText(i18n.tr("hotspot_esc_label"))
        self._retry_btn.setText(i18n.tr("hotspot_retry"))
        self._reset_counters_btn.setText(i18n.tr("hotspot_reset_counters"))
        self._update_counters_label()
        self._render_banner()
        self._update_start_stop_button()

    # ---- config / port / ESC-slot pickers ------------------------------------------------

    def _on_load_config_clicked(self) -> None:
        downloads = str(Path.home() / "Downloads")
        path, _ = QFileDialog.getOpenFileName(
            self, i18n.tr("hotspot_load_config"), downloads, i18n.tr("binary_config_filter")
        )
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

    def _restore_last_config(self) -> None:
        path = load_str(_CONFIG_PATH_KEY)
        if not path or not Path(path).is_file():
            return
        try:
            config_bytes = load_config_file(path)
        except ConfigFileError:
            return
        self._apply_config(path, config_bytes)

    def _apply_config(self, path: str, config_bytes: bytes) -> None:
        self._config_path = path
        self._config_bytes = config_bytes
        self._config_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._config_label.setText(i18n.tr("hotspot_config_loaded", path=path))
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

    def _restore_port(self) -> None:
        self._refresh_ports()
        remembered = load_str(_PORT_KEY)
        if remembered:
            idx = self._port_combo.findData(remembered)
            if idx >= 0:
                self._port_combo.setCurrentIndex(idx)
                self.controller.set_port(self._selected_port_device())

    def _restore_esc_index(self) -> None:
        index = load_int(_ESC_INDEX_KEY, 0)
        idx = self._esc_combo.findData(index)
        if idx >= 0:
            self._esc_combo.setCurrentIndex(idx)
        self.controller.set_esc_index(self._selected_esc_index())

    def _selected_port_device(self) -> Optional[str]:
        data = self._port_combo.currentData()
        return str(data) if data else None

    def _selected_esc_index(self) -> int:
        data = self._esc_combo.currentData()
        return int(data) if data is not None else 0

    def _on_port_changed(self, *_args) -> None:
        port = self._selected_port_device()
        self.controller.set_port(port)
        if port:
            save_str(_PORT_KEY, port)
        self._update_start_stop_button()

    def _on_esc_index_changed(self, *_args) -> None:
        index = self._selected_esc_index()
        self.controller.set_esc_index(index)
        save_int(_ESC_INDEX_KEY, index)

    # ---- start/stop ------------------------------------------------

    def set_port_busy_checker(self, checker: Callable[[str], bool]) -> None:
        """MainWindow injects this so arming can refuse a port already
        claimed by the other Хотспот tab or a manual connection. Confirmed
        on real hardware: two connections fighting over the same port
        doesn't just fail cleanly - it can make the WINNING side's ESC
        probes fail too, which looks exactly like "not detecting" a
        correctly-wired ESC. Not just cosmetic - genuinely avoid opening a
        second connection to an already-claimed port at all."""
        self._port_busy_checker = checker

    def recheck_availability(self) -> None:
        """Call when some OTHER connection surface's armed/connected state
        changes, so this tab's Старт button reflects port availability
        without waiting for the operator to touch this tab first."""
        if not self.controller.armed:
            self._update_start_stop_button()

    def _on_start_stop_clicked(self) -> None:
        if self.controller.armed:
            self.controller.stop()
            return
        port = self._selected_port_device()
        if port and self._port_busy_checker and self._port_busy_checker(port):
            self._set_start_hint(i18n.tr("hotspot_port_busy_elsewhere"))
            return
        self.controller.start()

    def shutdown(self) -> None:
        """Stop this tab's private connection/thread - call on app close."""
        self.controller.shutdown()

    # ---- rendering ------------------------------------------------

    def _on_phase_changed(self, _phase: str) -> None:
        self._render_banner()
        self._update_start_stop_button()

    def _on_esc_detail_changed(self, state: str, detail: str) -> None:
        self._detail_label.setText(detail)

    def _on_armed_changed(self, armed: bool) -> None:
        self._load_config_btn.setEnabled(not armed)
        self._port_combo.setEnabled(not armed)
        self._refresh_btn.setEnabled(not armed)
        self._esc_combo.setEnabled(not armed)
        if not armed:
            self._detail_label.setText("")
        self._update_start_stop_button()

    def _render_banner(self) -> None:
        bg, fg = PHASE_STYLE[self.controller.phase]
        self._banner.setStyleSheet(
            f"background-color: {bg}; color: {fg}; font-size: 22px; font-weight: 700;"
            "padding: 24px; border-radius: 8px;"
        )
        self._banner.setText(i18n.tr(PHASE_BANNER_KEY[self.controller.phase]))
        self._retry_btn.setVisible(self.controller.phase in RETRY_PHASES)

    def _update_start_stop_button(self) -> None:
        if self.controller.armed:
            self._start_stop_btn.setText(i18n.tr("hotspot_stop"))
            flashing = self.controller.phase == "flashing"
            self._start_stop_btn.setEnabled(not flashing)
            self._set_start_hint(i18n.tr("hotspot_stop_disabled_flashing") if flashing else "")
            return

        self._start_stop_btn.setText(i18n.tr("hotspot_start"))
        if self._config_bytes is None:
            self._start_stop_btn.setEnabled(False)
            self._set_start_hint(i18n.tr("hotspot_start_disabled_no_config"))
        elif not self._selected_port_device():
            self._start_stop_btn.setEnabled(False)
            self._set_start_hint(i18n.tr("hotspot_start_disabled_no_port"))
        elif self._port_busy_checker and self._port_busy_checker(self._selected_port_device()):
            self._start_stop_btn.setEnabled(False)
            self._set_start_hint(i18n.tr("hotspot_port_busy_elsewhere"))
        else:
            self._start_stop_btn.setEnabled(True)
            self._set_start_hint("")

    def _set_start_hint(self, text: str) -> None:
        self._start_stop_btn.setToolTip(text)
        self._start_hint_label.setText(text)
        self._start_hint_label.setVisible(bool(text))

    def _update_counters_label(self) -> None:
        flashed, ok, errors = self.controller.counters()
        self._counters_label.setText(i18n.tr("hotspot_counters", flashed=flashed, ok=ok, errors=errors))
