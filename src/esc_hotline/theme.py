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
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    background-color: {BG_MAIN};
    top: -1px;
}}
QTabBar::tab {{
    background-color: {BG_PANEL};
    color: {TEXT_MUTED};
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 8px 16px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background-color: {BG_MAIN};
    color: {TEXT_MAIN};
    font-weight: 600;
    border-color: {ACCENT_RED};
}}
QTabBar::tab:!selected:hover {{
    color: {TEXT_MAIN};
    border-color: {ACCENT_RED_HOVER};
}}
QTabBar::tab:disabled {{
    color: {TEXT_MUTED};
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
    padding: 0 6px;
    color: {TEXT_MAIN};
}}
QSlider {{
    background: {BORDER};
    outline: none;
    min-height: 20px;
    max-height: 20px;
}}
QSlider:focus {{
    outline: none;
    border: none;
}}
QSlider::groove:horizontal {{
    height: 4px;
    background: transparent;
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT_RED};
    border-radius: 2px;
    margin: 0px;
}}
QSlider::add-page:horizontal {{
    background: {BORDER};
    border-radius: 2px;
    margin: 0px;
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
QSlider::groove:vertical {{
    width: 4px;
    background: transparent;
    border-radius: 2px;
}}
QSlider::sub-page:vertical {{
    background: {ACCENT_RED};
    border-radius: 2px;
    margin: 0px;
}}
QSlider::add-page:vertical {{
    background: {BORDER};
    border-radius: 2px;
    margin: 0px;
}}
QSlider::handle:vertical {{
    background: {ACCENT_RED};
    border: 2px solid {BG_MAIN};
    width: 14px;
    height: 14px;
    margin: 0px -6px;
    border-radius: 8px;
}}
QSlider::handle:vertical:hover {{
    background: {ACCENT_RED_HOVER};
}}
QSlider:disabled::sub-page:vertical, QSlider:disabled::handle:vertical {{
    background: {TEXT_MUTED};
}}
QSlider#motorFader {{
    min-height: 0px;
    max-height: 16777215px;
    min-width: 0px;
    max-width: 16777215px;
}}
QProgressBar {{
    background-color: {BG_MAIN};
    border: 1px solid {BORDER};
    border-radius: 4px;
    text-align: center;
    color: {TEXT_MAIN};
}}
QProgressBar::chunk {{
    background-color: {ACCENT_RED};
    border-radius: 3px;
}}
"""
