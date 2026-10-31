"""Vertical per-motor faders plus an "all motors" master fader, shared by the motor-run tab and the FC-flash tab.

The faders only say WHERE the operator wants each motor (a target in raw MSP units, 1000-2000). They never talk to
the serial port: the owner connects `target_requested` to MotorTestManager.manual_set_target, which keeps the motor
at that value after the fader is released and slows it down along the down curve when the fader is pulled down.
New values go through a small mailbox that is flushed every FADER_FLUSH_MS, so a mouse firing hundreds of moves per
second cannot flood the motor thread's queue.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QSlider, QVBoxLayout, QWidget

from .motor_ramp import MSP_MAX, MSP_MIN, msp_to_percent
from .theme import TEXT_MUTED

FADER_FLUSH_MS = 30
_FADER_HEIGHT = 90
_FADER_WIDTH = 22


class MotorFaders(QWidget):
    target_requested = pyqtSignal(int, int)  # motor index, raw MSP target

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sliders: list[QSlider] = []
        self._labels: list[QLabel] = []
        self._pending: dict[int, int] = {}
        self._syncing = False  # True while the code (not the operator) moves a handle
        self._active = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(FADER_FLUSH_MS)
        self._timer.timeout.connect(self._flush)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._row = QHBoxLayout()
        self._row.setSpacing(10)
        outer.addLayout(self._row)

        master_row = QHBoxLayout()
        title = QLabel("Всі мотори:")
        title.setStyleSheet("font-weight: 700;")
        master_row.addWidget(title)
        self._master = QSlider(Qt.Orientation.Horizontal)
        self._master.setRange(MSP_MIN, MSP_MAX)
        self._master.setValue(MSP_MIN)
        self._master.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._master.setToolTip("Рухає всі повзунки разом. Мотори тримаються, де залишили; вниз - плавно по спадній кривій.")
        self._master.valueChanged.connect(self._on_master_changed)
        master_row.addWidget(self._master, 1)
        self._master_label = QLabel("0%")
        self._master_label.setFixedWidth(50)
        self._master_label.setStyleSheet("font-weight: 700;")
        master_row.addWidget(self._master_label)
        outer.addLayout(master_row)

        self.set_count(0)

    # ---- public API ----------------------------------------------------------------------------

    @property
    def sliders(self) -> list[QSlider]:
        return self._sliders

    def set_count(self, count: int) -> None:
        """Rebuild exactly `count` faders (0 = a placeholder) - on connect, once the real motor count is known."""
        while self._row.count():
            widget = self._row.takeAt(0).widget()
            if widget is not None:
                widget.hide()  # deleteLater() alone leaves it painted until the next loop iteration
                widget.deleteLater()
        self._sliders.clear()
        self._labels.clear()
        self._pending.clear()
        if count == 0:
            placeholder = QLabel("Немає даних — підключіться, щоб побачити мотори FC.")
            placeholder.setStyleSheet(f"color: {TEXT_MUTED};")
            self._row.addWidget(placeholder)
            self._row.addStretch(1)
        else:
            for i in range(count):
                self._row.addWidget(self._build_column(i))
            self._row.addStretch(1)
        self._set_master(MSP_MIN)
        self._apply_enabled()

    def set_active(self, active: bool) -> None:
        self._active = active
        self._apply_enabled()

    def show_values(self, values: list[int], mirror: bool) -> None:
        """Real motor values from the manager. Labels always follow; the handles only when `mirror` (an automatic run
        drives them) - in manual mode they are the controls and must stay where the operator put them."""
        self._syncing = True
        try:
            for slider, label, v in zip(self._sliders, self._labels, values):
                if mirror and not slider.isSliderDown():
                    slider.setValue(v)
                label.setText(f"{msp_to_percent(v):.1f}%")
            if mirror and values:
                self._set_master(max(values))
        finally:
            self._syncing = False

    def zero(self) -> None:
        """Стоп / panic key: the manager spins the motors down itself, the handles go to 0."""
        self._syncing = True
        try:
            for slider in self._sliders:
                slider.setValue(MSP_MIN)
            self._set_master(MSP_MIN)
        finally:
            self._syncing = False
        self._pending.clear()

    # ---- internals -----------------------------------------------------------------------------

    def _build_column(self, index: int) -> QWidget:
        column = QWidget()
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        name = QLabel(f"ESC {index + 1}")
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(name)
        slider = QSlider(Qt.Orientation.Vertical)
        slider.setObjectName("motorFader")  # theme.py: opens the vertical travel the base QSlider rule would clamp to 20px
        slider.setRange(MSP_MIN, MSP_MAX)
        slider.setValue(MSP_MIN)
        slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        slider.setFixedSize(_FADER_WIDTH, _FADER_HEIGHT)
        # valueChanged (not sliderMoved) also catches a click on the track. Releasing does NOT stop the motor.
        slider.valueChanged.connect(lambda v, i=index: self._on_fader_changed(i, v))
        layout.addWidget(slider, alignment=Qt.AlignmentFlag.AlignHCenter)
        label = QLabel("0%")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        self._sliders.append(slider)
        self._labels.append(label)
        return column

    def _apply_enabled(self) -> None:
        for slider in self._sliders:
            slider.setEnabled(self._active)
        self._master.setEnabled(self._active and bool(self._sliders))

    def _set_master(self, value: int) -> None:
        was, self._syncing = self._syncing, True
        try:
            self._master.setValue(value)
            self._master_label.setText(f"{msp_to_percent(value):.0f}%")
        finally:
            self._syncing = was

    def _on_fader_changed(self, index: int, value: int) -> None:
        if self._syncing:
            return
        self._pending[index] = value
        if not self._timer.isActive():
            self._timer.start()
        self._set_master(max(s.value() for s in self._sliders))

    def _on_master_changed(self, value: int) -> None:
        if self._syncing:
            return
        self._master_label.setText(f"{msp_to_percent(value):.0f}%")
        for slider in self._sliders:
            slider.setValue(value)  # each handle reports itself through _on_fader_changed

    def _flush(self) -> None:
        pending, self._pending = self._pending, {}
        for index, value in pending.items():
            self.target_requested.emit(index, value)
