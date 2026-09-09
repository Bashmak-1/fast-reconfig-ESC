"""Owns the EscManager on a dedicated QThread so serial I/O never blocks the GUI."""
from __future__ import annotations

from PyQt6.QtCore import QThread

from .esc_manager import EscManager


class ConnectionWorker:
    def __init__(self) -> None:
        self.thread = QThread()
        self.thread.setObjectName("EscManagerThread")
        self.manager = EscManager()
        self.manager.moveToThread(self.thread)
        self.thread.start()

    def stop(self) -> None:
        self.thread.quit()
        self.thread.wait(2000)
