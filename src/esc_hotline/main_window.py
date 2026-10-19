"""Main application window: top bar, config picker, ESC status grid, log panel."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QSize, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import i18n
from .config_editor_widget import ConfigEditorPage
from .connection_worker import ConnectionWorker
from .esc_comparison_widget import EscComparisonList
from .esc_manager import (
    DEFAULT_ESC_CONNECT_RETRIES,
    MAX_ESC_CONNECT_RETRIES,
    MIN_ESC_CONNECT_RETRIES,
)
from .esc_status_widget import EscStatusWidget
from .fc_flash_widget import FcFlashPage
from .file_logger import LOG_FILE
from .flag_icons import FLAG_ICON_BUILDERS
from .hotspot_line_widget import HotspotLinePage
from .hotspot_widget import HotspotFlashPage
from .motor_test_widget import MotorTestPage
from .serial_transport import list_fc_ports
from .settings_store import load_last_config_path, save_last_config_path
from .theme import ACCENT_AMBER, ACCENT_ERROR, ACCENT_GREEN, TEXT_MUTED

BAUD_RATES = [1000000, 500000, 256000, 115200, 57600, 38400, 19200, 14400, 9600]
GRID_COLUMNS = 4

_LOG_COLORS = {
    "info": TEXT_MUTED,
    "warning": ACCENT_AMBER,
    "error": ACCENT_ERROR,
    "success": ACCENT_GREEN,
}


class MainWindow(QMainWindow):
    # GUI -> worker thread. Connecting these to EscManager's plain methods is
    # enough to get a queued cross-thread call: Qt looks at the RECEIVER's
    # thread affinity (the EscManager lives on the worker QThread), not the
    # sender's, so this dispatch is automatically queued onto its event loop.
    request_connect = pyqtSignal(str, int)
    request_disconnect = pyqtSignal()
    request_discover = pyqtSignal()
    request_flash = pyqtSignal(str, object)  # config_path, target_indices (list | None)
    request_flash_bytes = pyqtSignal(bytes, object)  # config_bytes, target_indices (list | None)
    request_set_esc_retries = pyqtSignal(int)
    request_set_config_path = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.resize(1280, 800)

        self.worker = ConnectionWorker()
        self.esc_manager = self.worker.manager

        self._esc_widgets: dict[int, EscStatusWidget] = {}
        self._esc_read_cache: dict[int, tuple[str, bytes]] = {}  # index -> (label, settings), for "Read from ESC"
        self._connected = False
        self._hotspot_armed = False

        self._build_ui()
        self._wire_signals()
        self._refresh_ports()
        self._retranslate_ui()
        self._append_log("info", i18n.tr("log_file_location", path=str(LOG_FILE)))
        self._restore_last_config_path()
        i18n.signals.language_changed.connect(self._retranslate_ui)

    # ---- UI construction ------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        root.addWidget(self._build_top_bar())

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_main_tab(), "")
        self._tabs.addTab(self._build_config_tab(), "")
        self._tabs.addTab(self._build_hotspot_tab(), "")
        self._tabs.addTab(self._build_hotspot_line_tab(), "")
        # Not retranslated in _retranslate_ui like the other four tab titles -
        # this tab's strings are plain Ukrainian literals throughout (see
        # motor_test_widget.py's own docstring), matching the same
        # not-worth-i18n-upkeep call made for the (separate, shelved) UID Lab tab.
        self._tabs.addTab(self._build_motor_test_tab(), "Прогон мотором")
        # Also plain-Ukrainian-literal, not retranslated - same rationale
        # as the "Прогон мотором" tab above.
        self._tabs.addTab(self._build_fc_flash_tab(), "Прошивка FC")
        root.addWidget(self._tabs, 1)

        root.addWidget(self._build_log_panel())

    def _build_main_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self._build_config_row())

        self._grid_container = QFrame()
        self._grid_layout = QGridLayout(self._grid_container)
        layout.addWidget(self._grid_container, 1)
        return page

    def _build_config_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        self.config_editor = ConfigEditorPage()
        self._comparison_list = EscComparisonList()
        self._comparison_list.setMaximumWidth(300)
        layout.addWidget(self.config_editor, 1)
        layout.addWidget(self._comparison_list)
        return page

    def _build_hotspot_tab(self) -> QWidget:
        self.hotspot_page = HotspotFlashPage()
        return self.hotspot_page

    def _build_hotspot_line_tab(self) -> QWidget:
        self.hotspot_line_page = HotspotLinePage()
        return self.hotspot_line_page

    def _build_motor_test_tab(self) -> QWidget:
        self.motor_test_page = MotorTestPage()
        return self.motor_test_page

    def _build_fc_flash_tab(self) -> QWidget:
        self.fc_flash_page = FcFlashPage()
        return self.fc_flash_page

    def _build_top_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("topBar")
        layout = QHBoxLayout(bar)

        logo = QLabel("ESC HOTLINE")
        logo.setStyleSheet("font-weight: 700; font-size: 15px; color: #c0392b;")
        layout.addWidget(logo)
        layout.addStretch(1)

        self._port_label = QLabel()
        layout.addWidget(self._port_label)
        self._port_combo = QComboBox()
        self._port_combo.setMinimumWidth(220)
        layout.addWidget(self._port_combo)

        refresh_btn = QPushButton("↻")
        refresh_btn.setObjectName("refreshButton")
        refresh_btn.setFixedSize(36, 30)
        refresh_btn.setStyleSheet("font-size: 16px; padding: 0px;")
        refresh_btn.clicked.connect(self._refresh_ports)
        self._refresh_btn = refresh_btn
        layout.addWidget(refresh_btn)

        self._baud_label = QLabel()
        layout.addWidget(self._baud_label)
        self._baud_combo = QComboBox()
        for rate in BAUD_RATES:
            self._baud_combo.addItem(str(rate))
        self._baud_combo.setCurrentText("115200")
        layout.addWidget(self._baud_combo)

        self._connect_toggle_btn = QPushButton()
        self._connect_toggle_btn.setObjectName("connectButton")
        self._connect_toggle_btn.setMinimumWidth(110)
        self._connect_toggle_btn.clicked.connect(self._on_connect_toggle_clicked)
        layout.addWidget(self._connect_toggle_btn)

        self._lang_combo = QComboBox()
        self._lang_combo.setIconSize(QSize(22, 15))
        for code, _flag, label, _name in i18n.LANGUAGES:
            icon = FLAG_ICON_BUILDERS[code]()
            self._lang_combo.addItem(icon, label, code)
        current_idx = [c for c, *_ in i18n.LANGUAGES].index(i18n.current_language())
        self._lang_combo.setCurrentIndex(current_idx)
        self._lang_combo.setFixedWidth(96)
        self._lang_combo.currentIndexChanged.connect(self._on_language_selected)
        layout.addWidget(self._lang_combo)

        return bar

    def _build_config_row(self) -> QFrame:
        row = QFrame()
        row.setObjectName("configRow")
        layout = QHBoxLayout(row)

        self._config_label = QLabel()
        layout.addWidget(self._config_label)
        self._config_path_edit = QLineEdit()
        self._config_path_edit.setReadOnly(True)
        layout.addWidget(self._config_path_edit, 1)

        self._browse_btn = QPushButton()
        self._browse_btn.clicked.connect(self._on_browse_clicked)
        layout.addWidget(self._browse_btn)

        self._auto_flash_check = QCheckBox()
        layout.addWidget(self._auto_flash_check)

        self._esc_retries_label = QLabel()
        layout.addWidget(self._esc_retries_label)
        self._retries_spin = QSpinBox()
        self._retries_spin.setRange(MIN_ESC_CONNECT_RETRIES, MAX_ESC_CONNECT_RETRIES)
        self._retries_spin.setValue(DEFAULT_ESC_CONNECT_RETRIES)
        self._retries_spin.valueChanged.connect(self.request_set_esc_retries.emit)
        layout.addWidget(self._retries_spin)

        self._rescan_btn = QPushButton()
        self._rescan_btn.setEnabled(False)
        self._rescan_btn.clicked.connect(self._on_rescan_clicked)
        layout.addWidget(self._rescan_btn)

        self._flash_all_btn = QPushButton()
        self._flash_all_btn.setEnabled(False)
        self._flash_all_btn.clicked.connect(self._on_flash_all_clicked)
        layout.addWidget(self._flash_all_btn)

        return row

    def _build_log_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("logPanel")
        layout = QVBoxLayout(panel)
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(2000)
        self._log_view.setStyleSheet("font-family: Consolas, monospace;")
        layout.addWidget(self._log_view)
        panel.setMaximumHeight(220)
        return panel

    # ---- signal wiring ------------------------------------------------

    def _wire_signals(self) -> None:
        self.request_connect.connect(self.esc_manager.connect)
        self.request_disconnect.connect(self.esc_manager.disconnect)
        self.request_discover.connect(self.esc_manager.discover_all)
        self.request_flash.connect(self.esc_manager.flash_all)
        self.request_flash_bytes.connect(self.esc_manager.flash_bytes)
        self.request_set_esc_retries.connect(self.esc_manager.set_esc_connect_retries)
        self.request_set_config_path.connect(self.esc_manager.set_config_path)

        self.esc_manager.connection_state_changed.connect(self._on_connection_state_changed)
        self.esc_manager.esc_status_changed.connect(self._on_esc_status_changed)
        self.esc_manager.log_message.connect(self._on_log_message)
        self.esc_manager.discovery_finished.connect(self._on_discovery_finished)
        self.esc_manager.flash_finished.connect(self._on_flash_finished)

        self.config_editor.apply_requested.connect(self._on_apply_from_editor)
        self.config_editor.file_loaded.connect(self._set_config_path)

        self.hotspot_page.connect_requested.connect(self.request_connect.emit)
        self.hotspot_page.disconnect_requested.connect(self.request_disconnect.emit)
        self.hotspot_page.discover_requested.connect(self.request_discover.emit)
        self.hotspot_page.flash_requested.connect(lambda data: self.request_flash_bytes.emit(data, None))
        self.hotspot_page.armed_changed.connect(self._on_hotspot_armed_changed)
        self.hotspot_page.config_path_changed.connect(self._set_config_path)

        # hotspot_line_page (HotspotLinePage) is NOT wired into the shared
        # request_*/EscManager signals above: it drives its own private
        # EscManager, so it has no shared connection/config state with
        # MainWindow to sync. It DOES still need to know about port
        # collisions with the shared connection and with hotspot_page,
        # though - two connections fighting over the same physical port
        # was confirmed on real hardware to make ESC probes fail even on
        # the side that "won" the port, not just a clean refusal.
        self.hotspot_page.set_port_busy_checker(self._is_port_busy_for_hotspot)
        self.hotspot_line_page.set_port_busy_checker(self._is_port_busy_for_hotspot_line)
        self.hotspot_page.armed_changed.connect(lambda *_: self.hotspot_line_page.recheck_availability())
        self.hotspot_line_page.controller.armed_changed.connect(lambda *_: self.hotspot_page.recheck_availability())

        # motor_test_page (MotorTestPage) is likewise NOT wired into the
        # shared request_*/EscManager signals - own private plain-MSP
        # connection, see its own docstring. Same cross-tab port-collision
        # guard as the two Хотспот tabs (real-hardware-confirmed risk).
        self.motor_test_page.set_port_busy_checker(self._is_port_busy_for_motor_test)
        self.motor_test_page.connection_changed.connect(lambda *_: self.hotspot_page.recheck_availability())
        self.motor_test_page.connection_changed.connect(lambda *_: self.hotspot_line_page.recheck_availability())
        self.hotspot_page.armed_changed.connect(lambda *_: self.motor_test_page.recheck_availability())
        self.hotspot_line_page.controller.armed_changed.connect(lambda *_: self.motor_test_page.recheck_availability())

        # fc_flash_page (FcFlashPage) is likewise NOT wired into the shared
        # request_*/EscManager signals - own private connections (own
        # EscManager for the ESC phase, own DFU worker), see
        # fc_flash_station.py's docstring. Same cross-tab port-collision
        # guard as every other private-connection tab above, wired
        # symmetrically with all three of them.
        self.fc_flash_page.set_port_busy_checker(self._is_port_busy_for_fc_flash)
        self.fc_flash_page.controller.armed_changed.connect(lambda *_: self.hotspot_page.recheck_availability())
        self.fc_flash_page.controller.armed_changed.connect(lambda *_: self.hotspot_line_page.recheck_availability())
        self.fc_flash_page.controller.armed_changed.connect(lambda *_: self.motor_test_page.recheck_availability())
        self.hotspot_page.armed_changed.connect(lambda *_: self.fc_flash_page.recheck_availability())
        self.hotspot_line_page.controller.armed_changed.connect(lambda *_: self.fc_flash_page.recheck_availability())
        self.motor_test_page.connection_changed.connect(lambda *_: self.fc_flash_page.recheck_availability())

        # Both tabs' ramp-test settings (durations, curve/test mode, speed
        # bounds) are the SAME shared settings_store keys, not two separate
        # configs - this keeps each tab's own chart/controls reflecting the
        # other's latest change live, not just after a restart.
        self.motor_test_page.ramp_settings_changed.connect(self.fc_flash_page.apply_external_ramp_settings)
        self.fc_flash_page.ramp_settings_changed.connect(self.motor_test_page.apply_external_ramp_settings)

    # ---- i18n ------------------------------------------------

    def _on_language_selected(self, index: int) -> None:
        code = self._lang_combo.itemData(index)
        if code:
            i18n.set_language(code)

    def _sync_lang_combo(self) -> None:
        idx = [c for c, *_ in i18n.LANGUAGES].index(i18n.current_language())
        if self._lang_combo.currentIndex() != idx:
            self._lang_combo.blockSignals(True)
            self._lang_combo.setCurrentIndex(idx)
            self._lang_combo.blockSignals(False)

    def _update_connect_toggle_button(self) -> None:
        if self._connected:
            self._connect_toggle_btn.setText(i18n.tr("disconnect"))
            self._connect_toggle_btn.setObjectName("")
        else:
            self._connect_toggle_btn.setText(i18n.tr("connect"))
            self._connect_toggle_btn.setObjectName("connectButton")
        # objectName-based QSS doesn't auto-refresh at runtime - force it to.
        self._connect_toggle_btn.style().unpolish(self._connect_toggle_btn)
        self._connect_toggle_btn.style().polish(self._connect_toggle_btn)

    def _retranslate_ui(self, *_args) -> None:
        self._sync_lang_combo()
        self.setWindowTitle(i18n.tr("window_title"))
        self._tabs.setTabText(0, i18n.tr("tab_main"))
        self._tabs.setTabText(1, i18n.tr("tab_config"))
        self._tabs.setTabText(2, i18n.tr("tab_hotspot"))
        self._tabs.setTabText(3, i18n.tr("tab_hotspot_multi"))
        self._port_label.setText(i18n.tr("port_label"))
        self._refresh_btn.setToolTip(i18n.tr("refresh_tooltip"))
        self._baud_label.setText(i18n.tr("baud_label"))
        self._update_connect_toggle_button()

        self._config_label.setText(i18n.tr("config_label"))
        self._config_path_edit.setPlaceholderText(i18n.tr("config_placeholder"))
        self._browse_btn.setText(i18n.tr("browse"))
        self._auto_flash_check.setText(i18n.tr("auto_flash_mode"))
        self._auto_flash_check.setToolTip(i18n.tr("auto_flash_tooltip"))
        self._esc_retries_label.setText(i18n.tr("esc_retries_label"))
        self._retries_spin.setToolTip(i18n.tr("esc_retries_tooltip"))
        self._rescan_btn.setText(i18n.tr("rescan"))
        self._flash_all_btn.setText(i18n.tr("flash_all"))

    # ---- port list ------------------------------------------------

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

    # ---- button handlers ------------------------------------------------

    def _on_connect_toggle_clicked(self) -> None:
        if self._connected:
            self._on_disconnect_clicked()
        else:
            self._on_connect_clicked()

    def _on_connect_clicked(self) -> None:
        port = self._selected_port()
        if not port:
            self._append_log("error", i18n.tr("no_port_selected"))
            return
        baud = int(self._baud_combo.currentText())
        self._connect_toggle_btn.setEnabled(False)
        self._append_log("info", i18n.tr("connecting", port=port, baud=baud))
        self.request_connect.emit(port, baud)

    def _on_disconnect_clicked(self) -> None:
        self._connect_toggle_btn.setEnabled(False)
        self.request_disconnect.emit()

    def _on_rescan_clicked(self) -> None:
        self._rescan_btn.setEnabled(False)
        self.request_discover.emit()

    def _on_flash_all_clicked(self) -> None:
        path = self._config_path_edit.text()
        if not path:
            self._append_log("error", i18n.tr("no_config_selected"))
            return
        self._flash_all_btn.setEnabled(False)
        self.request_flash.emit(path, None)

    def _on_browse_clicked(self) -> None:
        current = self._config_path_edit.text()
        start_dir = str(Path(current).parent) if current else str(Path.home() / "Downloads")
        path, _ = QFileDialog.getOpenFileName(
            self, i18n.tr("select_config_dialog_title"), start_dir, i18n.tr("binary_config_filter")
        )
        if path:
            self._set_config_path(path)

    def _set_config_path(self, path: str) -> None:
        self._config_path_edit.setText(path)
        self._flash_all_btn.setEnabled(self._connected)
        self.request_set_config_path.emit(path)
        save_last_config_path(path)

    def _restore_last_config_path(self) -> None:
        path = load_last_config_path()
        if path and Path(path).is_file():
            self._set_config_path(path)
        elif path:
            self._append_log("warning", i18n.tr("remembered_config_missing", path=path))

    # ---- worker signal handlers ------------------------------------------------

    def _on_connection_state_changed(self, connected: bool, expected_count: int) -> None:
        self._connected = connected
        self._connect_toggle_btn.setEnabled(not self._hotspot_armed)
        self._update_connect_toggle_button()
        self._rescan_btn.setEnabled(connected and not self._hotspot_armed)
        self._flash_all_btn.setEnabled(connected and bool(self._config_path_edit.text()) and not self._hotspot_armed)

        self._rebuild_grid(expected_count if connected else 0)
        self._comparison_list.clear_all(expected_count if connected else 0)
        self._esc_read_cache.clear()
        self.config_editor.set_available_escs(self._esc_read_cache)
        self.config_editor.set_apply_enabled(connected)
        self.hotspot_page.on_connection_state_changed(connected, expected_count)
        self.hotspot_line_page.recheck_availability()
        if connected:
            self.request_discover.emit()

    def _rebuild_grid(self, count: int) -> None:
        while self._grid_layout.count():
            item = self._grid_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._esc_widgets.clear()

        for i in range(count):
            widget = EscStatusWidget(i)
            self._esc_widgets[i] = widget
            self._grid_layout.addWidget(widget, i // GRID_COLUMNS, i % GRID_COLUMNS)

    def _on_esc_status_changed(self, index: int, state: str, detail: str, esc_info) -> None:
        widget = self._esc_widgets.get(index)
        if widget is not None:
            widget.set_state(state, detail, esc_info)
        self._comparison_list.set_row(index, state, detail)

        if esc_info is not None and esc_info.settings is not None:
            self._esc_read_cache[index] = (detail or esc_info.mcu_family, esc_info.settings)
            self.config_editor.set_available_escs(self._esc_read_cache)

        self.hotspot_page.on_esc_status_changed(index, state, detail, esc_info)

    def _on_log_message(self, level: str, text: str) -> None:
        self._append_log(level, text)

    def _append_log(self, level: str, text: str) -> None:
        color = _LOG_COLORS.get(level, "#e6e6e6")
        self._log_view.appendHtml(f'<span style="color:{color};">{text}</span>')

    def _on_discovery_finished(self, found: int, expected: int) -> None:
        self._rescan_btn.setEnabled(self._connected and not self._hotspot_armed)
        if self._hotspot_armed:
            pass  # the Hotspot tab drives its own flash decision below
        elif self._auto_flash_check.isChecked() and self._config_path_edit.text():
            self._flash_all_btn.setEnabled(False)
            self.request_flash.emit(self._config_path_edit.text(), None)
        else:
            self._flash_all_btn.setEnabled(self._connected and bool(self._config_path_edit.text()))
        self.hotspot_page.on_discovery_finished(found, expected)

    def _on_flash_finished(self, success: int, attempted: int) -> None:
        self._flash_all_btn.setEnabled(
            self._connected and bool(self._config_path_edit.text()) and not self._hotspot_armed
        )
        self.config_editor.set_apply_enabled(self._connected)
        self.hotspot_page.on_flash_finished(success, attempted)

    def _on_apply_from_editor(self, config_bytes: bytes) -> None:
        if not self._connected:
            self._append_log("error", i18n.tr("not_connected_log"))
            return
        self.config_editor.set_apply_enabled(False)
        self.request_flash_bytes.emit(config_bytes, None)

    def _on_hotspot_armed_changed(self, armed: bool) -> None:
        # Hotspot mode drives the single shared EscManager connection on its
        # own - manual controls elsewhere must stay out of its way while armed.
        self._hotspot_armed = armed
        tooltip = i18n.tr("hotspot_manual_disabled_hotspot_active") if armed else ""
        for widget in (self._port_combo, self._refresh_btn, self._baud_combo):
            widget.setEnabled(not armed)
            widget.setToolTip(tooltip)

        self._connect_toggle_btn.setEnabled(not armed)
        self._connect_toggle_btn.setToolTip(tooltip)
        self._rescan_btn.setEnabled(self._connected and not armed)
        self._rescan_btn.setToolTip(tooltip)
        self._flash_all_btn.setEnabled(self._connected and bool(self._config_path_edit.text()) and not armed)
        self._flash_all_btn.setToolTip(tooltip)

    # ---- cross-tab port-collision guard ------------------------------------------------
    #
    # Confirmed on real hardware (2026-08-26): letting two connections fight
    # over the same physical port doesn't just fail cleanly on the loser -
    # it can make the WINNING side's own ESC probes start failing too
    # (ACK_D_GENERAL_ERROR on an ESC that was responding fine moments
    # earlier), which looks exactly like "stopped detecting a correctly
    # wired ESC". hotspot_page shares MainWindow's EscManager and already
    # has proper mutual exclusion with the Main/Config tabs; hotspot_line_page
    # deliberately doesn't (private EscManager, meant to be usable alongside
    # everything else on a DIFFERENT port) - these checks are the one thing
    # that still needs to cross that boundary: refuse to arm on a port
    # something else already holds, rather than silently racing for it.

    def _is_port_busy_for_hotspot(self, port: str) -> bool:
        if self.hotspot_line_page.controller.armed and self.hotspot_line_page._selected_port_device() == port:
            return True
        if self._connected and not self._hotspot_armed and self._port_combo.currentData() == port:
            return True
        if self.motor_test_page.connected_port() == port:
            return True
        if self.fc_flash_page.controller.armed and self.fc_flash_page._selected_port_device() == port:
            return True
        return False

    def _is_port_busy_for_hotspot_line(self, port: str) -> bool:
        if self._hotspot_armed and self.hotspot_page._selected_port_device() == port:
            return True
        if self._connected and not self._hotspot_armed and self._port_combo.currentData() == port:
            return True
        if self.motor_test_page.connected_port() == port:
            return True
        if self.fc_flash_page.controller.armed and self.fc_flash_page._selected_port_device() == port:
            return True
        return False

    def _is_port_busy_for_motor_test(self, port: str) -> bool:
        if self._hotspot_armed and self.hotspot_page._selected_port_device() == port:
            return True
        if self.hotspot_line_page.controller.armed and self.hotspot_line_page._selected_port_device() == port:
            return True
        if self._connected and not self._hotspot_armed and self._port_combo.currentData() == port:
            return True
        if self.fc_flash_page.controller.armed and self.fc_flash_page._selected_port_device() == port:
            return True
        return False

    def _is_port_busy_for_fc_flash(self, port: str) -> bool:
        if self._hotspot_armed and self.hotspot_page._selected_port_device() == port:
            return True
        if self.hotspot_line_page.controller.armed and self.hotspot_line_page._selected_port_device() == port:
            return True
        if self._connected and not self._hotspot_armed and self._port_combo.currentData() == port:
            return True
        if self.motor_test_page.connected_port() == port:
            return True
        return False

    # ---- window lifecycle ------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.config_editor.has_unsaved_changes():
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle(i18n.tr("config_exit_unsaved_title"))
            box.setText(i18n.tr("config_exit_unsaved_body", diff=self.config_editor.diff_text()))
            save_btn = box.addButton(i18n.tr("dialog_save"), QMessageBox.ButtonRole.AcceptRole)
            discard_btn = box.addButton(i18n.tr("dialog_discard"), QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(i18n.tr("dialog_cancel"), QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(save_btn)
            box.exec()
            clicked = box.clickedButton()
            if clicked is save_btn:
                if not self.config_editor.save_via_dialog():
                    event.ignore()
                    return
            elif clicked is not discard_btn:
                event.ignore()
                return

        if self._connected:
            self.esc_manager.disconnect()
        self.worker.stop()
        self.hotspot_line_page.shutdown()
        self.motor_test_page.shutdown()
        self.fc_flash_page.shutdown()
        super().closeEvent(event)
