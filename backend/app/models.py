"""Row -> API payload mapping helpers."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .config import Settings

SOURCE_LABELS = {
    "override": "Переопределение",
    "schedule": "Расписание",
    "rotation": "Ротация",
    "playlist": "Плейлист",
    "library": "Вся медиатека",
    "manual": "Ручная очередь",
}

STATE_LABELS = {
    "off": "Остановлено",
    "starting": "Запускается",
    "on_air": "В эфире",
    "no_music": "Нет музыки",
    "reconnecting": "Восстанавливает соединение",
    "error": "Ошибка",
}

REPEAT_MODES = ("none", "all", "one")
ROTATION_MODES = ("sequential", "by_count", "random")
TRANSITIONS = ("after_track", "immediate")


def _value(row: sqlite3.Row | dict[str, Any], key: str, default: Any = None) -> Any:
    if isinstance(row, sqlite3.Row):
        return row[key] if key in row.keys() else default
    return row.get(key, default)


def track_payload(
    row: sqlite3.Row | dict[str, Any],
    settings: Settings,
    *,
    include_source: bool = False,
) -> dict[str, Any]:
    track_id = _value(row, "id")
    cover_path = _value(row, "cover_path")
    cover_url = (
        f"{settings.public_base_url}/api/public/cover/{track_id}" if cover_path else None
    )
    payload: dict[str, Any] = {
        "id": track_id,
        "title": _value(row, "title") or "",
        "artist": _value(row, "artist"),
        "album": _value(row, "album"),
        "genre": _value(row, "genre"),
        "year": _value(row, "year"),
        "duration": _value(row, "duration"),
        "bitrate": _value(row, "bitrate"),
        "size": _value(row, "size"),
        "ext": _value(row, "ext"),
        "cover_url": cover_url,
        "play_count": _value(row, "play_count", 0) or 0,
        "pending_delete": bool(_value(row, "pending_delete", 0)),
        "created_at": _value(row, "created_at"),
        "updated_at": _value(row, "updated_at"),
        "stream_url": f"/api/admin/tracks/{track_id}/stream",
    }
    if include_source:
        payload["source_title"] = _value(row, "source_title")
        payload["source_artist"] = _value(row, "source_artist")
        payload["source_album"] = _value(row, "source_album")
        payload["source_genre"] = _value(row, "source_genre")
        payload["sha256"] = _value(row, "sha256")
        payload["path"] = _value(row, "path")
    return payload


def playlist_payload(
    conn: sqlite3.Connection,
    row: sqlite3.Row | dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    playlist_id = _value(row, "id")
    stats = conn.execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(t.duration), 0) AS duration "
        "FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id "
        "WHERE pt.playlist_id = ? AND t.pending_delete = 0",
        (playlist_id,),
    ).fetchone()
    cover = conn.execute(
        "SELECT t.cover_path FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id "
        "WHERE pt.playlist_id = ? AND t.cover_path IS NOT NULL AND t.pending_delete = 0 "
        "ORDER BY pt.position ASC LIMIT 1",
        (playlist_id,),
    ).fetchone()
    cover_url = (
        f"{settings.public_base_url}/api/public/cover/{playlist_id}" if cover else None
    )
    if cover:
        # Playlist covers are represented by the first track cover; the public
        # cover endpoint understands playlist ids too.
        cover_url = f"{settings.public_base_url}/api/public/playlist-cover/{playlist_id}"
    return {
        "id": playlist_id,
        "name": _value(row, "name"),
        "description": _value(row, "description"),
        "shuffle": bool(_value(row, "shuffle", 0)),
        "repeat_mode": _value(row, "repeat_mode", "none") or "none",
        "cover_url": cover_url,
        "track_count": int(stats["count"] or 0),
        "duration": float(stats["duration"] or 0.0),
        "created_at": _value(row, "created_at"),
        "updated_at": _value(row, "updated_at"),
    }


def source_payload(conn: sqlite3.Connection, kind: str | None, ref: str | None) -> dict[str, Any]:
    playlist_id = None
    name = SOURCE_LABELS.get(kind or "", "Источник")
    if ref:
        parts = ref.split(":")
        if "playlist" in parts:
            index = parts.index("playlist")
            if index + 1 < len(parts):
                playlist_id = parts[index + 1]
        if "item" in parts:
            index = parts.index("item")
            if index + 1 < len(parts):
                item = conn.execute(
                    "SELECT ri.playlist_id, p.name FROM rotation_items ri "
                    "LEFT JOIN playlists p ON p.id = ri.playlist_id WHERE ri.id = ?",
                    (parts[index + 1],),
                ).fetchone()
                if item and item["playlist_id"]:
                    playlist_id = item["playlist_id"]
                    name = f"Ротация · {item['name'] or 'без названия'}"
        if playlist_id:
            row = conn.execute("SELECT name FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
            if row:
                name = row["name"]
        if kind == "schedule":
            rule = rule_id_from_ref(ref)
            if rule:
                row = conn.execute(
                    "SELECT name FROM schedule_rules WHERE id = ?", (rule,)
                ).fetchone()
                if row and row["name"]:
                    name = f"Расписание · {row['name']}"
    return {
        "kind": kind or "unknown",
        "label": SOURCE_LABELS.get(kind or "", "Источник"),
        "playlist_id": playlist_id,
        "name": name,
    }


def rule_id_from_ref(ref: str | None) -> str | None:
    if not ref:
        return None
    parts = ref.split(":")
    if parts and parts[0] == "schedule" and len(parts) > 1:
        return parts[1]
    if parts and parts[0] == "override" and len(parts) > 1:
        return None
    return None


def schedule_rule_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    days = []
    try:
        days = json.loads(row["days"])
    except (TypeError, ValueError):
        days = []
    return {
        "id": row["id"],
        "name": row["name"],
        "playlist_id": row["playlist_id"],
        "use_rotation": bool(row["rotation"]),
        "days": [int(day) for day in days],
        "start": minutes_to_hhmm(int(row["start_min"])),
        "end": minutes_to_hhmm(int(row["end_min"])),
        "start_min": int(row["start_min"]),
        "end_min": int(row["end_min"]),
        "enabled": bool(row["enabled"]),
        "priority": int(row["priority"]),
        "transition": row["transition"] or "after_track",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def minutes_to_hhmm(minutes: int) -> str:
    minutes = int(minutes) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def hhmm_to_minutes(value: str) -> int:
    text = str(value or "").strip()
    if ":" not in text:
        raise ValueError("time")
    hours_text, minutes_text = text.split(":", 1)
    hours = int(hours_text)
    minutes = int(minutes_text)
    if not (0 <= hours <= 24) or not (0 <= minutes < 60):
        raise ValueError("time")
    total = hours * 60 + minutes
    if total > 24 * 60:
        raise ValueError("time")
    return total % (24 * 60)
