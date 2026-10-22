import hashlib

import pytest

from esc_hotline import updater_core as core
from esc_hotline.updater_cli import main as updater_main
from esc_hotline.main import main as app_main
from esc_hotline._version import __version__


def test_parse_version_accepts_tag_prefix():
    assert core.parse_version("v1.2.3")[:3] == (1, 2, 3)
    assert core.parse_version("EscHotline 10.0.1\n")[:3] == (10, 0, 1)
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


def test_dev_versions_sort_between_stable_releases():
    assert core.is_newer("1.2.0-dev.1", "1.1.9")
    assert core.is_newer("1.2.0", "1.2.0-dev.9")  # stable beats its own dev builds
    assert core.is_newer("1.2.0-dev.10", "1.2.0-dev.9")
    assert not core.is_newer("1.2.0-dev.1", "1.2.0")


def test_channel_of_and_dirs():
    assert core.channel_of("1.2.0-dev.3") == "dev" and core.channel_of("1.2.0") == "stable"
    assert core.channel_dir("dev").parent == core.channel_dir("stable").parent == core.ROOT_DIR


def test_list_releases_splits_channels_and_dev_picks_newest(monkeypatch):
    data = [
        {"tag_name": "v1.2.0-dev.2", "prerelease": True, "assets": []},
        {"tag_name": "v1.2.0-dev.3", "prerelease": True, "assets": []},
        {"tag_name": "v1.1.1", "prerelease": False, "assets": []},
        {"tag_name": "v1.1.2", "prerelease": False, "draft": True, "assets": []},
    ]
    monkeypatch.setattr(core, "_get_json", lambda url, timeout: data)
    assert [r.tag for r in core.list_releases("stable")] == ["v1.1.1"]
    assert core.fetch_latest(channel="dev").version == "1.2.0-dev.3"


def test_exe_version_keeps_dev_suffix(monkeypatch):
    import subprocess

    class R:
        stdout = "EscHotline 1.2.0-dev.7"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    assert core.exe_version("x") == "1.2.0-dev.7"


def test_updater_installs_into_channel_folder_and_migrates_legacy(tmp_path, monkeypatch):
    from esc_hotline import updater_cli

    monkeypatch.setattr(core, "ROOT_DIR", tmp_path)
    (tmp_path / "EscHotline.exe").write_bytes(b"flat-old")  # pre-channel layout
    (tmp_path / "EscHotlineUpdater.exe").write_bytes(b"flat-old-updater")
    rel = core.Release("1.2.0-dev.4", {}, tag="v1.2.0-dev.4")
    monkeypatch.setattr(core, "fetch_latest", lambda channel="stable", **k: rel)
    monkeypatch.setattr(core, "fetch_verified", lambda r, name, dest, progress=None: dest.write_bytes(b"new-" + name.encode()))
    monkeypatch.setattr(updater_cli.subprocess, "Popen", lambda *a, **k: None)
    shortcuts = []
    monkeypatch.setattr(updater_cli, "create_desktop_shortcut", lambda t, name="EscHotline": shortcuts.append((t, name)) or True)
    monkeypatch.setattr(updater_cli, "_own_channel", lambda: None)

    assert updater_main(["--channel", "dev", "--yes"]) == 0
    assert (tmp_path / "dev" / "EscHotline.exe").read_bytes() == b"new-EscHotline.exe"
    assert shortcuts == [(tmp_path / "dev" / "EscHotline.exe", "EscHotline Dev")]
    assert (tmp_path / "EscHotline.exe").exists()  # a dev install never touches the old flat/stable files

    # old flat app asking to update itself -> moved into stable/, flat leftovers removed
    monkeypatch.setattr(core, "fetch_latest", lambda channel="stable", **k: core.Release("1.3.0", {}, tag="v1.3.0"))
    assert updater_main(["--yes", "--target", str(tmp_path / "EscHotline.exe")]) == 0
    assert (tmp_path / "stable" / "EscHotline.exe").exists()
    assert not (tmp_path / "EscHotline.exe").exists() and not (tmp_path / "EscHotlineUpdater.exe").exists()


