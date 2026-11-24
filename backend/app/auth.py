"""Single-admin authentication: scrypt hashes, server sessions, cookies, CSRF."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Iterator

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from . import db
from .util import ApiError, Clock, parse_iso, to_iso

COOKIE_NAME = "vd_session"
SESSION_TTL = timedelta(days=7)
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
CSRF_HEADER = "X-CSRF-Token"
LOGIN_WINDOW_SECONDS = 60
LOGIN_MAX_FAILURES = 5
MIN_PASSWORD_LENGTH = 8


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    if not password:
        raise ValueError("empty password")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM,
        dklen=32,
    )
    return digest.hex(), salt.hex()


def verify_password(password: str, password_hash: str, salt_hex: str) -> bool:
    if not password or not password_hash or not salt_hex:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, password_hash)


def set_admin_password(conn: sqlite3.Connection, password: str, now: datetime) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ApiError(
            400,
            "weak_password",
            f"Пароль должен содержать не менее {MIN_PASSWORD_LENGTH} символов.",
        )
    password_hash, salt = hash_password(password)
    conn.execute(
        "INSERT INTO admin(id, password_hash, salt, updated_at) VALUES(1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET password_hash = excluded.password_hash, "
        "salt = excluded.salt, updated_at = excluded.updated_at",
        (password_hash, salt, to_iso(now)),
    )
    db.set_setting(conn, "admin_password_set", "1")
    # A new password invalidates every existing session and CSRF token.
    conn.execute("DELETE FROM sessions")
    db.delete_setting(conn, "_csrf_pepper")


def admin_credentials(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT password_hash, salt FROM admin WHERE id = 1").fetchone()


# ---------------------------------------------------------------------------
# Sessions and CSRF
# ---------------------------------------------------------------------------


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def csrf_pepper(conn: sqlite3.Connection) -> str:
    pepper = db.get_setting(conn, "_csrf_pepper")
    if not pepper:
        pepper = secrets.token_hex(32)
        db.set_setting(conn, "_csrf_pepper", pepper)
    return pepper


def csrf_token_for(conn: sqlite3.Connection, token: str) -> str:
    return hmac.new(
        csrf_pepper(conn).encode("ascii"), token_hash(token).encode("ascii"), hashlib.sha256
    ).hexdigest()


def create_session(conn: sqlite3.Connection, now: datetime) -> str:
    token = secrets.token_urlsafe(32)
    session_id = secrets.token_hex(16)
    conn.execute(
        "INSERT INTO sessions(id, token_hash, created_at, expires_at, last_seen) "
        "VALUES(?, ?, ?, ?, ?)",
        (
            session_id,
            token_hash(token),
            to_iso(now),
            to_iso(now + SESSION_TTL),
            to_iso(now),
        ),
    )
    return token


def find_session(conn: sqlite3.Connection, token: str | None, now: datetime) -> sqlite3.Row | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT * FROM sessions WHERE token_hash = ?", (token_hash(token),)
    ).fetchone()
    if row is None:
        return None
    expires = row["expires_at"]
    expires_at = parse_iso(expires)
    if expires_at is None or expires_at <= now:
        conn.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
        conn.commit()
        return None
    return row


def delete_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))


def purge_expired_sessions(conn: sqlite3.Connection, now: datetime) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (to_iso(now),))


def set_session_cookie(response: Response, settings, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=bool(settings.secure_cookies),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = db.open_db(request.app.state.settings)
    try:
        yield conn
    finally:
        conn.close()


def clock(request: Request) -> Clock:
    return request.app.state.clock


def optional_session(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    clk: Clock = Depends(clock),
) -> dict | None:
    token = request.cookies.get(COOKIE_NAME)
    now = clk.now()
    row = find_session(conn, token, now)
    if row is None:
        return None
    last_seen = parse_iso(row["last_seen"])
    if last_seen is None or (now - last_seen).total_seconds() > 60:
        conn.execute(
            "UPDATE sessions SET last_seen = ? WHERE id = ?", (to_iso(now), row["id"])
        )
        conn.commit()
    return {"row": row, "token": token, "id": row["id"]}


def require_session(session: dict | None = Depends(optional_session)) -> dict:
    if session is None:
        raise ApiError(401, "unauthorized", "Требуется авторизация.")
    return session


def csrf_expected(conn: sqlite3.Connection, session: dict) -> str:
    return csrf_token_for(conn, session["token"])


def require_csrf(
    request: Request,
    session: dict = Depends(require_session),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    provided = request.headers.get(CSRF_HEADER) or request.headers.get("x-csrf-token")
    if not provided or not hmac.compare_digest(provided, csrf_expected(conn, session)):
        raise ApiError(403, "csrf_failed", "Неверный или отсутствующий CSRF-токен.")
    return session


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/api/auth", tags=["auth"])


def _login_attempts(request: Request) -> dict[str, list[float]]:
    state = request.app.state
    if not hasattr(state, "login_attempts"):
        state.login_attempts = {}
    return state.login_attempts


def _client_ip(request: Request) -> str:
    client = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("X-Forwarded-For")
    if client in ("127.0.0.1", "::1", "localhost", "testclient") and forwarded:
        # Caddy appends the real peer address, so the last entry is trustworthy.
        return forwarded.split(",")[-1].strip() or client
    return client


def _check_rate_limit(request: Request) -> None:
    attempts = _login_attempts(request)
    ip = _client_ip(request)
    now_ts = request.app.state.clock.now().timestamp()
    bucket = [stamp for stamp in attempts.get(ip, []) if now_ts - stamp < LOGIN_WINDOW_SECONDS]
    attempts[ip] = bucket
    if len(bucket) >= LOGIN_MAX_FAILURES:
        raise ApiError(
            429,
            "too_many_attempts",
            "Слишком много попыток входа. Попробуйте через минуту.",
        )


def _record_failure(request: Request) -> None:
    attempts = _login_attempts(request)
    ip = _client_ip(request)
    now_ts = request.app.state.clock.now().timestamp()
    attempts.setdefault(ip, []).append(now_ts)


@router.post("/login")
async def login(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - invalid JSON body
        raise ApiError(400, "invalid_body", "Некорректный запрос.")
    password = str(body.get("password") or "") if isinstance(body, dict) else ""
    if not password:
        raise ApiError(400, "invalid_body", "Введите пароль.", {"password": "Введите пароль."})

    _check_rate_limit(request)
    credentials = admin_credentials(conn)
    if credentials is None:
        raise ApiError(
            503,
            "admin_not_configured",
            "Пароль администратора не задан. Выполните первичную настройку сервера.",
        )
    if not verify_password(password, credentials["password_hash"], credentials["salt"]):
        _record_failure(request)
        raise ApiError(401, "invalid_credentials", "Неверный пароль.")

    _login_attempts(request).pop(_client_ip(request), None)
    now = request.app.state.clock.now()
    token = create_session(conn, now)
    csrf = csrf_token_for(conn, token)
    conn.commit()
    response = JSONResponse({"ok": True, "csrf_token": csrf})
    set_session_cookie(response, request.app.state.settings, token)
    return response


@router.post("/logout")
def logout(
    request: Request,
    session: dict = Depends(require_csrf),
    conn: sqlite3.Connection = Depends(get_conn),
):
    delete_session(conn, session["token"])
    conn.commit()
    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    return response


@router.get("/me")
def me(
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict | None = Depends(optional_session),
):
    if session is None:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "csrf_token": csrf_token_for(conn, session["token"]),
    }


@router.post("/password")
def change_password(
    request: Request,
    body: dict,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    from .util import parse_iso

    current = str(body.get("current") or "")
    new = str(body.get("new") or "")
    credentials = admin_credentials(conn)
    if credentials is None:
        raise ApiError(401, "invalid_credentials", "Неверный текущий пароль.")
    if not verify_password(current, credentials["password_hash"], credentials["salt"]):
        raise ApiError(400, "invalid_credentials", "Неверный текущий пароль.")
    if len(new) < MIN_PASSWORD_LENGTH:
        raise ApiError(
            400,
            "weak_password",
            f"Пароль должен содержать не менее {MIN_PASSWORD_LENGTH} символов.",
        )
    now = request.app.state.clock.now()
    set_admin_password(conn, new, now)
    token = create_session(conn, now)
    csrf = csrf_token_for(conn, token)
    conn.commit()
    response = JSONResponse({"ok": True, "csrf_token": csrf})
    set_session_cookie(response, request.app.state.settings, token)
    return response


__all__ = [
    "COOKIE_NAME",
    "CSRF_HEADER",
    "Clock",
    "admin_credentials",
    "clear_session_cookie",
    "clock",
    "create_session",
    "csrf_token_for",
    "delete_session",
    "find_session",
    "get_conn",
    "hash_password",
    "purge_expired_sessions",
    "require_csrf",
    "require_session",
    "router",
    "set_admin_password",
    "set_session_cookie",
    "verify_password",
]
