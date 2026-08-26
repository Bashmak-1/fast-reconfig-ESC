"""A manually-correctable running total, shown next to the automatic
flashed/already-OK/errors breakdown on both Хотспот tabs.

The automatic breakdown reflects only what the state machine itself
observed each round, which can't cover every real-world situation an
operator runs into (a re-seated ESC re-flashed by hand, a miscount, a bench
restart mid-batch). This widget is the operator's OWN tally instead: it
still auto-increments by exactly one at the moment a flash actually
succeeds (see increment()), but stays freely correctable with +/- or by
typing a number directly, since only the operator watching the bench knows
when the automatic count and reality have drifted apart.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QIntValidator
from PyQt6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QWidget

MIN_VALUE = 0
MAX_VALUE = 100_000


def clamp_counter(value: int) -> int:
    """Pure clamp to [MIN_VALUE, MAX_VALUE], unit-tested without Qt."""
    return max(MIN_VALUE, min(MAX_VALUE, value))


class ManualCounterWidget(QWidget):
    value_changed = pyqtSignal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._value = 0

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._minus_btn = QPushButton("-")
        self._minus_btn.setFixedSize(28, 28)
        self._minus_btn.setStyleSheet("font-size: 14px; font-weight: 700; padding: 0px;")
        self._minus_btn.clicked.connect(lambda: self.set_value(self._value - 1))
        layout.addWidget(self._minus_btn)

        # QIntValidator with bottom=0 already rejects a leading "-" and any
        # non-digit keystroke outright, so no separate input filtering is
        # needed to satisfy "digits only, never negative".
        self._edit = QLineEdit(str(self._value))
        self._edit.setFixedWidth(64)
        self._edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._edit.setValidator(QIntValidator(MIN_VALUE, MAX_VALUE, self._edit))
        self._edit.editingFinished.connect(self._on_edit_finished)
        layout.addWidget(self._edit)

        self._plus_btn = QPushButton("+")
        self._plus_btn.setFixedSize(28, 28)
        self._plus_btn.setStyleSheet("font-size: 14px; font-weight: 700; padding: 0px;")
        self._plus_btn.clicked.connect(lambda: self.set_value(self._value + 1))
        layout.addWidget(self._plus_btn)

    def value(self) -> int:
        return self._value

    def set_value(self, value: int) -> None:
        clamped = clamp_counter(value)
        self._edit.setText(str(clamped))
        if clamped == self._value:
            return
        self._value = clamped
        self.value_changed.emit(clamped)

    def increment(self, delta: int = 1) -> None:
        self.set_value(self._value + delta)

    def reset(self) -> None:
        self.set_value(0)

    def _on_edit_finished(self) -> None:
        text = self._edit.text().strip()
        try:
            value = int(text) if text else 0
        except ValueError:
            value = 0
        self.set_value(value)
