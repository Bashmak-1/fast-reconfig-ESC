"""EscHotlineUpdater.exe: console tool that installs/updates EscHotline.exe from
GitHub Releases into ~/.esc_hotline (wherever the updater itself was started
from), and puts a desktop shortcut to it. Qt-free on purpose (see updater_core)."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import updater_core as core
from ._version import __version__

EXIT_UPDATE_AVAILABLE = 10  # --check only: distinguishes "update waiting" from "up to date" (0)


def _ps_quote(path: Path) -> str:
    return str(path).replace("'", "''")


def create_desktop_shortcut(target: Path) -> bool:
    script = (
        "$d=[Environment]::GetFolderPath('Desktop');"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$d\\EscHotline.lnk\");"
        f"$s.TargetPath='{_ps_quote(target)}';$s.WorkingDirectory='{_ps_quote(target.parent)}';$s.Save()"
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
    p.add_argument("--target", type=Path, help=f"куди ставити {core.MAIN_EXE} (за замовчуванням - {core.INSTALL_DIR})")
    p.add_argument("--shortcut", action="store_true", help="створити ярлик на робочому столі, навіть якщо оновлення не потрібне")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"EscHotlineUpdater {__version__}")
        return 0
    code = _run(args)
    if args.wait_pid is None and not args.check and sys.stdin and sys.stdin.isatty():
        try:
            input("\nНатисніть Enter, щоб закрити це вікно...")  # started by double-click: don't vanish with the result
        except (EOFError, OSError):
            pass
    return code


def _run(args: argparse.Namespace) -> int:
    target = args.target or core.INSTALL_DIR / core.MAIN_EXE
    print("[1/5] Перевірка оновлень...")
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
    if current and not args.yes and args.wait_pid is None and sys.stdin and sys.stdin.isatty():
        if input("Оновити? [y/N] ").strip().lower() not in ("y", "yes", "т", "так"):
            return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    new_path = target.with_name(target.name + ".new")
    updater_dest = target.parent / core.UPDATER_EXE
    updater_new = updater_dest.with_name(updater_dest.name + ".new")
    # The updater copy in the install dir is what the app itself keeps fresh later; skip it when
    # this very process is that copy (a running exe can't overwrite itself).
    install_updater = Path(sys.executable).resolve() != updater_dest.resolve()
    try:
        print(f"[2/5] Завантаження {core.MAIN_EXE} + перевірка SHA256...")
        core.fetch_verified(release, core.MAIN_EXE, new_path, _progress)
        print()
        if install_updater:
            print(f"[3/5] Завантаження {core.UPDATER_EXE}...")
            core.fetch_verified(release, core.UPDATER_EXE, updater_new, _progress)
            print()

        if args.wait_pid is not None:
            print("  чекаю закриття програми...")
            if not core.wait_for_pid(args.wait_pid):
                print("Програма не закрилась за 60 с - оновлення скасовано.")
                new_path.unlink(missing_ok=True)
                updater_new.unlink(missing_ok=True)
                return 1
        print(f"[4/5] Встановлення в {target.parent} ...")
        olds = [core.replace_file(new_path, target)]
        if install_updater:
            olds.append(core.replace_file(updater_new, updater_dest))
    except Exception as e:
        print(f"\nПомилка оновлення: {e}")
        new_path.unlink(missing_ok=True)
        updater_new.unlink(missing_ok=True)
        return 1

    print("[5/5] Ярлик на робочому столі...")
    print("  готово" if create_desktop_shortcut(target) else "  не вдалось створити ярлик (не критично)")
    print(f"Готово: {core.MAIN_EXE} {release.version}. Запускаю...")
    subprocess.Popen([str(target)], cwd=str(target.parent))
    for old in olds:
        try:
            old.unlink(missing_ok=True)
        except OSError:
            pass  # still locked for a moment; replace_file() clears the leftover next time
    return 0


if __name__ == "__main__":
    sys.exit(main())
