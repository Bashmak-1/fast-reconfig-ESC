"""Settings window (opened by the gear button in the top bar): language,
current app version, and the manual "check for updates" button."""
from __future__ import annotations

import threading

from PyQt6.QtCore import QSize, pyqtSignal
from PyQt6.QtWidgets import QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from . import i18n
from ._version import __version__
from .flag_icons import FLAG_ICON_BUILDERS
from . import updater_core as core
from .update_controller import CHANNEL, OTHER_CHANNEL, UpdateController


class SettingsDialog(QDialog):
    _other_version = pyqtSignal(str)  # from the probe thread; "" = not installed

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

        self._channel_label = QLabel()
        self._channel_value = QLabel()
        self._other_label = QLabel()
        self._other_status = QLabel()
        self._other_install_btn = QPushButton()
        self._other_install_btn.clicked.connect(self._controller.install_other)
        self._other_launch_btn = QPushButton()
        self._other_launch_btn.clicked.connect(self._controller.launch_other)
        self._other_version.connect(self._on_other_version)
        other_row = QHBoxLayout()
        other_row.addWidget(self._other_install_btn)
        other_row.addWidget(self._other_launch_btn)

        form = QFormLayout()
        form.addRow(self._lang_label, self._lang_combo)
        form.addRow(self._version_label, self._version_value)
        form.addRow(self._channel_label, self._channel_value)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._check_btn)
        layout.addSpacing(10)
        layout.addWidget(self._other_label)
        layout.addWidget(self._other_status)
        layout.addLayout(other_row)

        i18n.signals.language_changed.connect(self._retranslate)
        self._retranslate()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._other_status.setText(i18n.tr("other_checking"))
        threading.Thread(target=self._probe_other, daemon=True).start()

    def _probe_other(self) -> None:
        exe = self._controller.other_exe()
        self._other_version.emit((core.exe_version(exe) or "?") if exe.exists() else "")

    def _on_other_version(self, version: str) -> None:
        self._other_installed = version
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
        self._channel_label.setText(i18n.tr("channel_label"))
        self._channel_value.setText(i18n.tr(f"channel_{CHANNEL}"))
        other = i18n.tr(f"channel_{OTHER_CHANNEL}")
        self._other_label.setText(i18n.tr("other_title", channel=other))
        installed = getattr(self, "_other_installed", None)
        if installed is not None:
            self._other_status.setText(
                i18n.tr("other_installed", version=installed) if installed else i18n.tr("other_missing")
            )
        self._other_install_btn.setText(i18n.tr("other_update" if installed else "other_install", channel=other))
        self._other_launch_btn.setText(i18n.tr("other_launch", channel=other))
        self._other_launch_btn.setEnabled(bool(installed))
        if self._check_btn.isEnabled():
            self._check_btn.setText(i18n.tr("update_check_button"))
        idx = [c for c, *_ in i18n.LANGUAGES].index(i18n.current_language())
        if self._lang_combo.currentIndex() != idx:
            self._lang_combo.blockSignals(True)
            self._lang_combo.setCurrentIndex(idx)
            self._lang_combo.blockSignals(False)
