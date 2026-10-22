"""The "Config" tab: view + edit + compare a 184-byte ESC settings buffer.

The edited buffer (`self._working_config`) lives only in memory until the
user explicitly clicks Save (writes to a file) or Apply (writes straight to
ESC hardware via EscManager.flash_bytes) - it never silently overwrites the
loaded file or hardware on its own, matching how am32.ca/configurator only
commits on its own explicit Save/Apply buttons.

Every field is built generically from `eeprom_fields.FIELD_SPECS` - there is
no per-field widget code to keep in sync with the protocol layer by hand.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from . import i18n
from .eeprom_config import ConfigFileError, load_config_file
from .eeprom_fields import FieldSpec, GROUP_ORDER, applicable_specs, decode_field, encode_field
from .eeprom_values import format_field_value
from .mcu_profiles import LAYOUT_SIZE
from .theme import ACCENT_AMBER, ACCENT_ERROR, ACCENT_GREEN, TEXT_MUTED

_LOG_COLORS = {"info": TEXT_MUTED, "warning": ACCENT_AMBER, "error": ACCENT_ERROR, "success": ACCENT_GREEN}


class _ClickToWheelMixin:
    """Wheel only nudges the value once this widget has been clicked (given
    focus) - otherwise the wheel event is ignored so it bubbles up to the
    enclosing QScrollArea and just scrolls the page, instead of silently
    changing a value the cursor happened to be passing over. Moving the
    mouse off the widget clears focus again, so scrolling past it a second
    time (without clicking it again) stays safe. Dragging a slider past its
    own edges while the button is held still works normally - focus is only
    dropped on a genuine "moved away, not interacting" leave.
    """

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()

    def leaveEvent(self, event) -> None:
        if not (hasattr(self, "isSliderDown") and self.isSliderDown()):
            self.clearFocus()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if not self.underMouse():
            self.clearFocus()


class _ClickToWheelSlider(_ClickToWheelMixin, QSlider):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)


class _ClickToWheelComboBox(_ClickToWheelMixin, QComboBox):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)


class ConfigEditorPage(QWidget):
    apply_requested = pyqtSignal(bytes)
    file_loaded = pyqtSignal(str)  # so MainWindow can sync the Main tab's active config path

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._working_config: Optional[bytearray] = None
        self._original_config: Optional[bytes] = None
        self._source_desc = ""
        self._dirty = False
        self._connected = False
        self._esc_options: dict[int, tuple[str, bytes]] = {}  # index -> (label, settings)
        self._rows: dict[int, tuple] = {}  # offset -> (spec, control_widget, disabled_checkbox|None, row_widget)

        self._build_ui()
        i18n.signals.language_changed.connect(self._retranslate)
        self._retranslate()
        self._refresh_source_label()

    # ---- construction ------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        top = QHBoxLayout()
        self._load_file_btn = QPushButton()
        self._load_file_btn.clicked.connect(self._on_load_file_clicked)
        top.addWidget(self._load_file_btn)

        self._esc_combo = QComboBox()
        self._esc_combo.setMinimumWidth(160)
        top.addWidget(self._esc_combo)
        self._read_esc_btn = QPushButton()
        self._read_esc_btn.clicked.connect(self._on_read_from_esc_clicked)
        top.addWidget(self._read_esc_btn)

        top.addStretch(1)
        self._save_file_btn = QPushButton()
        self._save_file_btn.clicked.connect(self._on_save_file_clicked)
        top.addWidget(self._save_file_btn)
        self._apply_btn = QPushButton()
        self._apply_btn.setObjectName("connectButton")
        self._apply_btn.clicked.connect(self._on_apply_clicked)
        top.addWidget(self._apply_btn)
        root.addLayout(top)

        self._source_label = QLabel()
        self._source_label.setStyleSheet(f"color: {TEXT_MUTED};")
        root.addWidget(self._source_label)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._form_container = QWidget()
        self._form_layout = QVBoxLayout(self._form_container)
        self._form_layout.addStretch(1)
        self._scroll.setWidget(self._form_container)
        root.addWidget(self._scroll, 1)

        self._empty_label = QLabel()
        self._empty_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._form_layout.insertWidget(0, self._empty_label)

        # Mirrors MainWindow's own activity log here too (see append_log()
        # and main_window.py's _append_log()) - Apply's actual flash RESULT
        # comes back through the same shared EscManager the Головна tab
        # uses, and was only ever visible there before this.
        self._activity_log = QPlainTextEdit()
        self._activity_log.setReadOnly(True)
        self._activity_log.setMaximumBlockCount(2000)
        self._activity_log.setMaximumHeight(160)
        self._activity_log.setStyleSheet("font-family: Consolas, monospace;")
        root.addWidget(self._activity_log)

        self._set_controls_enabled(False)

    def _set_controls_enabled(self, enabled: bool) -> None:
        self._save_file_btn.setEnabled(enabled)
        self._apply_btn.setEnabled(enabled and self._connected)
        self._scroll.setVisible(enabled)
        self._empty_label.setVisible(not enabled)

    def set_apply_enabled(self, connected: bool) -> None:
        """Called by MainWindow when the FC connection state changes, or
        when a flash batch finishes and Apply should become clickable again.
        Apply is only ever enabled when BOTH a config is loaded in the
        editor AND the app is currently connected to a FC.
        """
        self._connected = connected
        self._apply_btn.setEnabled(connected and self._working_config is not None)

    # ---- i18n ------------------------------------------------

    def _retranslate(self, *_args) -> None:
        self._load_file_btn.setText(i18n.tr("config_load_file"))
        self._read_esc_btn.setText(i18n.tr("config_read_from_esc"))
        self._save_file_btn.setText(i18n.tr("config_save_to_file"))
        self._apply_btn.setText(i18n.tr("config_apply_to_esc"))
        self._empty_label.setText(i18n.tr("config_no_data_loaded"))
        self._refresh_esc_combo()
        self._rebuild_form()  # group/field labels need re-translating too
        self._refresh_source_label()

    # ---- external data feed (from MainWindow) ------------------------------------------------

    def set_available_escs(self, options: dict[int, tuple[str, bytes]]) -> None:
        self._esc_options = options
        self._refresh_esc_combo()

    def _refresh_esc_combo(self) -> None:
        current = self._esc_combo.currentData()
        self._esc_combo.clear()
        if not self._esc_options:
            self._esc_combo.addItem(i18n.tr("config_read_esc_placeholder"), None)
            self._read_esc_btn.setEnabled(False)
            return
        self._read_esc_btn.setEnabled(True)
        for index in sorted(self._esc_options):
            label, _settings = self._esc_options[index]
            self._esc_combo.addItem(i18n.tr("config_working_from_esc", n=index + 1) + f" ({label})", index)
        if current is not None:
            i = self._esc_combo.findData(current)
            if i >= 0:
                self._esc_combo.setCurrentIndex(i)

    # ---- loading ------------------------------------------------

    def _on_load_file_clicked(self) -> None:
        downloads = str(Path.home() / "Downloads")
        path, _ = QFileDialog.getOpenFileName(
            self, i18n.tr("select_config_dialog_title"), downloads, i18n.tr("binary_config_filter")
        )
        if not path:
            return
        try:
            data = load_config_file(path)
        except ConfigFileError as e:
            self._source_label.setText(str(e))
            self._source_label.setStyleSheet(f"color: {ACCENT_ERROR};")
            return
        self._load_working_config(bytearray(data), i18n.tr("config_working_from_file", path=path))
        self.file_loaded.emit(path)

    def _on_read_from_esc_clicked(self) -> None:
        index = self._esc_combo.currentData()
        if index is None or index not in self._esc_options:
            self._source_label.setText(i18n.tr("config_no_esc_to_read"))
            self._source_label.setStyleSheet(f"color: {ACCENT_ERROR};")
            return
        label, settings = self._esc_options[index]
        self._load_working_config(bytearray(settings), i18n.tr("config_working_from_esc", n=index + 1) + f" ({label})")

    def _load_working_config(self, data: bytearray, source_desc: str) -> None:
        self._working_config = data
        self._original_config = bytes(data)
        self._source_desc = source_desc
        self._dirty = False
        self._set_controls_enabled(True)
        self._rebuild_form()
        self._refresh_source_label()

    def has_unsaved_changes(self) -> bool:
        return self._dirty and self._working_config is not None

    def diff_text(self) -> str:
        """Human-readable "Label: old -> new" lines for every field that
        differs between the last loaded/saved/applied baseline and the
        current working buffer - used for the exit/save/apply confirmations
        so a change is never committed (or discarded) silently."""
        if self._working_config is None or self._original_config is None:
            return i18n.tr("config_diff_no_changes")
        current = bytes(self._working_config)
        lines = []
        for spec in applicable_specs(current):
            if self._original_config[spec.offset] == current[spec.offset]:
                continue
            old_text = format_field_value(spec, self._original_config)
            new_text = format_field_value(spec, current)
            lines.append(i18n.tr("config_diff_line", label=i18n.tr(spec.label_key), old=old_text, new=new_text))
        return "\n".join(lines) if lines else i18n.tr("config_diff_no_changes")

    def _refresh_source_label(self) -> None:
        if self._working_config is None:
            self._source_label.setText("")
            return
        text = i18n.tr("config_working_from", source=self._source_desc)
        if self._dirty:
            text += " " + i18n.tr("config_working_unsaved")
        self._source_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._source_label.setText(text)

    # ---- form construction ------------------------------------------------

    def _rebuild_form(self) -> None:
        # Clear existing group boxes (everything except the trailing stretch and the empty-state label).
        while self._form_layout.count() > 1:
            item = self._form_layout.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self._empty_label:
                widget.deleteLater()
        self._rows = {}

        if self._working_config is None:
            return

        settings = bytes(self._working_config)
        specs_by_group: dict[str, list[FieldSpec]] = {}
        for spec in applicable_specs(settings):
            specs_by_group.setdefault(spec.group_key, []).append(spec)

        insert_at = self._form_layout.count() - 1  # before the trailing stretch
        for group_key in GROUP_ORDER:
            specs = specs_by_group.get(group_key)
            if not specs:
                continue
            self._form_layout.insertWidget(insert_at, self._build_group_box(group_key, specs, settings))
            insert_at += 1

        self._update_conditional_visibility()

    def _build_group_box(self, group_key: str, specs: list[FieldSpec], settings: bytes) -> QGroupBox:
        box = QGroupBox(i18n.tr(group_key))
        box_layout = QHBoxLayout(box)
        box_layout.setSpacing(28)

        bool_specs = [s for s in specs if s.kind == "bool"]
        other_specs = [s for s in specs if s.kind != "bool"]

        if bool_specs:
            check_column = QVBoxLayout()
            check_column.setSpacing(10)
            for spec in bool_specs:
                check_column.addWidget(self._build_bool_checkbox(spec, settings))
            check_column.addStretch(1)
            box_layout.addLayout(check_column)

        if other_specs:
            grid = QGridLayout()
            grid.setHorizontalSpacing(24)
            grid.setVerticalSpacing(16)
            for col in range(3):
                grid.setColumnStretch(col, 1)
            for i, spec in enumerate(other_specs):
                cell = self._build_enum_cell(spec, settings) if spec.kind == "enum" else self._build_numeric_cell(spec, settings)
                grid.addWidget(cell, i // 3, i % 3)
            grid_wrap = QWidget()
            grid_wrap.setLayout(grid)
            box_layout.addWidget(grid_wrap, 1)

        return box

    def _make_label_row(self, spec: FieldSpec) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(i18n.tr(spec.label_key)))
        layout.addStretch(1)
        dots = QLabel("● ● ● ● ●")
        dots.setStyleSheet(f"color: {ACCENT_GREEN}; font-size: 7px;")
        layout.addWidget(dots)
        return row

    def _build_bool_checkbox(self, spec: FieldSpec, settings: bytes) -> QCheckBox:
        value = decode_field(spec, settings)
        control = QCheckBox(i18n.tr(spec.label_key))
        control.setChecked(bool(value))
        control.toggled.connect(lambda checked, s=spec: self._on_value_changed(s, checked))
        self._rows[spec.offset] = (spec, control, None, control)
        return control

    def _build_enum_cell(self, spec: FieldSpec, settings: bytes) -> QWidget:
        value = decode_field(spec, settings)
        cell = QWidget()
        layout = QVBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._make_label_row(spec))

        control = _ClickToWheelComboBox()
        for raw, text in (spec.options or {}).items():
            control.addItem(text, raw)
        i = control.findData(value)
        if i >= 0:
            control.setCurrentIndex(i)
        control.currentIndexChanged.connect(lambda _i, s=spec, c=control: self._on_value_changed(s, c.currentData()))
        layout.addWidget(control)
        if spec.key == "LOW_VOLTAGE_CUTOFF":
            control.currentIndexChanged.connect(self._update_conditional_visibility)

        self._rows[spec.offset] = (spec, control, None, cell)
        return cell

    def _build_numeric_cell(self, spec: FieldSpec, settings: bytes) -> QWidget:
        # The slider operates over the raw EEPROM byte domain, not display
        # units - display "step" sizes don't always evenly divide the raw
        # byte's actual resolution (e.g. TIMING_ADVANCE), so a display-unit
        # slider could skip achievable raw values. Raw-domain sliders can
        # always reach every byte, exactly like the old typeable spin box
        # could (just via drag/arrow-keys instead of typing digits).
        raw_min = encode_field(spec, spec.min_display)
        raw_max = encode_field(spec, spec.max_display)
        raw = settings[spec.offset]
        is_disabled = spec.kind == "number_or_disabled" and spec.disabled_at_raw is not None and raw >= spec.disabled_at_raw

        cell = QWidget()
        layout = QVBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._make_label_row(spec))

        slider = _ClickToWheelSlider(Qt.Orientation.Horizontal)
        slider.setFixedHeight(20)
        slider.setRange(raw_min, raw_max)
        slider.setValue(raw_max if is_disabled else raw)
        slider.setEnabled(not is_disabled)
        layout.addWidget(slider)

        value_row = QHBoxLayout()
        value_row.setContentsMargins(0, 0, 0, 0)
        value_label = QLabel(format_field_value(spec, settings))
        value_label.setStyleSheet(f"color: {TEXT_MUTED};")
        value_row.addWidget(value_label)
        value_row.addStretch(1)

        disabled_check = None
        if spec.kind == "number_or_disabled":
            disabled_check = QCheckBox(i18n.tr("config_disabled_checkbox"))
            disabled_check.setChecked(is_disabled)
            value_row.addWidget(disabled_check)
        layout.addLayout(value_row)

        slider.valueChanged.connect(lambda v, s=spec, lbl=value_label: self._on_slider_changed(s, lbl, v))
        if disabled_check is not None:
            disabled_check.toggled.connect(
                lambda checked, s=spec, sl=slider, lbl=value_label: self._on_disabled_toggled(s, sl, lbl, checked)
            )

        self._rows[spec.offset] = (spec, slider, disabled_check, cell)
        return cell

    # ---- edits flow live into the working buffer ------------------------------------------------

    def _on_value_changed(self, spec: FieldSpec, value) -> None:
        if self._working_config is None:
            return
        self._working_config[spec.offset] = encode_field(spec, value)
        self._mark_dirty()

    def _on_slider_changed(self, spec: FieldSpec, value_label: QLabel, raw_value: int) -> None:
        if self._working_config is None:
            return
        self._working_config[spec.offset] = raw_value
        value_label.setText(format_field_value(spec, bytes(self._working_config)))
        self._mark_dirty()

    def _on_disabled_toggled(self, spec: FieldSpec, slider: QSlider, value_label: QLabel, checked: bool) -> None:
        slider.setEnabled(not checked)
        if self._working_config is None:
            return
        self._working_config[spec.offset] = spec.disabled_at_raw if checked else slider.value()
        value_label.setText(format_field_value(spec, bytes(self._working_config)))
        self._mark_dirty()

    def _mark_dirty(self) -> None:
        if not self._dirty:
            self._dirty = True
            self._refresh_source_label()

    def _update_conditional_visibility(self, *_args) -> None:
        if self._working_config is None:
            return
        settings = bytes(self._working_config)
        for offset, (spec, _control, _disabled_check, row_widget) in self._rows.items():
            if spec.visible_when is not None:
                row_widget.setVisible(spec.visible_when(settings))

    # ---- save / apply ------------------------------------------------

    def _on_save_file_clicked(self) -> None:
        self.save_via_dialog()

    def save_via_dialog(self) -> bool:
        """Runs the full Save-to-file flow (file picker, write, change
        summary). Returns whether a file was actually written - used both by
        the Save button and by MainWindow's exit-confirmation "Save" choice,
        so a cancelled file picker or a write failure can correctly abort
        closing the window instead of silently discarding the edits."""
        if self._working_config is None:
            return False
        downloads = str(Path.home() / "Downloads")
        path, _ = QFileDialog.getSaveFileName(
            self, i18n.tr("config_save_to_file"), downloads, i18n.tr("binary_config_filter")
        )
        if not path:
            return False
        diff = self.diff_text()
        try:
            Path(path).write_bytes(bytes(self._working_config))
        except OSError as e:
            self._source_label.setText(i18n.tr("config_save_failed", error=e))
            self._source_label.setStyleSheet(f"color: {ACCENT_ERROR};")
            return False
        self._dirty = False
        self._original_config = bytes(self._working_config)
        self._source_desc = i18n.tr("config_working_from_file", path=path)
        self._refresh_source_label()
        self.file_loaded.emit(path)
        QMessageBox.information(
            self, i18n.tr("config_save_summary_title"), i18n.tr("config_save_summary_body", path=path, diff=diff)
        )
        return True

    def _on_apply_clicked(self) -> None:
        if self._working_config is None or len(self._working_config) != LAYOUT_SIZE:
            return
        if self.has_unsaved_changes():
            diff = self.diff_text()
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle(i18n.tr("config_apply_confirm_title"))
            box.setText(i18n.tr("config_apply_confirm_body", diff=diff))
            write_btn = box.addButton(i18n.tr("config_apply_confirm_yes"), QMessageBox.ButtonRole.AcceptRole)
            box.addButton(i18n.tr("config_apply_confirm_cancel"), QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(write_btn)
            box.exec()
            if box.clickedButton() is not write_btn:
                return
            self._dirty = False
            self._original_config = bytes(self._working_config)
            self._refresh_source_label()
        self.apply_requested.emit(bytes(self._working_config))

    # ---- activity log (fed by MainWindow._append_log - see that method) ---

    def append_log(self, level: str, text: str) -> None:
        color = _LOG_COLORS.get(level, TEXT_MUTED)
        self._activity_log.appendHtml(f'<span style="color:{color};">{text}</span>')
