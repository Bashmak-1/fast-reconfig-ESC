"""Головна tab block "Прошивка ESC": choose the AM32 .hex (where from) and the ESCs to write it to (where to).

Plain Ukrainian literals like the other newer tabs. The write itself is EscManager.flash_firmware(); this
panel only validates the file against every probed ESC and asks for confirmation.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
    QVBoxLayout,
)

from .esc_firmware import HexError, compatibility, load_firmware
from .mcu_profiles import MCU_VARIANTS
from .settings_store import load_str, save_str

_PATH_KEY = "esc_firmware_path"
_ICON = {"ok": "✅", "warn": "⚠", "block": "⛔"}


class EscFirmwarePanel(QFrame):
    flash_requested = pyqtSignal(str, object, bool)  # path, ESC indices, allow other board

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("configRow")
        self._connected = False
        self._busy = False
        self._infos: dict[int, object] = {}  # index -> EscInfo of every probed ESC
        self._checks: dict[int, QCheckBox] = {}
        self._verdicts: dict[int, str] = {}

        outer = QVBoxLayout(self)
        title = QLabel("Прошивка ESC (код AM32, не налаштування) - тільки для обраних ESC, з перевіркою після запису")
        title.setStyleSheet("font-weight: 700;")
        outer.addWidget(title)

        row = QHBoxLayout()
        self._path = QLineEdit(load_str(_PATH_KEY))
        self._path.setPlaceholderText("Файл прошивки AM32 (.hex)")
        self._path.setReadOnly(True)
        row.addWidget(self._path, 1)
        browse = QPushButton("Обрати .hex…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        outer.addLayout(row)

        self._info = QLabel("")
        self._info.setWordWrap(True)
        outer.addWidget(self._info)

        self._targets = QVBoxLayout()
        outer.addLayout(self._targets)

        bottom = QHBoxLayout()
        self._other_board = QCheckBox("Дозволити іншу назву плати (лише якщо впевнені)")
        self._other_board.toggled.connect(lambda *_: self._update_button())
        bottom.addWidget(self._other_board)
        bottom.addStretch(1)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setVisible(False)
        bottom.addWidget(self._progress, 1)
        self._btn = QPushButton("Прошити обрані ESC")
        self._btn.clicked.connect(self._on_flash)
        bottom.addWidget(self._btn)
        outer.addLayout(bottom)
        self._refresh()

    # ---- state from MainWindow ----
    def set_connected(self, connected: bool, esc_count: int) -> None:
        self._connected = connected
        self._infos.clear()
        self._rebuild(esc_count if connected else 0)
        self._refresh()

    def set_esc_info(self, index: int, esc_info) -> None:
        if esc_info is not None:
            self._infos[index] = esc_info
            self._refresh()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._progress.setVisible(busy)
        if not busy:
            self._progress.setValue(0)
        self._update_button()

    def on_progress(self, index: int, done: int, total: int) -> None:
        self._progress.setFormat(f"ESC {index + 1}: %p%")
        self._progress.setValue(int(done * 100 / max(total, 1)))

    # ---- internals ----
    def _rebuild(self, count: int) -> None:
        for cb in self._checks.values():
            self._targets.removeWidget(cb)
            cb.deleteLater()
        self._checks.clear()
        for i in range(count):
            cb = QCheckBox(f"ESC {i + 1}")
            cb.setEnabled(False)
            cb.toggled.connect(lambda *_: self._update_button())
            self._targets.addWidget(cb)
            self._checks[i] = cb

    @staticmethod
    def _variant(esc_info):
        return next((v for v in MCU_VARIANTS.values() if v.name == esc_info.mcu_family), None)

    def _refresh(self) -> None:
        path = self._path.text()
        self._verdicts.clear()
        first = None
        for i, cb in self._checks.items():
            info = self._infos.get(i)
            if info is None:
                cb.setText(f"ESC {i + 1} - не знайдено")
                cb.setEnabled(False)
                cb.setChecked(False)
                continue
            text = f"ESC {i + 1} - {info.mcu_family}, зараз: {info.firmware_name or '?'} {info.firmware_version or ''}"
            verdict = "block"
            variant = self._variant(info)
            if not path:
                text += " - оберіть файл"
            elif variant is None or variant.firmware_start is None:
                text += " - ⛔ запис прошивки для цього MCU не підтримується"
            else:
                try:
                    fw = load_firmware(path, variant)
                    verdict, why = compatibility(fw, info.firmware_name)
                    first = first or fw
                    text += f" - {_ICON[verdict]} {why}"
                except HexError as e:
                    text += f" - ⛔ {e}"
            self._verdicts[i] = verdict
            cb.setText(text)
            cb.setEnabled(verdict != "block")
            if verdict == "block":
                cb.setChecked(False)
        if first is not None:
            self._info.setText(f"Файл: прошивка «{first.name or 'без назви'}», {first.size} байт коду.")
        else:
            self._info.setText("" if not path else "Файл ще не підійшов жодному знайденому ESC.")
        self._update_button()

    def _chosen(self) -> list[int]:
        return [i for i, cb in self._checks.items() if cb.isChecked()]

    def _update_button(self) -> None:
        chosen = self._chosen()
        needs_override = any(self._verdicts.get(i) == "warn" for i in chosen)
        self._btn.setEnabled(
            self._connected and not self._busy and bool(chosen) and (not needs_override or self._other_board.isChecked())
        )

    def _browse(self) -> None:
        start = str(Path(self._path.text()).parent) if self._path.text() else str(Path.home() / "Downloads")
        path, _ = QFileDialog.getOpenFileName(self, "Прошивка ESC", start, "AM32 firmware (*.hex);;All files (*)")
        if path:
            self._path.setText(path)
            save_str(_PATH_KEY, path)
            self._refresh()

    def _on_flash(self) -> None:
        chosen = self._chosen()
        names = ", ".join(f"ESC {i + 1}" for i in chosen)
        answer = QMessageBox.warning(
            self, "Прошивка ESC",
            f"Записати {Path(self._path.text()).name} у: {names}?\n\nНе вимикайте живлення й не торкайтесь ESC до кінця "
            "(кілька десятків секунд на кожен). Якщо запис урветься, ESC лишиться в завантажувачі - прошийте ще раз.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.set_busy(True)
            self.flash_requested.emit(self._path.text(), chosen, self._other_board.isChecked())
