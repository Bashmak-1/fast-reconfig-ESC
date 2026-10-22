"""The "Hotspot" tab: hands-off hot-plug flashing station.

Meant for an operator who doesn't need to understand the tool at all: pick
the correct config file and the port once, click Start, then just plug ESCs
in one after another. Each one is checked against the loaded config, flashed
only if it actually differs (the existing skip-if-matches guarantee in
EscManager.flash_bytes makes an already-correct ESC a no-op), and a big
banner says clearly when it's safe to unplug that ESC's wires.

The FC itself stays connected over USB the whole time an operator is
swapping through ESCs - individual ESC signal wires are what get plugged in
and out, and neither MSP nor FourWay has any way to notify us when that
happens (the FC doesn't know or report it). So there is no "wait for the
port to disappear" signal to key off between rounds: instead, once
connected, this keeps the SAME connection open and loops
discover -> flash -> discover -> flash indefinitely on its own, pausing only
briefly after each round so the banner is readable. Re-scanning an ESC that
hasn't changed is harmless and cheap (no wear - flash_bytes never rewrites
an already-correct ESC), so looping continuously while a swap is in
progress is the correct default, not a bug to guard against.

This tab intentionally sweeps ALL of the FC's reported ESC outputs
(EscManager.discover_all) every round, not just one - it's the "flash a
whole batch of ESCs already wired to the FC" workflow. For the "watch just
one operator-chosen ESC output" workflow (a single port swapped through one
ESC at a time), see the second, independent "Хотспот (лінія)" tab
(hotspot_line_widget.py / hotspot_station.py) - deliberately a SEPARATE tab
with its own logic, not a mode of this one, per the user's explicit request
(docs/PROTOCOL_NOTES.md, 2026-08-26).

The port-presence poll is now only used for the very first connect (wait
for the FC's own USB link to appear) and to notice if that FC gets unplugged
entirely mid-session (a real, if rare, event - as opposed to an ESC wire
change, which is invisible to us).

Drives EscManager only indirectly, through signals MainWindow wires to the
existing request_connect/request_disconnect/request_discover/
request_flash_bytes - same delegation pattern ConfigEditorPage already uses.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import i18n
from .eeprom_config import ConfigFileError, load_config_file
from .esc_comparison_widget import EscComparisonList
from .manual_counter_widget import ManualCounterWidget
from .serial_transport import list_fc_ports
from .settings_store import load_last_config_path
from .theme import ACCENT_AMBER, ACCENT_ERROR, ACCENT_GREEN, BG_PANEL, TEXT_MAIN, TEXT_MUTED

DEFAULT_BAUD = 115200
MAX_CONNECT_ATTEMPTS = 5
POLL_INTERVAL_MS = 1000

_PHASE_STYLE = {
    "idle": (BG_PANEL, TEXT_MUTED),
    "waiting_device": (BG_PANEL, TEXT_MAIN),
    "connecting": (ACCENT_AMBER, "white"),
    "checking": (ACCENT_AMBER, "white"),
    "flashing": (ACCENT_ERROR, "white"),
    "round_done": (ACCENT_GREEN, "white"),
    "round_warning": (ACCENT_AMBER, "white"),
    "round_error": (ACCENT_ERROR, "white"),
    "error_connect": (ACCENT_ERROR, "white"),
}
_PHASE_BANNER_KEY = {
    "idle": "hotspot_banner_idle",
    "waiting_device": "hotspot_banner_waiting_device",
    "connecting": "hotspot_banner_connecting",
    "checking": "hotspot_banner_checking",
    "flashing": "hotspot_banner_flashing",
    "round_done": "hotspot_banner_done",
    "round_warning": "hotspot_banner_warning_not_found",
    "round_error": "hotspot_banner_error_flash",
    "error_connect": "hotspot_banner_error_connect",
}
# Phases that finish one round and, once the FC is confirmed still present,
# automatically loop back into another discover/flash round on the next
# poll tick - no click needed. Only a true connection-level failure
# (error_connect) waits for a manual retry or the FC itself disappearing.
_AUTO_CONTINUE_PHASES = {"round_done", "round_warning", "round_error"}
_RETRY_PHASES = {"error_connect"}
_LOG_COLORS = {"info": TEXT_MUTED, "warning": ACCENT_AMBER, "error": ACCENT_ERROR, "success": ACCENT_GREEN}


class HotspotFlashPage(QWidget):
    connect_requested = pyqtSignal(str, int)
    disconnect_requested = pyqtSignal()
    discover_requested = pyqtSignal()
    flash_requested = pyqtSignal(bytes)
    armed_changed = pyqtSignal(bool)
    config_path_changed = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._config_bytes: Optional[bytes] = None
        self._config_path: str = ""
        self._armed = False
        self._phase = "idle"
        self._connect_attempts = 0
        self._connect_in_flight = False
        self._manual_connection_active = False
        self._port_busy_checker: Optional[Callable[[str], bool]] = None
        self._counter_flashed = 0
        self._counter_ok = 0
        self._counter_errors = 0

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._on_poll_tick)

        # Purely a UI refresh - ticks faster than _poll_timer so the "next
        # check in..." countdown reads smoothly instead of jumping straight
        # from 1s to 0s. An impatient operator staring at an unchanging
        # banner for a few seconds easily mistakes "still polling" for
        # "stuck" - this is a live reassurance that it isn't.
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(150)
        self._countdown_timer.timeout.connect(self._update_countdown)

        self._build_ui()
        i18n.signals.language_changed.connect(self._retranslate)
        self._restore_last_config()
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
        # A long config path (deeply nested folders) must never be allowed to
        # dictate the window's natural width - QLabel's sizeHint ignores
        # word-wrap when a parent layout asks "how wide do you want to be",
        # so an explicit cap is the only thing that actually bounds it.
        self._config_label.setMaximumWidth(700)
        top.addWidget(self._config_label, 1)
        root.addLayout(top)

        port_row = QHBoxLayout()
        self._port_label = QLabel()
        port_row.addWidget(self._port_label)
        self._port_combo = QComboBox()
        self._port_combo.setMinimumWidth(220)
        self._port_combo.currentIndexChanged.connect(lambda *_: self._update_start_stop_button())
        port_row.addWidget(self._port_combo)
        self._refresh_btn = QPushButton("↻")
        self._refresh_btn.setFixedSize(36, 30)
        self._refresh_btn.setStyleSheet("font-size: 16px; padding: 0px;")
        self._refresh_btn.clicked.connect(self._refresh_ports)
        port_row.addWidget(self._refresh_btn)
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

        self._countdown_label = QLabel()
        self._countdown_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._countdown_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._countdown_label)

        self._retry_btn = QPushButton()
        self._retry_btn.clicked.connect(self._on_retry_clicked)
        self._retry_btn.setVisible(False)
        root.addWidget(self._retry_btn)

        self._comparison_list = EscComparisonList()
        root.addWidget(self._comparison_list, 1)

        counters_row = QHBoxLayout()
        self._counters_label = QLabel()
        self._counters_label.setStyleSheet(f"color: {TEXT_MUTED};")
        counters_row.addWidget(self._counters_label, 1)
        self._manual_counter_label = QLabel()
        self._manual_counter_label.setStyleSheet(f"color: {TEXT_MUTED};")
        counters_row.addWidget(self._manual_counter_label)
        self._manual_counter = ManualCounterWidget()
        counters_row.addWidget(self._manual_counter)
        self._reset_counters_btn = QPushButton()
        self._reset_counters_btn.clicked.connect(self._on_reset_counters_clicked)
        counters_row.addWidget(self._reset_counters_btn)
        root.addLayout(counters_row)

        # Mirrors MainWindow's own activity log here too (see append_log()
        # and main_window.py's _append_log()) - this tab drives the same
        # shared EscManager the Головна tab does (module docstring), so
        # without a local copy its own connect/discover/flash activity
        # would only ever be visible while looking at a different tab.
        self._activity_log = QPlainTextEdit()
        self._activity_log.setReadOnly(True)
        self._activity_log.setMaximumBlockCount(2000)
        self._activity_log.setMaximumHeight(160)
        self._activity_log.setStyleSheet("font-family: Consolas, monospace;")
        root.addWidget(self._activity_log)

        self._refresh_ports()

    # ---- i18n ------------------------------------------------

    def _retranslate(self, *_args) -> None:
        self._load_config_btn.setText(i18n.tr("hotspot_load_config"))
        if self._config_bytes is None:
            self._config_label.setText(i18n.tr("hotspot_no_config"))
        else:
            self._config_label.setText(i18n.tr("hotspot_config_loaded", path=self._config_path))
        self._port_label.setText(i18n.tr("hotspot_port_label"))
        self._refresh_btn.setToolTip(i18n.tr("refresh_tooltip"))
        self._retry_btn.setText(i18n.tr("hotspot_retry"))
        self._reset_counters_btn.setText(i18n.tr("hotspot_reset_counters"))
        self._manual_counter_label.setText(i18n.tr("hotspot_manual_counter_label"))
        self._manual_counter.setToolTip(i18n.tr("hotspot_manual_counter_tooltip"))
        self._update_counters_label()
        self._render_banner()
        self._update_start_stop_button()
        self._update_countdown()

    # ---- config / port pickers ------------------------------------------------

    def _on_load_config_clicked(self) -> None:
        downloads = str(Path.home() / "Downloads")
        path, _ = QFileDialog.getOpenFileName(
            self, i18n.tr("hotspot_load_config"), downloads, i18n.tr("binary_config_filter")
        )
        if not path:
            return
        try:
            self._config_bytes = load_config_file(path)
        except ConfigFileError as e:
            self._config_label.setText(str(e))
            self._config_label.setStyleSheet(f"color: {ACCENT_ERROR};")
            return
        self._config_path = path
        self._config_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._config_label.setText(i18n.tr("hotspot_config_loaded", path=path))
        self._update_start_stop_button()
        # EscManager.discover_all() classifies each ESC's readback against its
        # OWN _loaded_config_bytes, set only via request_set_config_path — not
        # the bytes flash_requested carries. Without this, a freshly-flashed
        # (now-matching) ESC gets re-classified as DIFF on the very next
        # auto-continuing round because EscManager is still comparing against
        # whatever config path the Main/Config tab last set (or none at all).
        self.config_path_changed.emit(path)

    def _restore_last_config(self) -> None:
        """Pick up the same last-used config file the Main tab remembers, so
        the operator doesn't have to separately re-browse for it here just
        because this tab keeps its own working copy of the bytes (an easy
        thing to mistake for "Start doesn't work" if missed)."""
        path = load_last_config_path()
        if not path or not Path(path).is_file():
            return
        try:
            self._config_bytes = load_config_file(path)
        except ConfigFileError:
            return
        self._config_path = path

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
        self._update_start_stop_button()

    def _selected_port_device(self) -> Optional[str]:
        data = self._port_combo.currentData()
        return str(data) if data else None

    # ---- arm / disarm ------------------------------------------------

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
        if not self._armed:
            self._update_start_stop_button()

    def _on_start_stop_clicked(self) -> None:
        if self._armed:
            self._disarm()
        else:
            self._arm()

    def _arm(self) -> None:
        port = self._selected_port_device()
        if port and self._port_busy_checker and self._port_busy_checker(port):
            self._set_start_hint(i18n.tr("hotspot_port_busy_elsewhere"))
            return
        self._armed = True
        self.armed_changed.emit(True)
        self._connect_attempts = 0
        self._connect_in_flight = False
        self._load_config_btn.setEnabled(False)
        self._port_combo.setEnabled(False)
        self._refresh_btn.setEnabled(False)
        self._set_phase("waiting_device")
        self._poll_timer.start()
        self._countdown_timer.start()

    def _disarm(self) -> None:
        self._armed = False
        self.armed_changed.emit(False)
        self._poll_timer.stop()
        self._countdown_timer.stop()
        self._countdown_label.setText("")
        if self._phase not in ("idle", "waiting_device"):
            self.disconnect_requested.emit()
        self._load_config_btn.setEnabled(True)
        self._port_combo.setEnabled(True)
        self._refresh_btn.setEnabled(True)
        self._comparison_list.clear_all(0)
        self._set_phase("idle")

    # ---- polling / hot-plug detection ------------------------------------------------

    def _on_poll_tick(self) -> None:
        if not self._armed:
            return
        port = self._selected_port_device()
        if not port:
            return
        present = port in {info.device for info in list_fc_ports()}

        if self._phase == "waiting_device":
            if present:
                self._connect_attempts = 0
                self._set_phase("connecting")
                self._attempt_connect()
        elif self._phase == "connecting":
            if self._connect_in_flight:
                # EscManager.connect() includes a required ~2s protocol
                # delay (POST_PASSTHROUGH_DELAY_S) after entering
                # passthrough - longer than this 1s poll interval. Firing
                # another connect_requested before the first one has even
                # resolved queues a second EscManager.connect() call that
                # runs right after the first succeeds, tries to open the
                # already-open port, and fails - and that failed open
                # attempt was confirmed on real hardware to disrupt the
                # FIRST connection's own FourWay traffic, not just fail
                # cleanly. Wait for the in-flight attempt to resolve
                # (success or failure) before ever attempting another.
                return
            if not present:
                self._set_phase("waiting_device")
            elif self._connect_attempts >= MAX_CONNECT_ATTEMPTS:
                self._set_phase("error_connect")
            else:
                self._attempt_connect()
        elif self._phase == "error_connect":
            if not present:
                self._set_phase("waiting_device")
        elif self._phase in _AUTO_CONTINUE_PHASES:
            if present:
                self._start_next_round()
            else:
                # The FC's own USB link is actually gone (not just an ESC
                # wire, which we'd never see) - release the connection and
                # wait for it to come back.
                self.disconnect_requested.emit()
                self._set_phase("waiting_device")

    def _attempt_connect(self) -> None:
        port = self._selected_port_device()
        if not port:
            self._set_phase("error_connect")
            return
        self._connect_attempts += 1
        self._connect_in_flight = True
        self.connect_requested.emit(port, DEFAULT_BAUD)

    def _start_next_round(self) -> None:
        self._set_phase("checking")
        self.discover_requested.emit()

    # ---- retry / counters ------------------------------------------------

    def _on_retry_clicked(self) -> None:
        if self._phase == "error_connect":
            self._connect_attempts = 0
            self._set_phase("connecting")
            self._attempt_connect()

    def _on_reset_counters_clicked(self) -> None:
        self._counter_flashed = 0
        self._counter_ok = 0
        self._counter_errors = 0
        self._update_counters_label()
        self._manual_counter.reset()

    def _update_counters_label(self) -> None:
        self._counters_label.setText(
            i18n.tr(
                "hotspot_counters",
                flashed=self._counter_flashed,
                ok=self._counter_ok,
                errors=self._counter_errors,
            )
        )

    # ---- banner / button rendering ------------------------------------------------

    def _set_phase(self, phase: str) -> None:
        self._phase = phase
        self._render_banner()
        self._update_start_stop_button()
        self._update_countdown()

    def _update_countdown(self) -> None:
        if self._phase == "connecting":
            self._countdown_label.setText(
                i18n.tr("hotspot_connect_attempt", attempt=self._connect_attempts, max=MAX_CONNECT_ATTEMPTS)
            )
            return
        remaining_ms = self._poll_timer.remainingTime() if self._poll_timer.isActive() else -1
        if self._phase == "flashing" or remaining_ms < 0:
            self._countdown_label.setText("")
            return
        self._countdown_label.setText(i18n.tr("hotspot_next_check_in", seconds=f"{remaining_ms / 1000:.1f}"))

    def _render_banner(self) -> None:
        bg, fg = _PHASE_STYLE[self._phase]
        self._banner.setStyleSheet(
            f"background-color: {bg}; color: {fg}; font-size: 22px; font-weight: 700;"
            "padding: 24px; border-radius: 8px;"
        )
        self._banner.setText(i18n.tr(_PHASE_BANNER_KEY[self._phase]))
        self._retry_btn.setVisible(self._phase in _RETRY_PHASES)

    def _update_start_stop_button(self) -> None:
        if self._armed:
            self._start_stop_btn.setText(i18n.tr("hotspot_stop"))
            flashing = self._phase == "flashing"
            self._start_stop_btn.setEnabled(not flashing)
            self._set_start_hint(i18n.tr("hotspot_stop_disabled_flashing") if flashing else "")
            return

        self._start_stop_btn.setText(i18n.tr("hotspot_start"))
        if self._manual_connection_active:
            self._start_stop_btn.setEnabled(False)
            self._set_start_hint(i18n.tr("hotspot_start_disabled_manual"))
        elif self._config_bytes is None:
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
        # A tooltip alone is too easy to miss (needs hovering exactly over
        # the button) - show the same reason as always-visible text too,
        # since "Старт does nothing" was reported as a bug when this was
        # tooltip-only and the actual cause (still manually connected on
        # the Main tab) was invisible without hovering.
        self._start_stop_btn.setToolTip(text)
        self._start_hint_label.setText(text)
        self._start_hint_label.setVisible(bool(text))

    # ---- forwarded EscManager signals (called by MainWindow) ------------------------------------------------

    def on_connection_state_changed(self, connected: bool, expected_count: int) -> None:
        # Cleared unconditionally, before any branch below - a stale
        # in-flight flag left True would otherwise silently block every
        # future reconnect attempt if this particular signal happens to
        # arrive after the phase already moved on (see _on_poll_tick).
        self._connect_in_flight = False
        if not self._armed:
            self._manual_connection_active = connected
            self._update_start_stop_button()
            return

        if self._phase != "connecting":
            return
        if connected:
            self._comparison_list.clear_all(expected_count)
            self._set_phase("checking")
        # A failed attempt just leaves us in "connecting" - the poll timer retries or gives up.

    def on_discovery_finished(self, found: int, expected: int) -> None:
        if not self._armed or self._phase != "checking":
            return
        if found == 0:
            # Nothing wired right now is a normal transient state while the
            # operator's hand is mid-swap, not an error - _on_poll_tick will
            # automatically start another round shortly, no click needed.
            self._set_phase("round_warning")
            return
        self._set_phase("flashing")
        self.flash_requested.emit(self._config_bytes)

    def on_flash_finished(self, success: int, attempted: int) -> None:
        if not self._armed or self._phase != "flashing":
            return
        if success == attempted:
            if attempted == 0:
                self._counter_ok += 1
            else:
                self._counter_flashed += 1
                self._manual_counter.increment()
            self._update_counters_label()
            self._set_phase("round_done")
        else:
            self._counter_errors += 1
            self._update_counters_label()
            self._set_phase("round_error")
        # Deliberately stays connected here - the FC's own USB link doesn't
        # change between ESCs, only individual ESC wires do, and we have no
        # way to see that. The next poll tick starts the next round on its
        # own (see _on_poll_tick / _AUTO_CONTINUE_PHASES).

    def on_esc_status_changed(self, index: int, state: str, detail: str, esc_info) -> None:
        if not self._armed:
            return
        self._comparison_list.set_row(index, state, detail)

    # ---- activity log (fed by MainWindow._append_log - see that method) ---

    def append_log(self, level: str, text: str) -> None:
        color = _LOG_COLORS.get(level, TEXT_MUTED)
        self._activity_log.appendHtml(f'<span style="color:{color};">{text}</span>')
