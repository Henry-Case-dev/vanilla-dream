"""SQLite access layer: WAL, foreign keys, busy timeout, idempotent schema."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import Settings

SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

DEFAULT_SETTINGS: dict[str, str] = {
    "station_name": "Vanilla Dream Radio",
    "station_description": "Круглосуточный сиреневый эфир",
    "public_base_url": "https://vanilladream.duckdns.org",
    "bitrate": "128",
    "timezone": "Asia/Magadan",
    "schedule_enabled": "1",
    "default_transition": "after_track",
    "default_playlist_id": "",
    "desired_state": "stopped",
    "queue_depth": "6",
    "max_upload_mb": "300",
    "max_library_gb": "0",
    "station_genre": "Eclectic",
    "admin_password_set": "0",
    "engine_last_start": "",
    "engine_last_error": "",
    "last_schedule_change": "",
}

# Keys starting with an underscore are internal bookkeeping and never exposed
# through the admin settings API.
INTERNAL_PREFIX = "_"


def open_db(settings: Settings) -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(settings.db_path), timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.create_function(
        "py_lower", 1, lambda value: str(value).lower() if value is not None else ""
    )
    return conn


def init_db(settings: Settings) -> None:
    settings.ensure_directories()
    conn = open_db(settings)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        for key, value in DEFAULT_SETTINGS.items():
            seeded = value
            if key == "public_base_url" and settings.public_base_url:
                # The deployment environment is the initial source of truth;
                # afterwards the owner-editable value in the database wins.
                seeded = settings.public_base_url
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
                (key, seeded),
            )
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        if default is not None:
            return default
        return DEFAULT_SETTINGS.get(key, "")
    return str(row["value"])


def get_int_setting(conn: sqlite3.Connection, key: str, default: int = 0) -> int:
    try:
        return int(get_setting(conn, key, str(default)))
    except (TypeError, ValueError):
        return default


def get_float_setting(conn: sqlite3.Connection, key: str, default: float = 0.0) -> float:
    try:
        return float(get_setting(conn, key, str(default)))
    except (TypeError, ValueError):
        return default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def delete_setting(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def all_settings(conn: sqlite3.Connection, include_internal: bool = False) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    return {
        str(row["key"]): str(row["value"])
        for row in rows
        if include_internal or not str(row["key"]).startswith(INTERNAL_PREFIX)
    }


def get_json_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    raw = get_setting(conn, key, "")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def set_json_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    set_setting(conn, key, json.dumps(value, ensure_ascii=False, sort_keys=True))


def log_event(
    conn: sqlite3.Connection,
    level: str,
    kind: str,
    message: str,
    detail: Any = None,
    at: str | None = None,
) -> None:
    from .util import to_iso, utcnow

    conn.execute(
        "INSERT INTO events(at, level, kind, message, detail) VALUES(?, ?, ?, ?, ?)",
        (
            at or to_iso(utcnow()),
            level,
            kind,
            message,
            None if detail is None else json.dumps(detail, ensure_ascii=False, default=str),
        ),
    )


def library_totals(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes FROM tracks WHERE pending_delete = 0"
    ).fetchone()
    return {"count": int(row["count"] or 0), "bytes": int(row["bytes"] or 0)}
