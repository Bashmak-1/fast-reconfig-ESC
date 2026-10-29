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
