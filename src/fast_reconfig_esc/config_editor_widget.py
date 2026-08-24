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

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import i18n
from .eeprom_config import ConfigFileError, load_config_file
from .eeprom_fields import FieldSpec, GROUP_ORDER, applicable_specs, decode_field, encode_field
from .mcu_profiles import LAYOUT_SIZE
from .theme import ACCENT_ERROR, TEXT_MUTED


class ConfigEditorPage(QWidget):
    apply_requested = pyqtSignal(bytes)
    file_loaded = pyqtSignal(str)  # so MainWindow can sync the Main tab's active config path

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._working_config: Optional[bytearray] = None
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
        self._source_desc = source_desc
        self._dirty = False
        self._set_controls_enabled(True)
        self._rebuild_form()
        self._refresh_source_label()

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
            box = QGroupBox(i18n.tr(group_key))
            form = QFormLayout(box)
            for spec in specs:
                self._add_row(form, spec, settings)
            self._form_layout.insertWidget(insert_at, box)
            insert_at += 1

        self._update_conditional_visibility()

    def _add_row(self, form: QFormLayout, spec: FieldSpec, settings: bytes) -> None:
        value = decode_field(spec, settings)
        label_text = i18n.tr(spec.label_key)

        if spec.kind == "bool":
            control = QCheckBox()
            control.setChecked(bool(value))
            control.toggled.connect(lambda checked, s=spec: self._on_value_changed(s, checked))
            form.addRow(label_text, control)
            self._rows[spec.offset] = (spec, control, None, control)
            return

        if spec.kind == "enum":
            control = QComboBox()
            for raw, text in (spec.options or {}).items():
                control.addItem(text, raw)
            i = control.findData(value)
            if i >= 0:
                control.setCurrentIndex(i)
            control.currentIndexChanged.connect(lambda _i, s=spec, c=control: self._on_value_changed(s, c.currentData()))
            form.addRow(label_text, control)
            self._rows[spec.offset] = (spec, control, None, control)
            if spec.key == "LOW_VOLTAGE_CUTOFF":
                control.currentIndexChanged.connect(self._update_conditional_visibility)
            return

        # number / number_or_disabled
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)

        spin = QDoubleSpinBox()
        spin.setRange(spec.min_display, spec.max_display)
        spin.setSingleStep(spec.step)
        spin.setDecimals(spec.decimals if spec.decimals else (2 if spec.step < 1 else 0))
        if spec.unit:
            spin.setSuffix(f" {spec.unit}")

        disabled_check = None
        if spec.kind == "number_or_disabled":
            disabled_check = QCheckBox(i18n.tr("config_disabled_checkbox"))
            is_disabled = value is None
            disabled_check.setChecked(is_disabled)
            spin.setEnabled(not is_disabled)
            spin.setValue(float(value) if value is not None else spec.min_display)
            disabled_check.toggled.connect(lambda checked, s=spec, sp=spin: self._on_disabled_toggled(s, sp, checked))
            row_layout.addWidget(disabled_check)
        else:
            spin.setValue(float(value) if value is not None else spec.min_display)

        spin.valueChanged.connect(lambda v, s=spec, dc=disabled_check: self._on_spin_changed(s, v, dc))
        row_layout.addWidget(spin, 1)
        form.addRow(label_text, row)
        self._rows[spec.offset] = (spec, spin, disabled_check, row)

    # ---- edits flow live into the working buffer ------------------------------------------------

    def _on_value_changed(self, spec: FieldSpec, value) -> None:
        if self._working_config is None:
            return
        self._working_config[spec.offset] = encode_field(spec, value)
        self._mark_dirty()

    def _on_spin_changed(self, spec: FieldSpec, value: float, disabled_check: Optional[QCheckBox]) -> None:
        if disabled_check is not None and disabled_check.isChecked():
            return  # value is "disabled" - the checkbox handler owns the raw byte, not the (greyed-out) spin box
        self._on_value_changed(spec, value)

    def _on_disabled_toggled(self, spec: FieldSpec, spin: QDoubleSpinBox, checked: bool) -> None:
        spin.setEnabled(not checked)
        self._on_value_changed(spec, None if checked else spin.value())

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
        if self._working_config is None:
            return
        downloads = str(Path.home() / "Downloads")
        path, _ = QFileDialog.getSaveFileName(
            self, i18n.tr("config_save_to_file"), downloads, i18n.tr("binary_config_filter")
        )
        if not path:
            return
        try:
            Path(path).write_bytes(bytes(self._working_config))
        except OSError as e:
            self._source_label.setText(i18n.tr("config_save_failed", error=e))
            self._source_label.setStyleSheet(f"color: {ACCENT_ERROR};")
            return
        self._dirty = False
        self._source_desc = i18n.tr("config_working_from_file", path=path)
        self._refresh_source_label()
        self.file_loaded.emit(path)

    def _on_apply_clicked(self) -> None:
        if self._working_config is None or len(self._working_config) != LAYOUT_SIZE:
            return
        self.apply_requested.emit(bytes(self._working_config))