def _legacy_home(tmp_path, monkeypatch):
    from esc_hotline import settings_store as ss

    (tmp_path / "settings.ini").write_text("[General]\nlast_config_path=C:/cfg/a.bin\nfoo=bar\n", encoding="utf-8")
    monkeypatch.setattr(core, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(ss, "SHARED_FILE", tmp_path / "shared.ini")
    monkeypatch.setattr(ss, "_LEGACY_FILE", tmp_path / "settings.ini")
    return ss


def test_old_flat_settings_carry_over_to_stable(tmp_path, monkeypatch):
    ss = _legacy_home(tmp_path, monkeypatch)
    monkeypatch.setattr(ss, "CHANNEL", "stable")
    monkeypatch.setattr(ss, "SETTINGS_DIR", tmp_path / "stable")
    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "stable" / "settings.ini")
    assert ss.load_str("foo") == "bar"
    assert ss.load_last_config_path() == "C:/cfg/a.bin"  # falls back to the carried-over value
    ss.save_last_config_path("C:/cfg/b.bin")
    assert ss.load_last_config_path() == "C:/cfg/b.bin"
    assert (tmp_path / "settings.ini").read_text(encoding="utf-8").count("a.bin") == 1  # old file left intact


def test_dev_seeds_from_stable_but_never_writes_back(tmp_path, monkeypatch):
    ss = _legacy_home(tmp_path, monkeypatch)
    (tmp_path / "stable").mkdir()
    (tmp_path / "stable" / "settings.ini").write_text("[General]\nfoo=stable-value\n", encoding="utf-8")
    monkeypatch.setattr(ss, "CHANNEL", "dev")
    monkeypatch.setattr(ss, "SETTINGS_DIR", tmp_path / "dev")
    monkeypatch.setattr(ss, "SETTINGS_FILE", tmp_path / "dev" / "settings.ini")
    assert ss.load_str("foo") == "stable-value"
    ss.save_str("foo", "dev-experiment")
    assert "stable-value" in (tmp_path / "stable" / "settings.ini").read_text(encoding="utf-8")


def test_remove_legacy_flat_survives_a_locked_exe(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(core, "ROOT_DIR", tmp_path)
    for n in (core.MAIN_EXE, core.UPDATER_EXE):
        (tmp_path / n).write_bytes(b"x")
    real_unlink = Path.unlink

    def locked(self, missing_ok=False):
        if self.name == core.UPDATER_EXE:
            raise PermissionError("in use")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked)
    core.remove_legacy_flat()
    assert not (tmp_path / core.MAIN_EXE).exists() and (tmp_path / core.UPDATER_EXE).exists()


def test_updater_relaunches_the_app_when_the_update_fails(tmp_path, monkeypatch):
    from esc_hotline import updater_cli

    app = tmp_path / "EscHotline.exe"
    app.write_bytes(b"old")
    monkeypatch.setattr(core, "ROOT_DIR", tmp_path / "root")
    monkeypatch.setattr(core, "fetch_latest", lambda **k: (_ for _ in ()).throw(OSError("offline")))
    started = []
    monkeypatch.setattr(updater_cli.subprocess, "Popen", lambda cmd, **k: started.append(cmd))
    monkeypatch.setattr(updater_cli, "_launched_new", False)
    assert updater_main(["--yes", "--wait-pid", "999999", "--target", str(app)]) == 1
    assert started == [[str(app)]]  # the app that closed itself for the update is brought back


def test_updater_tag_installs_an_older_version_over_a_newer_one(tmp_path, monkeypatch):
    from esc_hotline import updater_cli

    monkeypatch.setattr(core, "ROOT_DIR", tmp_path)
    exe = tmp_path / "stable" / "EscHotline.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"v1.1.3")
    old = core.Release("1.1.1", {}, tag="v1.1.1")
    monkeypatch.setattr(core, "list_releases", lambda ch, *a, **k: [old] if ch == "stable" else [])
    monkeypatch.setattr(core, "exe_version", lambda p: "1.1.3")
    monkeypatch.setattr(core, "fetch_verified", lambda r, name, dest, progress=None: dest.write_bytes(b"old-" + name.encode()))
    monkeypatch.setattr(updater_cli.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(updater_cli, "create_desktop_shortcut", lambda *a, **k: True)
    monkeypatch.setattr(updater_cli, "_launched_new", False)
    assert updater_main(["--yes", "--tag", "v1.1.1"]) == 0
    assert exe.read_bytes() == b"old-EscHotline.exe"  # a downgrade is allowed when explicitly requested
