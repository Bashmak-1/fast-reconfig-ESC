"""GUI side of the auto-update flow: background check on startup / on the
Settings "check" button, the Update / Later / Skip prompt, and handing the
actual install off to EscHotlineUpdater.exe (a running exe can't replace
itself on Windows, so the updater does the swap after this app exits)."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget

from . import i18n
from . import updater_core as core
from ._version import __version__
from .file_logger import get_logger
from .settings_store import load_str, save_str

_SKIPPED_KEY = "skipped_update_version"
CHANNEL = core.channel_of(__version__)  # which release stream this very build belongs to
OTHER_CHANNEL = "dev" if CHANNEL == "stable" else "stable"
CREATE_NEW_CONSOLE = 0x00000010


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_dir() -> Path:
    return Path(sys.executable).parent


def is_legacy_flat() -> bool:
    """Running from the pre-channel layout (exe directly in ~/.esc_hotline) and due to be moved into stable/."""
    return is_frozen() and CHANNEL == "stable" and install_dir().resolve() == core.ROOT_DIR.resolve()


class UpdateController(QObject):
    # (release | None, error text | "", manual) - emitted from the worker thread, delivered queued on the GUI thread
    _checked = pyqtSignal(object, str, bool)
    check_finished = pyqtSignal()

    def __init__(self, parent_widget: QWidget) -> None:
        super().__init__(parent_widget)
        self._parent = parent_widget
        self._busy = False
        self._checked.connect(self._on_checked)

    def check(self, manual: bool) -> None:
        if self._busy:
            return
        self._busy = True
        threading.Thread(target=self._worker, args=(manual,), daemon=True).start()

    # ---- worker thread (no Qt widgets here) ----

    def _worker(self, manual: bool) -> None:
        flog = get_logger()
        release: Optional[core.Release] = None
        error = ""
        try:
            release = core.fetch_latest(channel=CHANNEL)
            if is_frozen():
                self._sync_updater(release)
                if CHANNEL == "stable" and install_dir() == core.channel_dir("stable"):
                    core.remove_legacy_flat()  # leftovers of the old flat layout, e.g. the updater that migrated us
        except Exception as e:  # network down, rate limit, bad JSON, ...
            flog.info("Update check failed: %s", e)
            error = str(e)
        self._checked.emit(release, error, manual)

    def _sync_updater(self, release: core.Release) -> None:
        """Keep EscHotlineUpdater.exe present and current. It is not running
        while this app is, so this app can overwrite it - the mirror of the
        updater replacing this app."""
        flog = get_logger()
        updater = install_dir() / core.UPDATER_EXE
        have = core.exe_version(updater) if updater.exists() else None
        if have and not core.is_newer(release.version, have):
            return
        if core.UPDATER_EXE not in release.assets:
            return
        flog.info("Updater %s -> %s", have or "missing", release.version)
        new = updater.with_name(updater.name + ".new")
        try:
            core.fetch_verified(release, core.UPDATER_EXE, new)
            core.replace_file(new, updater)
            updater.with_name(updater.name + ".old").unlink(missing_ok=True)
        except Exception as e:
            flog.warning("Updater refresh failed: %s", e)
            new.unlink(missing_ok=True)

    # ---- GUI thread ----

    def _on_checked(self, release: Optional[core.Release], error: str, manual: bool) -> None:
        self._busy = False
        self.check_finished.emit()
        if release is None:
            if manual:
                QMessageBox.warning(self._parent, i18n.tr("update_title"), i18n.tr("update_check_failed", error=error))
            return
        if is_legacy_flat():
            # Online (the check succeeded) and still in the old flat folder: move into stable/ now.
            # The updater installs the latest stable there, relaunches it, and clears the flat leftovers.
            QMessageBox.information(self._parent, i18n.tr("update_title"), i18n.tr("migrate_notice"))
            self._launch_updater()
            return
        if not core.is_newer(release.version, __version__):
            if manual:
                QMessageBox.information(
                    self._parent, i18n.tr("update_title"), i18n.tr("update_up_to_date", version=__version__)
                )
            return
        if not manual and load_str(_SKIPPED_KEY) == release.version:
            return
        self._prompt(release)

    def _prompt(self, release: core.Release) -> None:
        box = QMessageBox(self._parent)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(i18n.tr("update_title"))
        box.setText(i18n.tr("update_available", new=release.version, current=__version__))
        update_btn = box.addButton(i18n.tr("update_now"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(i18n.tr("update_later"), QMessageBox.ButtonRole.RejectRole)
        skip_btn = box.addButton(i18n.tr("update_skip"), QMessageBox.ButtonRole.DestructiveRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is skip_btn:
            save_str(_SKIPPED_KEY, release.version)
        elif clicked is update_btn:
            self._launch_updater()

    def _launch_updater(self) -> None:
        updater = install_dir() / core.UPDATER_EXE
        if not is_frozen() or not updater.exists():
            QMessageBox.information(self._parent, i18n.tr("update_title"), i18n.tr("update_dev_build"))
            return
        subprocess.Popen(
            [str(updater), "--wait-pid", str(os.getpid()), "--yes", "--channel", CHANNEL, "--target", sys.executable],
            cwd=str(updater.parent),
            creationflags=CREATE_NEW_CONSOLE,
        )
        QApplication.quit()

    # ---- the other channel (dev <-> stable), installed side by side on request ----

    def other_exe(self) -> Path:
        return core.channel_dir(OTHER_CHANNEL) / core.MAIN_EXE

    def install_other(self) -> None:
        """Download/update the other channel's build. This app keeps running: it is a separate
        folder and exe, so nothing here can touch the build the user is currently in."""
        updater = install_dir() / core.UPDATER_EXE
        if not is_frozen() or not updater.exists():
            QMessageBox.information(self._parent, i18n.tr("update_title"), i18n.tr("update_dev_build"))
            return
        subprocess.Popen(
            [str(updater), "--channel", OTHER_CHANNEL], cwd=str(updater.parent), creationflags=CREATE_NEW_CONSOLE
        )

    def launch_other(self) -> None:
        exe = self.other_exe()
        if exe.exists():
            subprocess.Popen([str(exe)], cwd=str(exe.parent))
