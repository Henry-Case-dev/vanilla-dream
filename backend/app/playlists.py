"""Playlist CRUD and ordering."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

from . import db
from .config import SYSTEM_PLAYLIST_ID, Settings
from .models import (
    REPEAT_MODES,
    playlist_available_ids,
    playlist_available_rows,
    playlist_payload,
    track_payload,
)
from .util import ApiError, is_relative_to, to_iso


def require_playlist(conn: sqlite3.Connection, playlist_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    if row is None:
        raise ApiError(404, "playlist_not_found", "Плейлист не найден.")
    return row


def is_system(row: sqlite3.Row | dict) -> bool:
    value = row["system"] if isinstance(row, sqlite3.Row) else row.get("system", 0)
    playlist_id = row["id"] if isinstance(row, sqlite3.Row) else row.get("id")
    return bool(value) or playlist_id == SYSTEM_PLAYLIST_ID


def require_mutable(conn: sqlite3.Connection, playlist_id: str) -> sqlite3.Row:
    row = require_playlist(conn, playlist_id)
    if is_system(row):
        raise ApiError(
            409,
            "system_playlist",
            "Системный плейлист «Вся медиатека» изменяется только через медиатеку.",
        )
    return row


def remove_playlist_cover(settings: Settings, row: sqlite3.Row | dict) -> None:
    cover = row["cover_path"] if isinstance(row, sqlite3.Row) else row.get("cover_path")
    if not cover:
        return
    path = settings.playlist_cover_dir / str(cover)
    if not is_relative_to(path, settings.playlist_cover_dir):
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


def _validate_name(name: str | None) -> str:
    text = str(name or "").strip()
    if not text:
        raise ApiError(400, "invalid_name", "Укажите название плейлиста.", {"name": "Укажите название."})
    if len(text) > 200:
        raise ApiError(400, "invalid_name", "Название плейлиста слишком длинное.")
    return text


def _validate_repeat(value: str | None, current: str = "none") -> str:
    if value is None:
        return current
    text = str(value).strip().lower()
    if text not in REPEAT_MODES:
        raise ApiError(422, "invalid_repeat", "Недопустимый режим повторения.")
    return text


def list_playlists(conn: sqlite3.Connection, settings: Settings) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM playlists ORDER BY CASE WHEN system = 1 OR id = ? THEN 0 ELSE 1 END, "
        "lower(name) ASC, created_at ASC",
        (SYSTEM_PLAYLIST_ID,),
    ).fetchall()
    return [playlist_payload(conn, row, settings) for row in rows]


def create_playlist(
    conn: sqlite3.Connection,
    settings: Settings,
    name: str,
    description: str | None = None,
    shuffle: bool = False,
    repeat_mode: str = "none",
    now: datetime | None = None,
) -> dict:
    stamp = to_iso(now)
    playlist_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO playlists(id, name, description, cover_path, shuffle, repeat_mode, "
        "system, created_at, updated_at) VALUES(?, ?, ?, NULL, ?, ?, 0, ?, ?)",
        (
            playlist_id,
            _validate_name(name),
            (description or "").strip() or None,
            1 if shuffle else 0,
            _validate_repeat(repeat_mode),
            stamp,
            stamp,
        ),
    )
    conn.commit()
    row = require_playlist(conn, playlist_id)
    return playlist_payload(conn, row, settings)


def update_playlist(
    conn: sqlite3.Connection,
    settings: Settings,
    playlist_id: str,
    fields: dict,
    now: datetime,
) -> dict:
    row = require_playlist(conn, playlist_id)
    system = is_system(row)
    updates: dict = {}
    if "name" in fields:
        if system and str(fields.get("name") or "").strip() != row["name"]:
            raise ApiError(409, "system_playlist", "Название системного плейлиста изменить нельзя.")
        updates["name"] = _validate_name(fields.get("name"))
    if "description" in fields:
        description = str(fields.get("description") or "").strip() or None
        if system and description != row["description"]:
            raise ApiError(409, "system_playlist", "Описание системного плейлиста изменить нельзя.")
        updates["description"] = description
    if "shuffle" in fields:
        updates["shuffle"] = 1 if fields.get("shuffle") else 0
    if "repeat_mode" in fields:
        updates["repeat_mode"] = _validate_repeat(fields.get("repeat_mode"), row["repeat_mode"])
    if updates:
        updates["updated_at"] = to_iso(now)
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE playlists SET {assignments} WHERE id = ?",
            (*updates.values(), playlist_id),
        )
        conn.commit()
    return playlist_payload(conn, require_playlist(conn, playlist_id), settings)


def delete_playlist(conn: sqlite3.Connection, settings: Settings, playlist_id: str, now: datetime) -> dict:
    row = require_playlist(conn, playlist_id)
    if is_system(row):
        raise ApiError(
            409,
            "system_playlist",
            "Системный плейлист «Вся медиатека» удалить нельзя.",
        )
    remove_playlist_cover(settings, row)
    conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
    conn.execute("DELETE FROM rotation_items WHERE playlist_id = ?", (playlist_id,))
    conn.execute(
        "UPDATE schedule_rules SET playlist_id = NULL, rotation = 1, updated_at = ? "
        "WHERE playlist_id = ?",
        (to_iso(now), playlist_id),
    )
    conn.execute(
        "UPDATE overrides SET active = 0 WHERE playlist_id = ? AND active = 1",
        (playlist_id,),
    )
    if db.get_setting(conn, "default_playlist_id") == playlist_id:
        db.set_setting(conn, "default_playlist_id", "")
    conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
    db.delete_setting(conn, f"_cycle:playlist:{playlist_id}")
    conn.commit()
    return {"deleted": True}


def playlist_tracks(
    conn: sqlite3.Connection,
    settings: Settings,
    playlist_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    require_playlist(conn, playlist_id)
    rows = playlist_available_rows(conn, playlist_id, limit=limit, offset=offset)
    items = []
    for index, row in enumerate(rows):
        item = track_payload(row, settings)
        stored_position = row["playlist_position"]
        item["position"] = (
            int(stored_position) if stored_position is not None else offset + index
        )
        items.append(item)
    return items


def playlist_detail(
    conn: sqlite3.Connection,
    settings: Settings,
    playlist_id: str,
    *,
    tracks_limit: int = 500,
    tracks_offset: int = 0,
) -> dict:
    row = require_playlist(conn, playlist_id)
    payload = playlist_payload(conn, row, settings)
    limit = max(0, min(int(tracks_limit), 2000))
    offset = max(0, int(tracks_offset))
    payload["tracks"] = playlist_tracks(
        conn, settings, playlist_id, limit=limit, offset=offset
    )
    payload["tracks_total"] = payload["track_count"]
    return payload


def _track_file_available(
    conn: sqlite3.Connection, settings: Settings, track: sqlite3.Row
) -> bool:
    from . import media

    try:
        path = media.resolve_track_path(settings, track)
    except ApiError:
        return False
    return path.exists()


def add_tracks(
    conn: sqlite3.Connection,
    settings: Settings,
    playlist_id: str,
    track_ids: list[str],
    now: datetime,
) -> dict:
    require_mutable(conn, playlist_id)
    if not isinstance(track_ids, list) or not track_ids:
        raise ApiError(422, "invalid_tracks", "Передайте список треков.")
    added = 0
    skipped = 0
    unavailable = 0
    seen: set[str] = set()
    position_row = conn.execute(
        "SELECT COALESCE(MAX(position), -1) AS position FROM playlist_tracks WHERE playlist_id = ?",
        (playlist_id,),
    ).fetchone()
    position = int(position_row["position"]) if position_row["position"] is not None else -1
    for raw_id in track_ids:
        track_id = str(raw_id or "").strip()
        if not track_id or track_id in seen:
            continue
        seen.add(track_id)
        track = conn.execute(
            "SELECT * FROM tracks WHERE id = ? AND pending_delete = 0", (track_id,)
        ).fetchone()
        if track is None:
            unavailable += 1
            continue
        existing = conn.execute(
            "SELECT 1 FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
            (playlist_id, track_id),
        ).fetchone()
        if existing is not None:
            # A stored relation stays "skipped" even if the file is currently
            # missing; it is still present in the playlist.
            skipped += 1
            continue
        if not _track_file_available(conn, settings, track):
            unavailable += 1
            continue
        position += 1
        conn.execute(
            "INSERT INTO playlist_tracks(playlist_id, track_id, position) VALUES(?, ?, ?)",
            (playlist_id, track_id, position),
        )
        added += 1
    conn.execute(
        "UPDATE playlists SET updated_at = ? WHERE id = ?", (to_iso(now), playlist_id)
    )
    conn.commit()
    return {
        "added": added,
        "skipped": skipped,
        "unavailable": unavailable,
        "playlist": playlist_payload(conn, require_playlist(conn, playlist_id), settings),
    }


def remove_track(
    conn: sqlite3.Connection,
    settings: Settings,
    playlist_id: str,
    track_id: str,
    now: datetime,
) -> dict:
    require_mutable(conn, playlist_id)
    conn.execute(
        "DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
        (playlist_id, track_id),
    )
    conn.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (to_iso(now), playlist_id))
    conn.commit()
    return {
        "removed": True,
        "playlist": playlist_payload(conn, require_playlist(conn, playlist_id), settings),
    }


def reorder(
    conn: sqlite3.Connection,
    settings: Settings,
    playlist_id: str,
    track_ids: list[str],
    now: datetime,
) -> dict:
    require_mutable(conn, playlist_id)
    current = {
        str(row["track_id"])
        for row in conn.execute(
            "SELECT track_id FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,)
        ).fetchall()
    }
    ordered = [str(item) for item in track_ids]
    if set(ordered) != current or len(ordered) != len(current):
        raise ApiError(
            422,
            "invalid_order",
            "Порядок должен содержать ровно те треки, которые есть в плейлисте.",
        )
    for index, track_id in enumerate(ordered):
        conn.execute(
            "UPDATE playlist_tracks SET position = ? WHERE playlist_id = ? AND track_id = ?",
            (index, playlist_id, track_id),
        )
    conn.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (to_iso(now), playlist_id))
    conn.commit()
    return {
        "playlist": playlist_payload(conn, require_playlist(conn, playlist_id), settings)
    }
