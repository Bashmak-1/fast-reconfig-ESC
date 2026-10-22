"""Settings window (opened by the gear button in the top bar): language,
current app version, and the manual "check for updates" button."""
from __future__ import annotations

from PyQt6.QtCore import QSize
from PyQt6.QtWidgets import QComboBox, QDialog, QFormLayout, QLabel, QPushButton, QVBoxLayout

from . import i18n
from ._version import __version__
from .flag_icons import FLAG_ICON_BUILDERS
from .update_controller import UpdateController


class SettingsDialog(QDialog):
    def __init__(self, controller: UpdateController, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(360)
        self._controller = controller

        self._lang_label = QLabel()
        self._lang_combo = QComboBox()
        self._lang_combo.setIconSize(QSize(22, 15))
        for code, _flag, label, _name in i18n.LANGUAGES:
            self._lang_combo.addItem(FLAG_ICON_BUILDERS[code](), label, code)
        self._lang_combo.currentIndexChanged.connect(self._on_language_selected)

        self._version_label = QLabel()
        self._version_value = QLabel(__version__)
        self._check_btn = QPushButton()
        self._check_btn.clicked.connect(self._on_check_clicked)
        controller.check_finished.connect(self._on_check_finished)

        form = QFormLayout()
        form.addRow(self._lang_label, self._lang_combo)
        form.addRow(self._version_label, self._version_value)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._check_btn)

        i18n.signals.language_changed.connect(self._retranslate)
        self._retranslate()

    def _on_language_selected(self, index: int) -> None:
        code = self._lang_combo.itemData(index)
        if code:
            i18n.set_language(code)

    def _on_check_clicked(self) -> None:
        self._check_btn.setEnabled(False)
        self._check_btn.setText(i18n.tr("update_checking"))
        self._controller.check(manual=True)

    def _on_check_finished(self) -> None:
        self._check_btn.setEnabled(True)
        self._check_btn.setText(i18n.tr("update_check_button"))

    def _retranslate(self, *_args) -> None:
        self.setWindowTitle(i18n.tr("settings_title"))
        self._lang_label.setText(i18n.tr("language_label"))
        self._version_label.setText(i18n.tr("version_label"))
        if self._check_btn.isEnabled():
            self._check_btn.setText(i18n.tr("update_check_button"))
        idx = [c for c, *_ in i18n.LANGUAGES].index(i18n.current_language())
        if self._lang_combo.currentIndex() != idx:
            self._lang_combo.blockSignals(True)
            self._lang_combo.setCurrentIndex(idx)
            self._lang_combo.blockSignals(False)
