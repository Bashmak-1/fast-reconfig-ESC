"""Tab "Телеметрія ESC": live view of the optional ID2BIT telemetry (see esc_telemetry.py).

Fed by the "Прогон мотором" tab's connection - nothing here opens a port of its own. The CSV recording of a
run happens in the poller regardless of this tab; the tab only shows the values.
"""
from __future__ import annotations

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView, QHeaderView, QCheckBox, QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from .esc_telemetry import TELEMETRY_DIR, CommandSnapshot, EscHeader, EscRecord
from .motor_ramp import msp_to_percent

COLUMNS = ("ESC", "ID", "План %", "Відпр. %", "RPM", "eRPM", "В", "А", "°C", "мАг", "Кадри", "Вік", "dt мс", "Стан")


class EscTelemetryPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)

        self._sources: dict[str, tuple[bool, str]] = {}
        self._status = QLabel("Підключіться на вкладці «Прогон мотором» - тут з'явиться стан телеметрії ESC.")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        row = QHBoxLayout()
        self.live_checkbox = QCheckBox("Показувати наживо (опитування й без прогону моторів)")
        self.live_checkbox.setEnabled(False)
        row.addWidget(self.live_checkbox)
        row.addStretch(1)
        open_btn = QPushButton("Відкрити теку з CSV")
        open_btn.clicked.connect(self._open_folder)
        row.addWidget(open_btn)
        layout.addLayout(row)

        self._table = QTableWidget(0, len(COLUMNS))
        self._table.setHorizontalHeaderLabels(COLUMNS)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table, 1)

        self._counters = QLabel("")
        layout.addWidget(self._counters)
        hint = QLabel("Під час прогону моторів кожні ~30 мс пишеться CSV: план за кривою, реально відправлене значення, "
                      "RPM та електрика кожного ESC. Підсумок прогону - у технічному журналі.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #7f8c8d;")
        layout.addWidget(hint)

    def on_available(self, source: str, available: bool, detail: str) -> None:
        """`source` is the tab whose connection reported it ("Прогон мотором" / "Прошивка FC")."""
        self._sources[source] = (available, detail)
        available = any(a for a, _ in self._sources.values())
        detail = "; ".join(f"{s}: {d}" for s, (a, d) in self._sources.items() if a)
        self.live_checkbox.setEnabled(available)
        if not available:
            self.live_checkbox.setChecked(False)
            self._table.setRowCount(0)
            self._counters.setText("")
        self._status.setText(
            f"✅ Прошивка FC з телеметрією ESC (ID2BIT): {detail}." if available
            else "ℹ Телеметрія ESC недоступна: не підключено або звичайна прошивка FC (без ID2BIT). Прогін моторів працює як завжди."
        )

    def on_telemetry(self, header: EscHeader, records: list[EscRecord], snap: CommandSnapshot | None) -> None:
        if self._table.rowCount() != len(records):
            self._table.setRowCount(len(records))
        for i, r in enumerate(records):
            planned = f"{msp_to_percent(snap.planned[i]):.1f}" if snap and i < len(snap.planned) else "—"
            sent = f"{msp_to_percent(snap.sent[i]):.1f}" if snap and i < len(snap.sent) else "—"
            cells = (i + 1, r.id, planned, sent, r.rpm, r.erpm, f"{r.voltage_v:.2f}", f"{r.current_a:.2f}", r.temp_c,
                     r.consumption_mah, r.frames, r.age, f"{r.dt_us / 1000:.1f}", "немає даних" if r.stale else "ОК")
            for col, value in enumerate(cells):
                item = self._table.item(i, col)
                if item is None:
                    item = QTableWidgetItem()
                    self._table.setItem(i, col, item)
                item.setText(str(value))
        self._counters.setText(f"Валідних кадрів: {header.valid} | CRC-помилок: {header.crc_errors} | "
                               f"ресинхронізацій: {header.resync} | bad_id: {header.bad_id}")

    @staticmethod
    def _open_folder() -> None:
        TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(TELEMETRY_DIR)))
