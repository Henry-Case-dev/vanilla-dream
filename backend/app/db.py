"""SQLite access layer: WAL, foreign keys, busy timeout, idempotent schema."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import (
    SYSTEM_PLAYLIST_DESCRIPTION,
    SYSTEM_PLAYLIST_ID,
    SYSTEM_PLAYLIST_NAME,
    Settings,
)

SCHEMA_VERSION = 3
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
        previous_version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _migrate(conn, previous_version)
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
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection, previous_version: int) -> None:
    """Apply non-destructive schema migrations (v1 -> v2 -> v3) atomically."""

    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN IMMEDIATE")
    try:
        _migrate_locked(conn, previous_version)
        if started:
            conn.commit()
    except BaseException:
        if started:
            conn.rollback()
        raise


def _migrate_locked(conn: sqlite3.Connection, previous_version: int) -> None:
    from .util import to_iso, utcnow

    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(playlists)").fetchall()
    }
    if "system" not in columns:
        conn.execute(
            "ALTER TABLE playlists ADD COLUMN system INTEGER NOT NULL DEFAULT 0"
        )
    stamp = to_iso(utcnow()) or ""
    conn.execute(
        "INSERT INTO playlists(id, name, description, cover_path, shuffle, repeat_mode, "
        "system, created_at, updated_at) VALUES(?, ?, ?, NULL, 1, 'all', 1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "name = excluded.name, description = excluded.description, system = 1",
        (SYSTEM_PLAYLIST_ID, SYSTEM_PLAYLIST_NAME, SYSTEM_PLAYLIST_DESCRIPTION, stamp, stamp),
    )
    if previous_version < 2:
        # The v1 backend hardcoded an exhausted library cycle; it must start
        # fresh on the system playlist row. User data is untouched.
        conn.execute(
            "DELETE FROM settings WHERE key IN (?, ?)",
            ("_cycle:library", "_cycle:playlist:library"),
        )
    _migrate_tracks_v3(conn)


TRACK_COLUMNS_V3 = (
    "id",
    "sha256",
    "path",
    "ext",
    "title",
    "artist",
    "album",
    "genre",
    "year",
    "duration",
    "bitrate",
    "size",
    "cover_path",
    "custom_cover_path",
    "source_title",
    "source_artist",
    "source_album",
    "source_genre",
    "source_filename",
    "duplicate_of",
    "duplicate_kind",
    "duplicate_confirmed_at",
    "play_count",
    "pending_delete",
    "created_at",
    "updated_at",
)


def _sha256_is_unique(conn: sqlite3.Connection) -> bool:
    """Report whether the legacy ``UNIQUE(sha256)`` constraint is still active."""

    for index in conn.execute("PRAGMA index_list(tracks)").fetchall():
        if not int(index["unique"] or 0):
            continue
        listed = conn.execute(
            f'PRAGMA index_info("{index["name"]}")'
        ).fetchall()
        if [str(row["name"]) for row in listed] == ["sha256"]:
            return True
    return False


def _migrate_tracks_v3(conn: sqlite3.Connection) -> None:
    """Rebuild ``tracks`` to drop ``UNIQUE(sha256)`` and add media columns.

    The rebuild copies every row, id, path and relation. It is idempotent: a
    database that already has the v3 layout (and no unique sha256 constraint)
    is left untouched.
    """

    columns = {
        str(row["name"]) for row in conn.execute("PRAGMA table_info(tracks)").fetchall()
    }
    if not columns:
        # schema.sql always creates the table, but keep the migration safe.
        return
    required = {
        "source_filename",
        "custom_cover_path",
        "duplicate_of",
        "duplicate_kind",
        "duplicate_confirmed_at",
    }
    if required <= columns and not _sha256_is_unique(conn):
        return

    conn.execute("DROP TABLE IF EXISTS tracks_rebuild_v3")
    conn.execute(
        """
        CREATE TABLE tracks_rebuild_v3 (
            id TEXT PRIMARY KEY,
            sha256 TEXT NOT NULL,
            path TEXT NOT NULL,
            ext TEXT,
            title TEXT NOT NULL,
            artist TEXT,
            album TEXT,
            genre TEXT,
            year INTEGER,
            duration REAL,
            bitrate INTEGER,
            size INTEGER,
            cover_path TEXT,
            custom_cover_path TEXT,
            source_title TEXT,
            source_artist TEXT,
            source_album TEXT,
            source_genre TEXT,
            source_filename TEXT,
            duplicate_of TEXT,
            duplicate_kind TEXT,
            duplicate_confirmed_at TEXT,
            play_count INTEGER DEFAULT 0,
            pending_delete INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    defaults = {
        "play_count": "0",
        "pending_delete": "0",
    }
    select_parts = []
    for name in TRACK_COLUMNS_V3:
        if name in columns:
            select_parts.append(name)
        else:
            select_parts.append(f"{defaults.get(name, 'NULL')} AS {name}")
    conn.execute(
        f"INSERT INTO tracks_rebuild_v3 ({', '.join(TRACK_COLUMNS_V3)}) "
        f"SELECT {', '.join(select_parts)} FROM tracks"
    )
    conn.execute("DROP TABLE tracks")
    conn.execute("ALTER TABLE tracks_rebuild_v3 RENAME TO tracks")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_sha256 ON tracks(sha256)")


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
