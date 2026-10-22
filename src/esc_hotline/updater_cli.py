"""EscHotlineUpdater.exe: console tool that installs the latest EscHotline.exe
from GitHub Releases next to itself. Qt-free on purpose (see updater_core)."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import updater_core as core
from ._version import __version__

EXIT_UPDATE_AVAILABLE = 10  # --check only: distinguishes "update waiting" from "up to date" (0)


def install_dir() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()


def create_desktop_shortcut(target: Path) -> bool:
    script = (
        "$d=[Environment]::GetFolderPath('Desktop');"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$d\\EscHotline.lnk\");"
        f"$s.TargetPath='{target}';$s.WorkingDirectory='{target.parent}';$s.Save()"
    )
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", script], timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


_last_percent = -1


def _progress(done: int, total: int) -> None:
    global _last_percent
    if total and done * 100 // total != _last_percent:
        _last_percent = done * 100 // total
        print(f"\r  {_last_percent:3d}%  ({done // 1024} / {total // 1024} KB)", end="", flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="EscHotlineUpdater", description="Оновлює EscHotline.exe з GitHub Releases.")
    p.add_argument("-v", "--version", action="store_true", help="показати версію й вийти")
    p.add_argument("--check", action="store_true", help="лише перевірити; код виходу 10 = є оновлення")
    p.add_argument("--yes", action="store_true", help="не питати підтвердження")
    p.add_argument("--wait-pid", type=int, help="дочекатись завершення процесу (основної програми) перед заміною")
    p.add_argument("--target", type=Path, help=f"шлях до {core.MAIN_EXE} (за замовчуванням - поруч з оновлювачем)")
    p.add_argument("--shortcut", action="store_true", help="створити ярлик на робочому столі")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"EscHotlineUpdater {__version__}")
        return 0

    target = args.target or install_dir() / core.MAIN_EXE
    print("[1/4] Перевірка оновлень...")
    try:
        release = core.fetch_latest()
    except Exception as e:
        print(f"Не вдалось звʼязатись з GitHub: {e}")
        return 1

    current = core.exe_version(target) if target.exists() else None
    if current and not core.is_newer(release.version, current):
        print(f"Встановлена версія {current} актуальна (на GitHub {release.version}).")
        if args.shortcut:
            create_desktop_shortcut(target)
        return 0
    print(f"Доступна версія {release.version} (зараз: {current or 'не встановлено'}).")
    if args.check:
        return EXIT_UPDATE_AVAILABLE
    if not args.yes and args.wait_pid is None and sys.stdin and sys.stdin.isatty():
        if input("Оновити? [y/N] ").strip().lower() not in ("y", "yes", "т", "так"):
            return 0

    new_path = target.with_name(target.name + ".new")
    try:
        print(f"[2/4] Завантаження {core.MAIN_EXE} + перевірка SHA256...")
        core.fetch_verified(release, core.MAIN_EXE, new_path, _progress)
        print()

        if args.wait_pid is not None:
            print("[3/4] Чекаю закриття програми...")
            if not core.wait_for_pid(args.wait_pid):
                print("Програма не закрилась за 60 с - оновлення скасовано.")
                new_path.unlink(missing_ok=True)
                return 1
        print("[4/4] Заміна файлу...")
        old = core.replace_file(new_path, target)
    except Exception as e:
        print(f"\nПомилка оновлення: {e}")
        new_path.unlink(missing_ok=True)
        return 1

    if args.shortcut or current is None:
        create_desktop_shortcut(target)
    print(f"Готово: {core.MAIN_EXE} {release.version}. Запускаю...")
    subprocess.Popen([str(target)], cwd=str(target.parent))
    try:
        old.unlink(missing_ok=True)
    except OSError:
        pass  # still locked for a moment; replace_file() clears the leftover next time
    return 0


if __name__ == "__main__":
    sys.exit(main())
