from esc_hotline import session_gate as g

USERS = {g.name_hash("Ivan")}


def test_login_results():
    assert g.check_login(" ivan ", "ivan1029", USERS, "2026-10-29") == "ok"
    assert g.check_login("Ivan", "ivan1028", USERS, "2026-10-29") == "bad_password"
    assert g.check_login("Petro", "petro1029", USERS, "2026-10-29") == "unknown_user"


def test_users_file_parsing():
    assert g.load_users("# c\nAB12\n\n  cd34  \n") == {"ab12", "cd34"}


def test_token_is_bound_to_day_and_pid():
    env = g.session_env(123)
    assert g.token_ok(env)
    assert not g.token_ok({**env, g.ENV_PID: "124"})
    assert not g.token_ok({})
    assert g.make_token(123, "2026-10-29") != g.make_token(123, "2026-10-30")
