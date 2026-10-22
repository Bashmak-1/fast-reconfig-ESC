"""Stdlib-only update logic, shared by the GUI (checking) and
EscHotlineUpdater.exe (installing). Must never import PyQt6 - the updater exe
is deliberately tiny and Qt-free."""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

REPO = "Bashmak-1/fast-reconfig-ESC"
# env override exists only so the update flow can be tested against a local fake server
API_LATEST = os.environ.get("ESC_HOTLINE_API_URL", f"https://api.github.com/repos/{REPO}/releases/latest")
MAIN_EXE = "EscHotline.exe"
UPDATER_EXE = "EscHotlineUpdater.exe"
SUMS_FILE = "SHA256SUMS.txt"
# Fixed home of both exes (and the logs/settings the app already keeps here), so the
# updater can be run from anywhere and still installs into the right place.
INSTALL_DIR = Path.home() / ".esc_hotline"

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass
class Release:
    version: str
    assets: dict[str, str]  # asset name -> download url


def parse_version(text: str) -> tuple[int, int, int]:
    m = _VERSION_RE.search(text)
    if not m:
        raise ValueError(f"not a version: {text!r}")
    return int(m[1]), int(m[2]), int(m[3])


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def fetch_latest(timeout: float = 5.0, url: str = API_LATEST) -> Release:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "EscHotline"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}
    return Release(version=data["tag_name"].lstrip("v"), assets=assets)


def parse_sums(text: str) -> dict[str, str]:
    """`sha256sum` format: '<hex>  <name>' (a leading '*' on the name is the binary-mode marker)."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            sums[parts[1].strip().lstrip("*")] = parts[0].lower()
    return sums


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, progress: Optional[Callable[[int, int], None]] = None, timeout: float = 30.0) -> None:
    """progress(bytes_done, bytes_total); total is 0 when the server omits Content-Length."""
    req = urllib.request.Request(url, headers={"User-Agent": "EscHotline"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while chunk := resp.read(1 << 16):
            out.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)


def fetch_verified(release: Release, name: str, dest: Path, progress: Optional[Callable[[int, int], None]] = None) -> None:
    """Download release asset `name` to `dest` and check it against the
    release's SHA256SUMS.txt; deletes `dest` and raises ValueError on mismatch."""
    url, sums_url = release.assets.get(name), release.assets.get(SUMS_FILE)
    if not url or not sums_url:
        raise ValueError(f"у релізі немає {name} або {SUMS_FILE}")
    download(url, dest, progress)
    sums_path = dest.with_name(SUMS_FILE + ".tmp")
    try:
        download(sums_url, sums_path)
        expected = parse_sums(sums_path.read_text(encoding="utf-8")).get(name)
    finally:
        sums_path.unlink(missing_ok=True)
    if expected != sha256_of(dest):
        dest.unlink(missing_ok=True)
        raise ValueError("контрольна сума не збігається - файл пошкоджено")


def exe_version(path: Path) -> Optional[str]:
    """Ask an installed exe for its version via its own --version, so there is
    no separate version file that could drift from the real binary."""
    import subprocess

    try:
        out = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=15).stdout
        return ".".join(map(str, parse_version(out)))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def replace_file(new: Path, target: Path) -> Path:
    """Swap `new` in place of `target`, keeping the old file as `<target>.old`.
    A running exe can't be deleted or overwritten on Windows but CAN be renamed,
    so the old one is moved aside first. Rolls back if the swap fails midway."""
    old = target.with_name(target.name + ".old")
    if old.exists():
        old.unlink()
    had_target = target.exists()
    if had_target:
        os.replace(target, old)
    try:
        os.replace(new, target)
    except OSError:
        if had_target:
            os.replace(old, target)
        raise
    return old


def wait_for_pid(pid: int, timeout_s: float = 60.0) -> bool:
    """True once the process is gone (or never existed), False on timeout."""
    if os.name == "nt":
        import ctypes

        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return True
        try:
            return k32.WaitForSingleObject(handle, int(timeout_s * 1000)) == 0
        finally:
            k32.CloseHandle(handle)
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    return False
