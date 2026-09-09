"""A single ESC status card: index, colored circle, one-line status text,
and (when available) a hover tooltip with full firmware/bootloader details -
matching the level of detail am32.ca/configurator shows per ESC.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QLabel, QVBoxLayout

from . import i18n
from .models import EscInfo
from .theme import ACCENT_AMBER, ACCENT_ERROR, ACCENT_GREEN, TEXT_MUTED

_STATE_COLORS = {
    "idle": TEXT_MUTED,
    "probing": ACCENT_AMBER,
    "ok": ACCENT_GREEN,
    "diff": ACCENT_AMBER,
    "blank": ACCENT_AMBER,
    "error": ACCENT_ERROR,
}

_STATE_KEYS = {
    "idle": "state_idle",
    "probing": "state_probing",
    "ok": "state_ok",
    "diff": "state_diff",
    "blank": "state_blank",
    "error": "state_error",
}


def _build_tooltip(info: EscInfo) -> str:
    lines = []

    mcu_value = info.mcu_family
    if info.mcu_type and info.mcu_type != info.mcu_family:
        mcu_value = f"{info.mcu_family} ({info.mcu_type})"
    lines.append(i18n.tr("info_mcu", value=mcu_value))

    if info.firmware_name or info.firmware_version:
        fw = info.firmware_name or "?"
        if info.firmware_version:
            fw = f"{fw} v{info.firmware_version}"
        lines.append(i18n.tr("info_firmware", value=fw))

    if info.boot_pin or info.boot_version is not None:
        boot = info.boot_pin or "?"
        if info.boot_version is not None:
            boot = f"{boot}, v{info.boot_version}"
        lines.append(i18n.tr("info_bootloader", value=boot))

    if info.eeprom_version is not None:
        lines.append(i18n.tr("info_eeprom", value=info.eeprom_version))

    yes, no = i18n.tr("yes"), i18n.tr("no")
    lines.append(i18n.tr("info_reversed", value=yes if info.reversed else no))
    lines.append(i18n.tr("info_3d_mode", value=yes if info.bidirectional else no))

    return "\n".join(lines)


class EscStatusWidget(QFrame):
    def __init__(self, index: int, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("escCard")
        self.index = index
        self._state = "idle"
        self._detail = ""
        self._esc_info: Optional[EscInfo] = None

        layout = QVBoxLayout(self)

        self._title = QLabel()
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setStyleSheet("font-weight: 600;")

        self._dot = QLabel("●")
        self._dot.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._detail_label = QLabel()
        self._detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._detail_label.setWordWrap(True)
        self._detail_label.setStyleSheet(f"color: {TEXT_MUTED};")

        layout.addWidget(self._title)
        layout.addWidget(self._dot)
        layout.addWidget(self._detail_label)

        self.set_state("idle", "")
        i18n.signals.language_changed.connect(self._on_language_changed)

    def set_state(self, state: str, detail: str, esc_info: Optional[EscInfo] = None) -> None:
        self._state = state
        self._detail = detail
        self._esc_info = esc_info

        self._title.setText(i18n.tr("esc_card_title", n=self.index + 1))
        color = _STATE_COLORS.get(state, TEXT_MUTED)
        self._dot.setStyleSheet(f"color: {color}; font-size: 22px;")
        state_key = _STATE_KEYS.get(state)
        self._detail_label.setText(detail or (i18n.tr(state_key) if state_key else state))

        self.setToolTip(_build_tooltip(esc_info) if esc_info is not None else "")

    def _on_language_changed(self, _code: str) -> None:
        self.set_state(self._state, self._detail, self._esc_info)
