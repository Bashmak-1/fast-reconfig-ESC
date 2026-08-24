"""Dark QSS theme approximating am32.ca/configurator's palette."""

BG_MAIN = "#0a0e1a"
BG_PANEL = "#12172a"
ACCENT_RED = "#c0392b"
ACCENT_RED_HOVER = "#e74c3c"
ACCENT_GREEN = "#2ecc71"
ACCENT_AMBER = "#e67e22"
ACCENT_ERROR = "#e74c3c"
TEXT_MAIN = "#e6e6e6"
TEXT_MUTED = "#8a93a6"
BORDER = "#232a45"

QSS = f"""
QMainWindow, QWidget {{
    background-color: {BG_MAIN};
    color: {TEXT_MAIN};
    font-family: "Segoe UI", sans-serif;
    font-size: 13px;
}}
QFrame#topBar, QFrame#configRow, QFrame#escCard, QFrame#logPanel {{
    background-color: {BG_PANEL};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}
QPushButton {{
    background-color: {BG_PANEL};
    color: {TEXT_MAIN};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 6px 14px;
}}
QPushButton:hover:!disabled {{
    border-color: {ACCENT_RED_HOVER};
}}
QPushButton:disabled {{
    color: {TEXT_MUTED};
}}
QPushButton#connectButton {{
    background-color: {ACCENT_RED};
    color: white;
    font-weight: 600;
    border: none;
}}
QPushButton#connectButton:hover:!disabled {{
    background-color: {ACCENT_RED_HOVER};
}}
QComboBox, QLineEdit, QPlainTextEdit {{
    background-color: {BG_MAIN};
    color: {TEXT_MAIN};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 6px;
}}
QCheckBox {{
    spacing: 6px;
}}
QCheckBox::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {BORDER};
    border-radius: 3px;
    background-color: {BG_MAIN};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT_RED};
    border-color: {ACCENT_RED};
}}
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 12px;
    font-weight: 600;
    padding-top: 14px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    color: {TEXT_MAIN};
}}
QSlider::groove:horizontal {{
    height: 4px;
    background: {BORDER};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT_RED};
    border-radius: 2px;
}}
QSlider::add-page:horizontal {{
    background: {BORDER};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {ACCENT_RED};
    border: 2px solid {BG_MAIN};
    width: 14px;
    height: 14px;
    margin: -6px 0;
    border-radius: 8px;
}}
QSlider::handle:horizontal:hover {{
    background: {ACCENT_RED_HOVER};
}}
QSlider:disabled::sub-page:horizontal, QSlider:disabled::handle:horizontal {{
    background: {TEXT_MUTED};
}}
"""
