"""Compact "which ESCs already have this config" list for the Config tab -
same state stream (`EscManager.esc_status_changed`) the Main tab's big status
cards already consume, just rendered as a small table instead.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QAbstractItemView, QHeaderView, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

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


class EscComparisonList(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: dict[int, tuple] = {}  # index -> (state, detail)

        layout = QVBoxLayout(self)
        self._title = QLabel()
        self._title.setStyleSheet("font-weight: 600;")
        layout.addWidget(self._title)

        self._table = QTableWidget(0, 3)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._table, 1)

        i18n.signals.language_changed.connect(self._retranslate)
        self._retranslate()

    def _retranslate(self, *_args) -> None:
        self._title.setText(i18n.tr("comparison_title"))
        self._table.setHorizontalHeaderLabels(
            [i18n.tr("comparison_column_index"), i18n.tr("comparison_column_esc"), i18n.tr("comparison_column_status")]
        )
        self._render_all()

    def clear_all(self, count: int) -> None:
        self._rows = {i: ("idle", "") for i in range(count)}
        self._render_all()

    def set_row(self, index: int, state: str, detail: str) -> None:
        self._rows[index] = (state, detail)
        if index < self._table.rowCount():
            self._render_row(index, state, detail)

    def _render_all(self) -> None:
        self._table.setRowCount(len(self._rows))
        for index in sorted(self._rows):
            state, detail = self._rows[index]
            self._render_row(index, state, detail)

    def _render_row(self, index: int, state: str, detail: str) -> None:
        idx_item = QTableWidgetItem(str(index + 1))
        idx_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self._table.setItem(index, 0, idx_item)

        esc_item = QTableWidgetItem(detail or "-")
        self._table.setItem(index, 1, esc_item)

        state_key = _STATE_KEYS.get(state, "comparison_status_not_scanned")
        status_item = QTableWidgetItem("●  " + i18n.tr(state_key))
        status_item.setForeground(QColor(_STATE_COLORS.get(state, TEXT_MUTED)))
        self._table.setItem(index, 2, status_item)
