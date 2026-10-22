"""EscHotlineUpdater.exe: console tool that installs/updates EscHotline.exe from
GitHub Releases into ~/.esc_hotline/<channel> (stable or dev, each in its own folder;
wherever the updater itself was started from), and puts a desktop shortcut to it.
Qt-free on purpose (see updater_core)."""
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


def create_desktop_shortcut(target: Path, name: str = "EscHotline") -> bool:
    script = (
        "$d=[Environment]::GetFolderPath('Desktop');"
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$d\\{name}.lnk\");"
        f"$s.TargetPath='{_ps_quote(target)}';$s.WorkingDirectory='{_ps_quote(target.parent)}';$s.Save()"
    )
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", script], timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


_last_percent = -1
_launched_new = False  # set once the freshly installed app has been started


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
    p.add_argument("--channel", choices=core.CHANNELS, help="stable (за замовчуванням) або dev")
    p.add_argument("--tag", help="поставити саме цю версію (напр. v1.1.0), навіть старішу")
    p.add_argument("--target", type=Path, help=f"куди ставити {core.MAIN_EXE} (за замовчуванням - {core.ROOT_DIR}/<канал>)")
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
    if args.wait_pid is not None and not _launched_new and args.target and args.target.exists():
        # The app closed itself for us; if nothing new got installed (error, nothing to do) bring it back.
        subprocess.Popen([str(args.target)], cwd=str(args.target.parent))
    if args.wait_pid is None and not args.check and sys.stdin and sys.stdin.isatty():
        try:
            input("\nНатисніть Enter, щоб закрити це вікно...")  # started by double-click: don't vanish with the result
        except (EOFError, OSError):
            pass
    return code


def _own_channel() -> Optional[str]:
    """The channel this updater copy sits in (~/.esc_hotline/<channel>/), if any."""
    here = Path(sys.executable).resolve().parent
    return here.name if here.parent == core.ROOT_DIR.resolve() and here.name in core.CHANNELS else None


def _find_release(args: argparse.Namespace, channel: str) -> core.Release:
    if not args.tag:
        return core.fetch_latest(channel=channel)
    wanted = args.tag if args.tag.startswith("v") else "v" + args.tag
    for ch in core.CHANNELS:
        for r in core.list_releases(ch):
            if r.tag == wanted:
                return r
    raise ValueError(f"релізу {wanted} не знайдено")


def _run(args: argparse.Namespace) -> int:
    channel = args.channel or (core.channel_of(args.tag) if args.tag else _own_channel() or "stable")
    target = args.target or core.channel_dir(channel) / core.MAIN_EXE
    legacy_dir: Optional[Path] = None
    if target.parent.resolve() == core.ROOT_DIR.resolve():
        # Old flat layout (exes directly in ~/.esc_hotline): move into the channel folder.
        legacy_dir, target = target.parent, core.channel_dir(channel) / core.MAIN_EXE
    shortcut_name = "EscHotline" if channel == "stable" else "EscHotline Dev"
    print("[1/5] Перевірка оновлень...")
    try:
        release = _find_release(args, channel)
    except Exception as e:
        print(f"Не вдалось отримати реліз з GitHub: {e}")
        return 1

    current = core.exe_version(target) if target.exists() else None
    up_to_date = current == release.version if args.tag else bool(current) and not core.is_newer(release.version, current)
    if up_to_date and legacy_dir is None:
        print(f"Встановлена версія {current} актуальна (на GitHub {release.version}).")
        if args.shortcut:
            create_desktop_shortcut(target, shortcut_name)
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
    print("  готово" if create_desktop_shortcut(target, shortcut_name) else "  не вдалось створити ярлик (не критично)")
    if legacy_dir is not None:  # the calling app has exited; the flat updater (this process?) is cleaned up by the new app later
        core.remove_legacy_flat()
    print(f"Готово: {core.MAIN_EXE} {release.version}. Запускаю...")
    subprocess.Popen([str(target)], cwd=str(target.parent))
    global _launched_new
    _launched_new = True
    for old in olds:
        try:
            old.unlink(missing_ok=True)
        except OSError:
            pass  # still locked for a moment; replace_file() clears the leftover next time
    return 0


if __name__ == "__main__":
    sys.exit(main())
