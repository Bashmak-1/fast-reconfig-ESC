"""EXPERIMENTAL "UID Lab" tab: reads a hardware factory-unique Device ID from
an ESC running a custom-forked AM32 bootloader (see
docs-local/IDEA_ESC_TRACEABILITY.md), and flashes a bootloader-updater .hex
that carries such a fork onto a real ESC.

Kept in its own tab, driven by its own private connection (own
UidLabManager + QThread, mirroring hotspot_station.py's isolation pattern),
deliberately NOT wired into the shared EscManager/request_* signals the
Main/Config/Хотспот tabs use - this feature's firmware-flash operation is
destructive (erases the ESC's working application until a new one is
flashed back) in a way nothing else in this app does, and it only matters
for the handful of ESCs actually being used to develop/validate the UID
patch, not the normal flashing workflow every other tab exists for.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .esc_manager import MAX_ESC_COUNT
from .serial_transport import list_fc_ports
from .settings_store import load_str, save_str
from .theme import ACCENT_ERROR, ACCENT_GREEN, TEXT_MUTED
from .uid_lab_manager import UidLabManager

_PORT_KEY = "uid_lab_port"
_ESC_INDEX_KEY = "uid_lab_esc_index"
_HEX_PATH_KEY = "uid_lab_hex_path"
DEFAULT_BAUD = 115200

_LOG_COLORS = {"info": TEXT_MUTED, "warning": "#e8a33d", "error": ACCENT_ERROR, "success": ACCENT_GREEN}


class UidLabPage(QWidget):
    # GUI -> worker thread. These must be emitted, never called directly -
    # self.manager lives on self._thread, and a plain Python method call
    # runs synchronously on the CALLING (GUI) thread, not the receiver's.
    # Only a signal-slot connection gets Qt's automatic queued dispatch onto
    # the receiver's own thread's event loop (based on the receiver's thread
    # affinity, not the sender's) - see connection_worker.py /
    # hotspot_station.py, which this mirrors. Calling manager.connect(...)
    # etc. directly here would run its time.sleep() calls (up to ~2s for
    # connect, much longer for a multi-KB firmware flash) on the GUI thread
    # and freeze the whole window for that long.
    _connect_requested = pyqtSignal(str, int)
    _disconnect_requested = pyqtSignal()
    _read_uid_requested = pyqtSignal(int)
    _flash_firmware_requested = pyqtSignal(int, str)

    # Own GUI-thread-affinity signal (this widget lives on the GUI thread),
    # re-emitted from within _on_connection_state_changed once that slot is
    # actually running on the GUI thread (guaranteed by the queued
    # cross-thread dispatch on manager.connection_state_changed below).
    # MainWindow connects other tabs' recheck_availability() to THIS signal,
    # not to manager.connection_state_changed directly - that one is emitted
    # from self._thread, and a plain lambda/function slot (unlike a bound
    # QObject method) gets no automatic queuing, so calling into another
    # tab's widgets from it would touch Qt objects off the GUI thread.
    connection_changed = pyqtSignal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self._thread = QThread()
        self._thread.setObjectName("UidLabManagerThread")
        self.manager = UidLabManager()
        self.manager.moveToThread(self._thread)
        self._thread.start()

        self._connected = False
        self._hex_path = ""
        self._port_busy_checker: Optional[Callable[[str], bool]] = None

        self._build_ui()
        self._wire_signals()
        self._refresh_ports()
        self._restore_settings()

    # ---- construction ------------------------------------------------

    def _build_ui(self) -> None:
        # Deliberately plain Ukrainian string literals, not i18n.tr() like
        # every other tab: this whole feature is an EXPERIMENTAL lab bench
        # for validating the UID bootloader patch (docs-local/
        # IDEA_ESC_TRACEABILITY.md), whose wording is expected to change
        # often while that work is ongoing - not worth the i18n.py upkeep
        # for a tab most operators will never open. The app's day-to-day
        # language is Ukrainian (see every other tab's log strings), so
        # that's what this one uses too, rather than defaulting to English.
        root = QVBoxLayout(self)

        warning = QLabel(
            "⚠ ЕКСПЕРИМЕНТАЛЬНО: лише для розробки й перевірки кастомного форку бутлоадера AM32. "
            "Прошивання тут стирає робочу прошивку ESC, поки не буде прошито нову. "
            "Не використовувати на робочих ESC."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(f"color: {_LOG_COLORS['warning']}; font-weight: 600;")
        root.addWidget(warning)

        conn_row = QHBoxLayout()
        conn_row.addWidget(QLabel("Порт:"))
        self._port_combo = QComboBox()
        self._port_combo.setMinimumWidth(220)
        conn_row.addWidget(self._port_combo)
        self._refresh_btn = QPushButton("↻")
        self._refresh_btn.setFixedSize(36, 30)
        self._refresh_btn.clicked.connect(self._refresh_ports)
        conn_row.addWidget(self._refresh_btn)
        conn_row.addWidget(QLabel("ESC:"))
        self._esc_combo = QComboBox()
        for i in range(MAX_ESC_COUNT):
            self._esc_combo.addItem(f"ESC {i + 1}", i)
        self._esc_combo.currentIndexChanged.connect(self._on_esc_index_changed)
        conn_row.addWidget(self._esc_combo)
        conn_row.addStretch(1)
        self._connect_btn = QPushButton("Підключити")
        self._connect_btn.clicked.connect(self._on_connect_toggle_clicked)
        conn_row.addWidget(self._connect_btn)
        root.addLayout(conn_row)

        uid_row = QHBoxLayout()
        self._read_uid_btn = QPushButton("Прочитати UID")
        self._read_uid_btn.setEnabled(False)
        self._read_uid_btn.clicked.connect(self._on_read_uid_clicked)
        uid_row.addWidget(self._read_uid_btn)
        self._uid_result_label = QLabel("")
        self._uid_result_label.setStyleSheet(f"color: {TEXT_MUTED}; font-family: Consolas, monospace;")
        uid_row.addWidget(self._uid_result_label, 1)
        root.addLayout(uid_row)

        fw_row = QHBoxLayout()
        self._pick_hex_btn = QPushButton("Обрати updater .hex...")
        self._pick_hex_btn.clicked.connect(self._on_pick_hex_clicked)
        fw_row.addWidget(self._pick_hex_btn)
        self._hex_path_label = QLabel("Файл прошивки не обрано...")
        self._hex_path_label.setWordWrap(True)
        fw_row.addWidget(self._hex_path_label, 1)
        root.addLayout(fw_row)

        flash_row = QHBoxLayout()
        self._flash_btn = QPushButton("Прошити firmware")
        self._flash_btn.setEnabled(False)
        self._flash_btn.clicked.connect(self._on_flash_clicked)
        flash_row.addWidget(self._flash_btn)
        self._flash_progress = QProgressBar()
        self._flash_progress.setRange(0, 100)
        flash_row.addWidget(self._flash_progress, 1)
        root.addLayout(flash_row)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(1000)
        self._log_view.setStyleSheet("font-family: Consolas, monospace;")
        root.addWidget(self._log_view, 1)

    def _wire_signals(self) -> None:
        self._connect_requested.connect(self.manager.connect)
        self._disconnect_requested.connect(self.manager.disconnect)
        self._read_uid_requested.connect(self.manager.read_uid)
        self._flash_firmware_requested.connect(self.manager.flash_firmware)

        self.manager.connection_state_changed.connect(self._on_connection_state_changed)
        self.manager.log_message.connect(self._append_log)
        self.manager.uid_result.connect(self._on_uid_result)
        self.manager.flash_progress.connect(self._on_flash_progress)
        self.manager.flash_finished.connect(self._on_flash_finished)

    # ---- port/ESC pickers ------------------------------------------------

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

    def _selected_esc_index(self) -> int:
        data = self._esc_combo.currentData()
        return int(data) if data is not None else 0

    def _on_esc_index_changed(self, *_args) -> None:
        save_str(_ESC_INDEX_KEY, str(self._selected_esc_index()))

    def _restore_settings(self) -> None:
        port = load_str(_PORT_KEY)
        if port:
            idx = self._port_combo.findData(port)
            if idx >= 0:
                self._port_combo.setCurrentIndex(idx)
        index_str = load_str(_ESC_INDEX_KEY)
        if index_str:
            idx = self._esc_combo.findData(int(index_str))
            if idx >= 0:
                self._esc_combo.setCurrentIndex(idx)
        hex_path = load_str(_HEX_PATH_KEY)
        if hex_path and Path(hex_path).is_file():
            self._set_hex_path(hex_path)

    # ---- port-collision guard ------------------------------------------------
    # Same real-hardware-confirmed risk as hotspot_page/hotspot_line_page
    # (main_window.py's _is_port_busy_for_* methods): two connections on the
    # same physical port can make BOTH sides' ESC probes fail, not just the
    # loser. MainWindow injects this the same way it does for those two tabs.

    def set_port_busy_checker(self, checker: Callable[[str], bool]) -> None:
        self._port_busy_checker = checker

    def recheck_availability(self) -> None:
        """Call when some OTHER connection surface's armed/connected state
        changes, so this tab's Підключити button reflects port availability
        without waiting for the operator to touch this tab first."""
        if not self._connected:
            port = self._selected_port()
            busy = bool(port and self._port_busy_checker and self._port_busy_checker(port))
            self._connect_btn.setEnabled(not busy)
            self._connect_btn.setToolTip(f"{port} вже використовується іншою вкладкою." if busy else "")

    # ---- connect / disconnect ------------------------------------------------

    def _on_connect_toggle_clicked(self) -> None:
        if self._connected:
            self._connect_btn.setEnabled(False)
            self._disconnect_requested.emit()
            return
        port = self._selected_port()
        if not port:
            self._append_log("error", "COM-порт не обрано.")
            return
        if self._port_busy_checker and self._port_busy_checker(port):
            self._append_log("error", f"{port} вже використовується іншою вкладкою.")
            return
        save_str(_PORT_KEY, port)
        self._connect_btn.setEnabled(False)
        self._append_log("info", f"Підключення до {port} @ {DEFAULT_BAUD}...")
        self._connect_requested.emit(port, DEFAULT_BAUD)

    def _on_connection_state_changed(self, connected: bool, expected_count: int) -> None:
        self._connected = connected
        self._connect_btn.setEnabled(True)
        self._connect_btn.setText("Відключити" if connected else "Підключити")
        self._read_uid_btn.setEnabled(connected)
        self._flash_btn.setEnabled(connected and bool(self._hex_path))
        self.connection_changed.emit(connected)

    def connected_port(self) -> Optional[str]:
        """For MainWindow's cross-tab port-collision guard - the port this
        tab currently holds a connection on, or None if not connected."""
        return self._selected_port() if self._connected else None

    # ---- UID read ------------------------------------------------

    def _on_read_uid_clicked(self) -> None:
        self._uid_result_label.setText("Читання...")
        self._read_uid_requested.emit(self._selected_esc_index())

    def _on_uid_result(self, index: int, ok: bool, value: str) -> None:
        if index != self._selected_esc_index():
            return
        if ok:
            self._uid_result_label.setStyleSheet(f"color: {ACCENT_GREEN}; font-family: Consolas, monospace;")
            self._uid_result_label.setText(f"UID: {value}")
        else:
            self._uid_result_label.setStyleSheet(f"color: {ACCENT_ERROR}; font-family: Consolas, monospace;")
            self._uid_result_label.setText(f"Помилка: {value}")

    # ---- firmware flashing ------------------------------------------------

    def _on_pick_hex_clicked(self) -> None:
        downloads = str(Path.home() / "Downloads")
        path, _ = QFileDialog.getOpenFileName(
            self, "Обрати прошивку updater-а", downloads, "Intel HEX (*.hex);;Всі файли (*)"
        )
        if path:
            self._set_hex_path(path)
            save_str(_HEX_PATH_KEY, path)

    def _set_hex_path(self, path: str) -> None:
        self._hex_path = path
        self._hex_path_label.setText(path)
        self._flash_btn.setEnabled(self._connected and bool(path))

    def _on_flash_clicked(self) -> None:
        if not self._hex_path:
            return
        self._flash_btn.setEnabled(False)
        self._flash_progress.setValue(0)
        self._flash_firmware_requested.emit(self._selected_esc_index(), self._hex_path)

    def _on_flash_progress(self, done: int, total: int) -> None:
        if total > 0:
            self._flash_progress.setValue(int(done * 100 / total))

    def _on_flash_finished(self, success: bool, message: str) -> None:
        self._flash_btn.setEnabled(self._connected and bool(self._hex_path))
        self._flash_progress.setValue(100 if success else 0)

    # ---- log / lifecycle ------------------------------------------------

    def _append_log(self, level: str, text: str) -> None:
        color = _LOG_COLORS.get(level, "#e6e6e6")
        self._log_view.appendHtml(f'<span style="color:{color};">{text}</span>')

    def shutdown(self) -> None:
        """Stop this tab's private connection/thread - call on app close."""
        if self._connected:
            self.manager.disconnect()
        self._thread.quit()
        self._thread.wait(2000)
