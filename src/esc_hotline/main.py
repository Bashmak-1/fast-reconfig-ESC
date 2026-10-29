"""Application entry point."""
from __future__ import annotations

import argparse
import os
import sys

from ._version import __version__


def _cli_write(text: str) -> None:
    """The exe is built --windowed (no console), so sys.stdout is None there.
    Write straight to the inherited stdout handle (pipe/file), or attach to the
    parent's console when launched from cmd/PowerShell."""
    if sys.stdout is not None:
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    k32.GetStdHandle.restype = wintypes.HANDLE
    k32.CreateFileW.restype = wintypes.HANDLE
    handle = k32.GetStdHandle(-11)
    if not handle or handle == wintypes.HANDLE(-1).value:
        k32.AttachConsole(-1)  # ATTACH_PARENT_PROCESS
        handle = k32.CreateFileW("CONOUT$", 0x40000000, 3, None, 3, 0, None)  # GENERIC_WRITE, share rw, OPEN_EXISTING
    written = wintypes.DWORD(0)
    if k32.GetConsoleMode(handle, ctypes.byref(wintypes.DWORD())):  # real console: UTF-16 so Cyrillic survives any code page
        k32.WriteConsoleW(handle, text, len(text), ctypes.byref(written), None)
    else:  # pipe/file
        data = text.encode("utf-8")
        k32.WriteFile(handle, data, len(data), ctypes.byref(written), None)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="EscHotline", description="EscHotline - офлайн-прошивальник AM32-ESC.", add_help=False)
    p.add_argument("-h", "--help", action="store_true", help="показати цю довідку й вийти")
    p.add_argument("-v", "--version", action="store_true", help="показати версію й вийти")
    return p


def _install_crash_hooks() -> None:
    """PyQt6 aborts the whole process on an unhandled exception inside a slot - log it and carry on instead,
    and dump native crashes (access violations) to a file next to the log."""
    import faulthandler

    from .file_logger import LOG_DIR, get_logger

    def hook(exc_type, exc, tb) -> None:
        get_logger().error("Unhandled exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = hook
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        faulthandler.enable(open(LOG_DIR / "crash.log", "a", encoding="utf-8"))  # kept open for the process lifetime
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        parser = build_parser()
        args = parser.parse_args(argv)
        if args.help:
            _cli_write(parser.format_help())
            return 0
        if args.version:
            _cli_write(f"EscHotline {__version__}\n")
            return 0

    from .session_gate import require_session
    if not require_session():
        return 1

    from PyQt6.QtWidgets import QApplication

    from .main_window import MainWindow
    from .theme import QSS

    _install_crash_hooks()
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
