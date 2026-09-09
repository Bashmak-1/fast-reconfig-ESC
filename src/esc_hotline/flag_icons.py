"""Small flag icons drawn with QPainter.

Qt's widget text layout (QLabel/QPushButton/QComboBox) does not reliably
render color emoji (incl. flag sequences) on Windows regardless of font
choice - they fall back to plain two-letter boxes ("UA", "GB"). Drawing the
flags as tiny QIcons sidesteps that entirely and always looks right.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

ICON_SIZE = (24, 16)


def ukraine_flag_icon(size: tuple[int, int] = ICON_SIZE) -> QIcon:
    w, h = size
    pixmap = QPixmap(w, h)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#0057B7"))  # blue
    painter.drawRect(0, 0, w, h // 2)
    painter.setBrush(QColor("#FFD700"))  # yellow
    painter.drawRect(0, h // 2, w, h - h // 2)
    painter.end()
    return QIcon(pixmap)


def uk_flag_icon(size: tuple[int, int] = ICON_SIZE) -> QIcon:
    w, h = size
    pixmap = QPixmap(w, h)
    pixmap.fill(QColor("#00247D"))  # navy
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    pen = QPen(QColor("#FFFFFF"))
    pen.setWidth(max(2, h // 5))
    painter.setPen(pen)
    painter.drawLine(0, 0, w, h)
    painter.drawLine(w, 0, 0, h)

    pen = QPen(QColor("#CF142B"))  # red
    pen.setWidth(max(1, h // 8))
    painter.setPen(pen)
    painter.drawLine(0, 0, w, h)
    painter.drawLine(w, 0, 0, h)

    pen = QPen(QColor("#FFFFFF"))
    pen.setWidth(max(3, h // 3))
    painter.setPen(pen)
    painter.drawLine(w // 2, 0, w // 2, h)
    painter.drawLine(0, h // 2, w, h // 2)

    pen = QPen(QColor("#CF142B"))
    pen.setWidth(max(1, h // 5))
    painter.setPen(pen)
    painter.drawLine(w // 2, 0, w // 2, h)
    painter.drawLine(0, h // 2, w, h // 2)

    painter.end()
    return QIcon(pixmap)


FLAG_ICON_BUILDERS = {
    "en": uk_flag_icon,
    "uk": ukraine_flag_icon,
}
