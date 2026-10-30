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
