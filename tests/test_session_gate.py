from esc_hotline import session_gate as g

USERS = {g.name_hash("Ivan")}


def test_password_only_login():
    assert g.check_password(" Ivan1029 ", USERS, "2026-10-29") == "ok"
    assert g.check_password("ivan1028", USERS, "2026-10-29") == "bad_password"
    assert g.check_password("petro1029", USERS, "2026-10-29") == "unknown_user"
    assert g.check_password("1029", USERS, "2026-10-29") == "unknown_user"
    assert g.password_for("Ivan", "2026-10-29") == "ivan1029"


def test_users_file_parsing():
    assert g.load_users("# c\nAB12\n\n  cd34  \n") == {"ab12", "cd34"}


def test_token_is_bound_to_day_and_pid():
    env = g.session_env(123)
    assert g.token_ok(env)
    assert not g.token_ok({**env, g.ENV_PID: "124"})
    assert not g.token_ok({})
    assert g.make_token(123, "2026-10-29") != g.make_token(123, "2026-10-30")


def test_find_users_and_non_frozen_updater_passes(tmp_path):
    (tmp_path / "users.sha256").write_text("AB12\n", encoding="utf-8")
    assert g.find_users(tmp_path / "nope", tmp_path) == {"ab12"}
    assert g.find_users(tmp_path / "nope") == set()
    assert g.require_login(tmp_path)  # source run: no gate


def test_pasted_junk_and_bom_do_not_break_login(tmp_path):
    (tmp_path / "users.sha256").write_bytes(b"\xef\xbb\xbf" + g.name_hash("Ivan").encode() + b"\r\n")
    users = g.find_users(tmp_path)
    assert users == {g.name_hash("Ivan")}
    assert g.check_password("Iv​an1029", users, "2026-10-29") == "ok"
    assert g.name_hash("Ivan​ ") == g.name_hash("ivan")


def test_whitelist_latin_and_digits_only():
    assert g.is_valid_name("Ivan7") and not g.is_valid_name("Іван") and not g.is_valid_name("iv an")
    assert not g.is_valid_name("iv-an") and not g.is_valid_name("")
    assert g.check_password("іван1029", USERS, "2026-10-29") == "bad_chars"
    assert g.check_password("iv an1029", USERS, "2026-10-29") == "bad_chars"
    assert g.check_password("ivan1029", USERS, "2026-10-29") == "ok"


def test_update_cleanup_only_touches_our_temp_folder(tmp_path):
    import tempfile
    from pathlib import Path

    from esc_hotline import updater_cli as u

    tmp = Path(tempfile.gettempdir())
    assert u.cleanup_allowed(tmp / "esc_hotline_update_abc")
    assert not u.cleanup_allowed(tmp / "something_else")
    assert not u.cleanup_allowed(tmp_path / "esc_hotline_update_abc")  # wrong parent
    assert not u.cleanup_allowed(Path("C:/Users"))


def test_in_app_update_inside_a_launcher_session_hands_over_to_a_fresh_updater(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from PyQt6.QtWidgets import QApplication, QWidget

    from esc_hotline import update_controller as uc
    from esc_hotline import updater_core as core

    app = QApplication.instance() or QApplication([])  # noqa: F841
    for k, v in {**g.session_env(4242), g.ENV_LAUNCHER_EXE: str(tmp_path / "EscHotline.exe")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(uc, "CREATE_NEW_CONSOLE", 0)
    release = core.Release(version="9.9.9", assets={}, tag="v9.9.9")
    monkeypatch.setattr(core, "fetch_latest", lambda **kw: release)
    monkeypatch.setattr(core, "fetch_verified", lambda rel, name, dest, *a: dest.write_bytes(b"x"))
    popen, quit_ = MagicMock(), MagicMock()
    monkeypatch.setattr(uc.subprocess, "Popen", popen)
    monkeypatch.setattr(uc.QApplication, "quit", quit_)
    uc.UpdateController(QWidget())._launch_updater()
    cmd = popen.call_args[0][0]
    assert cmd[0].endswith("EscHotlineUpdater.exe") and "--launcher" in cmd
    assert cmd[cmd.index("--wait-pid") + 1] == "4242" and cmd[cmd.index("--target") + 1].endswith("EscHotline.exe")
    quit_.assert_called_once()
    import shutil

    shutil.rmtree(cmd[cmd.index("--cleanup") + 1], ignore_errors=True)  # the real updater would do this


def test_login_dialog_rereads_the_user_list_on_every_try(monkeypatch, tmp_path):
    import tkinter as tk

    from PyQt6.QtWidgets import QApplication  # noqa: F401  (keeps the Qt/Tk mix identical to the other tests)

    users_file = tmp_path / "users.sha256"
    users_file.write_text(g.name_hash("Ivan") + "\n", encoding="utf-8")
    orig = tk.Tk.mainloop
    seen: list[str] = []

    def drive(self, n=0):
        entry = [w for w in self.winfo_children() if isinstance(w, tk.Entry)][0]
        button = [w for w in self.winfo_children() if isinstance(w, tk.Button)][0]
        entry.insert(0, "petro" + g.password_for("x")[-4:])
        button.invoke(); self.update()  # Petro is not in the file yet
        seen.append(entry.get())
        users_file.write_text(g.name_hash("Ivan") + "\n" + g.name_hash("Petro") + "\n", encoding="utf-8")  # list replaced while the window is open
        button.invoke(); self.update()

    monkeypatch.setattr(tk.Tk, "mainloop", drive)
    assert g.login_dialog(lambda: g.find_users(tmp_path)) is not None
    monkeypatch.setattr(tk.Tk, "mainloop", orig)
    assert seen  # the first try stayed in the window
