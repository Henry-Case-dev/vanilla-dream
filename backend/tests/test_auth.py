"""AUTH-01..03: login, sessions, CSRF, password change, rate limiting."""

from __future__ import annotations

from datetime import timedelta

from app import auth, db


def test_login_rejected_without_admin_password(env):
    response = env.client.post("/api/auth/login", json={"password": "whatever"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "admin_not_configured"
    assert "Пароль администратора не задан" in response.json()["detail"]["message"]


def test_login_wrong_password_and_rate_limit(env, admin):
    env.client.post("/api/auth/logout")
    codes = []
    for _ in range(6):
        response = env.client.post("/api/auth/login", json={"password": "wrong-password"})
        codes.append(response.status_code)
    assert codes[:5] == [401] * 5
    assert codes[5] == 429
    body = env.client.post("/api/auth/login", json={"password": "wrong"}).json()
    assert body["detail"]["code"] == "too_many_attempts"
    assert "Слишком много попыток" in body["detail"]["message"]


def test_login_sets_httponly_cookie_and_me(env, admin):
    cookie = env.client.cookies.get("vd_session")
    assert cookie
    me = env.client.get("/api/auth/me").json()
    assert me["authenticated"] is True
    assert me["csrf_token"] == admin
    # Re-login to inspect the raw Set-Cookie attributes.
    env.client.post("/api/auth/logout")
    response = env.client.post("/api/auth/login", json={"password": env.admin_password})
    assert response.status_code == 200
    header = response.headers.get("set-cookie", "")
    assert "HttpOnly" in header
    assert "SameSite=lax" in header or "SameSite=Lax" in header
    assert "Path=/" in header
    assert "Secure" not in header


def test_admin_endpoints_require_session(env):
    assert env.client.get("/api/admin/settings").status_code == 401
    assert env.client.get("/api/admin/queue").status_code == 401
    assert env.client.get("/api/admin/links").status_code == 401


def test_csrf_required_for_mutations(env, admin):
    env.client.headers.pop("X-CSRF-Token")
    response = env.client.post("/api/admin/playlists", json={"name": "Без CSRF"})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "csrf_failed"
    env.client.headers.update({"X-CSRF-Token": admin})
    ok = env.client.post("/api/admin/playlists", json={"name": "С CSRF"})
    assert ok.status_code == 201


def test_logout_invalidates_session(env, admin):
    assert env.client.post("/api/auth/logout").status_code == 200
    assert env.client.get("/api/auth/me").json()["authenticated"] is False
    assert env.client.get("/api/admin/settings").status_code == 401


def test_password_change_rotates_sessions_and_csrf(env, admin):
    bad = env.client.post(
        "/api/auth/password", json={"current": "nope", "new": "another-secret-1"}
    )
    assert bad.status_code == 400
    weak = env.client.post(
        "/api/auth/password", json={"current": env.admin_password, "new": "short"}
    )
    assert weak.status_code == 400
    good = env.client.post(
        "/api/auth/password",
        json={"current": env.admin_password, "new": "another-secret-1"},
    )
    assert good.status_code == 200
    new_csrf = good.json()["csrf_token"]
    assert new_csrf != admin
    old_csrf_attempt = env.client.patch(
        "/api/admin/tracks/missing",
        json={"title": "x"},
        headers={"X-CSRF-Token": admin},
    )
    assert old_csrf_attempt.status_code == 403
    env.client.headers.update({"X-CSRF-Token": new_csrf})
    me = env.client.get("/api/auth/me").json()
    assert me["authenticated"] is True
    assert me["csrf_token"] == new_csrf


def test_session_expires_after_seven_days(env, admin):
    assert env.client.get("/api/auth/me").json()["authenticated"] is True
    env.clock.advance(timedelta(days=8).total_seconds())
    assert env.client.get("/api/auth/me").json()["authenticated"] is False
    assert env.client.get("/api/admin/settings").status_code == 401


def test_rate_limit_is_per_forwarded_ip(env, admin):
    env.client.post("/api/auth/logout")
    for _ in range(5):
        env.client.post(
            "/api/auth/login",
            json={"password": "wrong"},
            headers={"X-Forwarded-For": "1.1.1.1"},
        )
    blocked = env.client.post(
        "/api/auth/login",
        json={"password": env.admin_password},
        headers={"X-Forwarded-For": "1.1.1.1"},
    )
    assert blocked.status_code == 429
    other = env.client.post(
        "/api/auth/login",
        json={"password": env.admin_password},
        headers={"X-Forwarded-For": "2.2.2.2"},
    )
    assert other.status_code == 200


def test_scrypt_hash_is_salted(env, admin):
    conn = db.open_db(env.settings)
    try:
        row = conn.execute("SELECT password_hash, salt FROM admin WHERE id = 1").fetchone()
        assert row["password_hash"] != env.admin_password
        assert len(row["salt"]) == 32
        assert auth.verify_password(env.admin_password, row["password_hash"], row["salt"])
        assert not auth.verify_password("nope", row["password_hash"], row["salt"])
        first_hash = row["password_hash"]
        auth.set_admin_password(conn, "same-password-1", env.clock.now())
        conn.commit()
        second = conn.execute("SELECT password_hash, salt FROM admin WHERE id = 1").fetchone()
        assert second["password_hash"] != first_hash
        auth.set_admin_password(conn, "same-password-1", env.clock.now())
        conn.commit()
        third = conn.execute("SELECT password_hash, salt FROM admin WHERE id = 1").fetchone()
        assert third["password_hash"] != second["password_hash"]
    finally:
        conn.close()


def test_weak_password_rejected_by_cli_helper(env):
    from app.util import ApiError

    conn = db.open_db(env.settings)
    try:
        try:
            auth.set_admin_password(conn, "1234567", env.clock.now())
        except ApiError as exc:
            assert exc.status_code == 400
            assert "не менее 8" in exc.message
        else:  # pragma: no cover - explicit failure
            raise AssertionError("weak password accepted")
    finally:
        conn.close()
