"""A single ESC status card: index, colored circle, one-line status text."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QLabel, QVBoxLayout

from . import i18n
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


class EscStatusWidget(QFrame):
    def __init__(self, index: int, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("escCard")
        self.index = index
        self._state = "idle"
        self._detail = ""

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

    def set_state(self, state: str, detail: str) -> None:
        self._state = state
        self._detail = detail
        self._title.setText(i18n.tr("esc_card_title", n=self.index + 1))
        color = _STATE_COLORS.get(state, TEXT_MUTED)
        self._dot.setStyleSheet(f"color: {color}; font-size: 22px;")
        state_key = _STATE_KEYS.get(state)
        self._detail_label.setText(detail or (i18n.tr(state_key) if state_key else state))

    def _on_language_changed(self, _code: str) -> None:
        self.set_state(self._state, self._detail)
