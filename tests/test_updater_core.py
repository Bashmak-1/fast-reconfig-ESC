import hashlib

import pytest

from esc_hotline import updater_core as core
from esc_hotline.updater_cli import main as updater_main
from esc_hotline.main import main as app_main
from esc_hotline._version import __version__


def test_parse_version_accepts_tag_prefix():
    assert core.parse_version("v1.2.3") == (1, 2, 3)
    assert core.parse_version("EscHotline 10.0.1\n") == (10, 0, 1)
    with pytest.raises(ValueError):
        core.parse_version("dev")


def test_is_newer_compares_numerically_not_lexically():
    assert core.is_newer("1.10.0", "1.9.9")
    assert not core.is_newer("1.0.1", "1.0.1")
    assert not core.is_newer("1.0.0", "1.0.1")


def test_parse_sums_handles_binary_marker():
    text = "AB12  EscHotline.exe\ncd34 *EscHotlineUpdater.exe\n"
    assert core.parse_sums(text) == {"EscHotline.exe": "ab12", "EscHotlineUpdater.exe": "cd34"}


def test_replace_file_keeps_old_and_swaps(tmp_path):
    target, new = tmp_path / "a.exe", tmp_path / "a.exe.new"
    target.write_bytes(b"old")
    new.write_bytes(b"new")
    old = core.replace_file(new, target)
    assert target.read_bytes() == b"new" and old.read_bytes() == b"old" and not new.exists()


def test_replace_file_rolls_back_when_swap_fails(tmp_path):
    target = tmp_path / "a.exe"
    target.write_bytes(b"old")
    with pytest.raises(OSError):
        core.replace_file(tmp_path / "missing.new", target)
    assert target.read_bytes() == b"old"


def test_fetch_verified_rejects_bad_checksum(tmp_path, monkeypatch):
    def fake_download(url, dest, progress=None, timeout=30.0):
        dest.write_bytes(b"payload" if "exe" in url else f"{'0' * 64}  EscHotline.exe\n".encode())

    monkeypatch.setattr(core, "download", fake_download)
    rel = core.Release("1.0.0", {"EscHotline.exe": "http://x/exe", core.SUMS_FILE: "http://x/sums"})
    dest = tmp_path / "EscHotline.exe.new"
    with pytest.raises(ValueError):
        core.fetch_verified(rel, "EscHotline.exe", dest)
    assert not dest.exists()


def test_fetch_verified_accepts_good_checksum(tmp_path, monkeypatch):
    good = hashlib.sha256(b"payload").hexdigest()

    def fake_download(url, dest, progress=None, timeout=30.0):
        dest.write_bytes(b"payload" if "exe" in url else f"{good}  EscHotline.exe\n".encode())

    monkeypatch.setattr(core, "download", fake_download)
    rel = core.Release("1.0.0", {"EscHotline.exe": "http://x/exe", core.SUMS_FILE: "http://x/sums"})
    dest = tmp_path / "EscHotline.exe.new"
    core.fetch_verified(rel, "EscHotline.exe", dest)
    assert dest.read_bytes() == b"payload"


def test_both_clis_report_version(capsys):
    assert updater_main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"EscHotlineUpdater {__version__}"
    assert app_main(["-v"]) == 0
    assert capsys.readouterr().out.strip() == f"EscHotline {__version__}"
