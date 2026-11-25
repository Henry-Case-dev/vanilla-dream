"""Queue engine: source resolution, shuffle/repeat cycles, rotation, engine sync.

Liquidsoap only plays annotated request paths; the backend is the single source of
truth for what should play next.  Positions are persisted in ``queue_positions``
with stable UUIDs that travel to Liquidsoap and come back through the
``track-start`` callback.
"""

from __future__ import annotations

import functools
import logging
import random
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import db, media, scheduler
from .config import SYSTEM_PLAYLIST_ID, SYSTEM_PLAYLIST_NAME, Settings
from .models import (
    playlist_available_ids,
    playlist_available_rows,
    source_payload,
    track_payload,
)
from .util import ApiError, parse_iso, to_iso

LOGGER = logging.getLogger("vdradio.queue")

ENGINE_PREFETCH = 2
ENGINE_SYNC_MIN_INTERVAL = 5.0
ROTATION_ID = "default"
LIVE_STATES = ("pending", "prepared")

# One backend process owns programming; this lock keeps the scheduler thread and
# request threads from generating or syncing the same queue concurrently.
QUEUE_LOCK = threading.RLock()


def _locked(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with QUEUE_LOCK:
            return func(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


@dataclass
class Source:
    kind: str
    key: str
    playlist_id: str | None = None
    rotation_id: str | None = None
    rotation_item_id: str | None = None
    rule_id: str | None = None
    override_id: str | None = None
    name: str = ""
    repeat_mode: str = "none"
    shuffle: bool = False
    by_count: bool = False
    allow_empty: bool = False
    fallback_allowed: bool = False
    is_system: bool = False

    @property
    def ref_prefix(self) -> str:
        return self.key

    def ref(self, cycle: int) -> str:
        return f"{self.key}:cycle:{cycle}"


@dataclass
class Choice:
    track_id: str | None
    cycle: int = 0
    reason: str = "ok"  # ok | exhausted | advanced


def key_of(ref: str | None) -> str:
    if not ref:
        return ""
    marker = ":cycle:"
    if marker in ref:
        return ref.rsplit(marker, 1)[0]
    return ref


def parse_item_id(ref: str | None) -> str | None:
    if not ref:
        return None
    parts = ref.split(":")
    if "item" in parts:
        index = parts.index("item")
        if index + 1 < len(parts):
            return parts[index + 1]
    return None


def parse_override_id(ref: str | None) -> str | None:
    if not ref:
        return None
    parts = ref.split(":")
    if parts and parts[0] == "override" and len(parts) > 1:
        return parts[1]
    return None


# ---------------------------------------------------------------------------
# Basic data helpers
# ---------------------------------------------------------------------------


def playlist_track_ids(conn: sqlite3.Connection, playlist_id: str) -> list[str]:
    return playlist_available_ids(conn, playlist_id)


def library_track_ids(conn: sqlite3.Connection) -> list[str]:
    return playlist_available_ids(conn, SYSTEM_PLAYLIST_ID)


def get_playlist(conn: sqlite3.Connection, playlist_id: str | None) -> sqlite3.Row | None:
    if not playlist_id:
        return None
    return conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()


def system_playlist_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    row = get_playlist(conn, SYSTEM_PLAYLIST_ID)
    if row is not None:
        return row
    return None


def get_rotation(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM rotations ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END, updated_at ASC LIMIT 1",
        (ROTATION_ID,),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO rotations(id, name, mode, updated_at) VALUES(?, ?, 'sequential', ?)",
            (ROTATION_ID, "Основная ротация", to_iso(None)),
        )
        row = conn.execute("SELECT * FROM rotations WHERE id = ?", (ROTATION_ID,)).fetchone()
    return row


def _rotation_items(conn: sqlite3.Connection, rotation_id: str) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT ri.*, p.name AS playlist_name FROM rotation_items ri "
        "LEFT JOIN playlists p ON p.id = ri.playlist_id "
        "WHERE ri.rotation_id = ? AND ri.enabled = 1 ORDER BY ri.position ASC, ri.id ASC",
        (rotation_id,),
    ).fetchall()
    usable = []
    for row in rows:
        ids = playlist_track_ids(conn, str(row["playlist_id"])) if row["playlist_id"] else []
        if ids:
            usable.append(row)
        else:
            _warn_once(
                conn,
                f"rotation:{row['id']}",
                "Пустой плейлист в ротации пропущен",
                {"playlist_id": row["playlist_id"]},
            )
    return usable


def _warn_once(conn: sqlite3.Connection, key: str, message: str, detail: dict | None = None) -> None:
    setting_key = f"_warn:{key}"
    if db.get_setting(conn, setting_key):
        return
    db.set_setting(conn, setting_key, "1")
    db.log_event(conn, "warning", "queue", message, detail)

def _played(conn: sqlite3.Connection, item_id: str) -> int:
    row = conn.execute(
        "SELECT played FROM rotation_state WHERE id = ?", (f"played:{item_id}",)
    ).fetchone()
    try:
        return int(row["played"]) if row else 0
    except (TypeError, ValueError):
        return 0


def _set_played(conn: sqlite3.Connection, item_id: str, value: int) -> None:
    conn.execute(
        "INSERT INTO rotation_state(id, item_id, played, updated_at) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET played = excluded.played, updated_at = excluded.updated_at",
        (f"played:{item_id}", item_id, int(value), to_iso(None)),
    )


def _cursor_item(conn: sqlite3.Connection, rotation_id: str) -> str | None:
    row = conn.execute(
        "SELECT item_id FROM rotation_state WHERE id = ?", (f"cursor:{rotation_id}",)
    ).fetchone()
    return str(row["item_id"]) if row and row["item_id"] else None


def _set_cursor(conn: sqlite3.Connection, rotation_id: str, item_id: str) -> None:
    conn.execute(
        "INSERT INTO rotation_state(id, item_id, played, updated_at) VALUES(?, ?, 0, ?) "
        "ON CONFLICT(id) DO UPDATE SET item_id = excluded.item_id, updated_at = excluded.updated_at",
        (f"cursor:{rotation_id}", item_id, to_iso(None)),
    )


def _advance_rotation(
    conn: sqlite3.Connection, rotation: sqlite3.Row, current_item_id: str | None
) -> sqlite3.Row | None:
    items = _rotation_items(conn, str(rotation["id"]))
    if not items:
        return None
    mode = rotation["mode"] or "sequential"
    ids = [str(item["id"]) for item in items]
    if mode == "random":
        choices = [item for item in items if str(item["id"]) != current_item_id] or items
        chosen = random.SystemRandom().choice(choices)
    else:
        if current_item_id in ids:
            index = ids.index(current_item_id)
            chosen = items[(index + 1) % len(items)]
        else:
            chosen = items[0]
    _set_cursor(conn, str(rotation["id"]), str(chosen["id"]))
    _set_played(conn, str(chosen["id"]), 0)
    _reset_cycles_for_item(conn, str(chosen["id"]))
    return chosen


def _reset_cycles_for_item(conn: sqlite3.Connection, item_id: str) -> None:
    conn.execute("DELETE FROM settings WHERE key LIKE ?", (f"_cycle:%:item:{item_id}",))


def _queued_for_item(conn: sqlite3.Connection, item_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM queue_positions "
        "WHERE state IN ('pending', 'prepared') AND source_ref LIKE ?",
        (f"%:item:{item_id}:cycle:%",),
    ).fetchone()
    return int(row["count"] or 0)


def _rotation_item_valid(conn: sqlite3.Connection, item_id: str) -> bool:
    row = conn.execute(
        "SELECT ri.playlist_id, ri.enabled FROM rotation_items ri WHERE ri.id = ?", (item_id,)
    ).fetchone()
    if row is None or not row["enabled"] or not row["playlist_id"]:
        return False
    return bool(playlist_track_ids(conn, str(row["playlist_id"])))


def _source_key_valid(conn: sqlite3.Connection, key: str, now: datetime) -> bool:
    """Is a queued source key still reachable under the current settings?"""

    if not key:
        return False
    if key == "library":
        return bool(library_track_ids(conn))
    parts = key.split(":")
    if not parts:
        return False
    if parts[0] == "playlist" and len(parts) > 1:
        playlist_id = parts[1]
        return (
            db.get_setting(conn, "default_playlist_id") == playlist_id
            and bool(playlist_track_ids(conn, playlist_id))
        )
    if parts[0] == "override" and len(parts) > 1:
        row = conn.execute(
            "SELECT active, expires_at FROM overrides WHERE id = ?", (parts[1],)
        ).fetchone()
        if row is None or not row["active"]:
            return False
        expires = parse_iso(row["expires_at"])
        if expires is not None and expires <= now:
            return False
        if "item" in parts:
            index = parts.index("item")
            if index + 1 >= len(parts):
                return False
            return _rotation_item_valid(conn, parts[index + 1])
        if "playlist" in parts:
            index = parts.index("playlist")
            if index + 1 >= len(parts):
                return False
            return bool(playlist_track_ids(conn, parts[index + 1]))
        return True
    if parts[0] == "schedule" and len(parts) > 1:
        rule = conn.execute(
            "SELECT * FROM schedule_rules WHERE id = ?", (parts[1],)
        ).fetchone()
        if rule is None or not rule["enabled"]:
            return False
        if db.get_setting(conn, "schedule_enabled", "1") != "1":
            return False
        local = scheduler.local_now(now, scheduler.station_tz(conn))
        if not scheduler.rule_matches(rule, local):
            return False
        if "item" in parts:
            index = parts.index("item")
            if index + 1 >= len(parts):
                return False
            return _rotation_item_valid(conn, parts[index + 1])
        if "playlist" in parts:
            index = parts.index("playlist")
            if index + 1 >= len(parts):
                return False
            return bool(playlist_track_ids(conn, parts[index + 1]))
        return True
    if parts[0] == "rotation" and "item" in parts:
        index = parts.index("item")
        if index + 1 >= len(parts):
            return False
        return _rotation_item_valid(conn, parts[index + 1])
    return True


def _current_rotation_item(
    conn: sqlite3.Connection, rotation: sqlite3.Row
) -> sqlite3.Row | None:
    items = _rotation_items(conn, str(rotation["id"]))
    if not items:
        return None
    current_id = _cursor_item(conn, str(rotation["id"]))
    current = next((item for item in items if str(item["id"]) == current_id), None)
    if current is None:
        if (rotation["mode"] or "sequential") == "random":
            current = random.SystemRandom().choice(items)
        else:
            current = items[0]
        _set_cursor(conn, str(rotation["id"]), str(current["id"]))
        _set_played(conn, str(current["id"]), 0)
    if (rotation["mode"] or "sequential") == "by_count":
        if _played(conn, str(current["id"])) >= max(1, int(current["track_count"] or 1)):
            current = _advance_rotation(conn, rotation, str(current["id"]))
    return current


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------


def deactivate_expired_overrides(conn: sqlite3.Connection, now: datetime) -> list[str]:
    rows = conn.execute(
        "SELECT id, expires_at FROM overrides WHERE active = 1 AND expires_at IS NOT NULL"
    ).fetchall()
    expired = []
    for row in rows:
        expires = parse_iso(row["expires_at"])
        if expires is not None and expires <= now:
            conn.execute("UPDATE overrides SET active = 0 WHERE id = ?", (row["id"],))
            expired.append(str(row["id"]))
    return expired


def _playlist_source(
    conn: sqlite3.Connection,
    playlist_id: str,
    kind: str,
    key: str,
    *,
    rule_id: str | None = None,
    override_id: str | None = None,
    allow_empty: bool = False,
    fallback_allowed: bool = False,
) -> Source | None:
    if playlist_id == SYSTEM_PLAYLIST_ID:
        return _system_source(
            conn,
            kind=kind,
            key=key,
            rule_id=rule_id,
            override_id=override_id,
            fallback_allowed=fallback_allowed,
            allow_empty=allow_empty,
        )
    playlist = get_playlist(conn, playlist_id)
    if playlist is None:
        return None
    if not playlist_track_ids(conn, playlist_id) and not allow_empty:
        _warn_once(
            conn, f"empty:{key}", "Пустой плейлист пропущен", {"playlist_id": playlist_id}
        )
        return None
    return Source(
        kind=kind,
        key=key,
        playlist_id=playlist_id,
        rule_id=rule_id,
        override_id=override_id,
        name=str(playlist["name"] or ""),
        repeat_mode=str(playlist["repeat_mode"] or "none"),
        shuffle=bool(playlist["shuffle"]),
        allow_empty=allow_empty,
        fallback_allowed=fallback_allowed,
    )


def _system_source(
    conn: sqlite3.Connection,
    *,
    kind: str = "library",
    key: str = SYSTEM_PLAYLIST_ID,
    rule_id: str | None = None,
    override_id: str | None = None,
    fallback_allowed: bool = False,
    allow_empty: bool = True,
) -> Source:
    row = system_playlist_row(conn)
    return Source(
        kind=kind,
        key=key,
        playlist_id=SYSTEM_PLAYLIST_ID,
        rule_id=rule_id,
        override_id=override_id,
        name=SYSTEM_PLAYLIST_NAME,
        repeat_mode=str((row["repeat_mode"] if row is not None else "all") or "none"),
        shuffle=bool(row["shuffle"]) if row is not None else True,
        allow_empty=allow_empty,
        fallback_allowed=fallback_allowed,
        is_system=True,
    )


def _rotation_source(
    conn: sqlite3.Connection,
    prefix: str,
    *,
    kind: str,
    rule_id: str | None = None,
    override_id: str | None = None,
) -> Source | None:
    rotation = get_rotation(conn)
    item = _current_rotation_item(conn, rotation)
    if item is None:
        _warn_once(conn, "rotation:empty", "В ротации нет доступных плейлистов", None)
        return None
    playlist = get_playlist(conn, item["playlist_id"])
    if playlist is None:
        return None
    key = f"{prefix}:item:{item['id']}"
    return Source(
        kind=kind,
        key=key,
        playlist_id=str(item["playlist_id"]),
        rotation_id=str(rotation["id"]),
        rotation_item_id=str(item["id"]),
        rule_id=rule_id,
        override_id=override_id,
        name=str(playlist["name"] or ""),
        repeat_mode="none",
        shuffle=bool(playlist["shuffle"]),
        by_count=(rotation["mode"] or "sequential") == "by_count",
        fallback_allowed=kind in ("schedule", "rotation"),
        is_system=str(item["playlist_id"]) == SYSTEM_PLAYLIST_ID,
    )


def resolve_source(
    conn: sqlite3.Connection,
    now: datetime,
    excluded: frozenset[str] | set[str] = frozenset(),
) -> Source | None:
    deactivate_expired_overrides(conn, now)

    override = conn.execute(
        "SELECT * FROM overrides WHERE active = 1 ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if override is not None:
        kind = str(override["kind"] or "playlist")
        if kind == "playlist" and override["playlist_id"]:
            key = f"override:{override['id']}:playlist:{override['playlist_id']}"
            if key not in excluded:
                source = _playlist_source(
                    conn,
                    str(override["playlist_id"]),
                    "override",
                    key,
                    override_id=str(override["id"]),
                    allow_empty=True,
                )
                if source is not None:
                    return source
        elif kind == "rotation":
            key = f"override:{override['id']}:rotation"
            source = _rotation_source(
                conn, key, kind="override", override_id=str(override["id"])
            )
            if source is not None and source.key not in excluded:
                return source

    for rule in scheduler.active_rules(conn, now):
        source = None
        if rule["playlist_id"]:
            key = f"schedule:{rule['id']}:playlist:{rule['playlist_id']}"
            if key not in excluded:
                source = _playlist_source(
                    conn,
                    str(rule["playlist_id"]),
                    "schedule",
                    key,
                    rule_id=str(rule["id"]),
                    allow_empty=False,
                    fallback_allowed=True,
                )
        elif int(rule["rotation"] or 0):
            key = f"schedule:{rule['id']}:rotation"
            source = _rotation_source(conn, key, kind="schedule", rule_id=str(rule["id"]))
            if source is not None and source.key in excluded:
                source = None
        if source is not None:
            return source
        _warn_once(
            conn,
            f"schedule-source:{rule['id']}",
            "Правило расписания пропущено: источник пуст или недоступен",
            {"rule_id": rule["id"], "playlist_id": rule["playlist_id"]},
        )

    source = _rotation_source(conn, "rotation", kind="rotation")
    if source is not None and source.key not in excluded:
        return source

    default_id = db.get_setting(conn, "default_playlist_id")
    if default_id == SYSTEM_PLAYLIST_ID:
        return _system_source(conn)
    if default_id:
        key = f"playlist:{default_id}"
        playlist = get_playlist(conn, default_id)
        if playlist is not None:
            return _playlist_source(
                conn,
                default_id,
                "playlist",
                key,
                allow_empty=True,
                fallback_allowed=False,
            )
        # Unknown default id falls back to the system playlist.

    return _system_source(conn)


# ---------------------------------------------------------------------------
# Cycles (shuffle / repeat)
# ---------------------------------------------------------------------------


def _cycle_setting_key(key: str) -> str:
    return f"_cycle:{key}"


def _last_played_for_key(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT track_id FROM history WHERE source_ref LIKE ? ORDER BY started_at DESC, id DESC LIMIT 1",
        (key + ":cycle:%",),
    ).fetchone()
    return str(row["track_id"]) if row and row["track_id"] else None


def _source_signature(conn: sqlite3.Connection, source: Source) -> dict:
    ids = playlist_track_ids(conn, source.playlist_id) if source.playlist_id else []
    return {
        "ids": ids,
        "shuffle": bool(source.shuffle),
        "repeat": source.repeat_mode,
    }


def _source_setting_key(key: str) -> str:
    return f"_source_sig:{key}"


def _invalidate_source_key(conn: sqlite3.Connection, key: str) -> int:
    rows = conn.execute(
        "SELECT id FROM queue_positions WHERE state IN ('pending', 'prepared') "
        "AND manual = 0 AND source_ref LIKE ?",
        (key + ":cycle:%",),
    ).fetchall()
    for row in rows:
        conn.execute("DELETE FROM queue_positions WHERE id = ?", (row["id"],))
    db.delete_setting(conn, _cycle_setting_key(key))
    return len(rows)


def invalidate_position_refs(conn: sqlite3.Connection, fragments: list[str]) -> int:
    """Delete future positions whose source_ref contains any fragment."""

    if not fragments:
        return 0
    removed = 0
    keys: set[str] = set()
    for row in future_positions(conn):
        if row["manual"]:
            continue
        ref = str(row["source_ref"] or "")
        if any(fragment in ref for fragment in fragments):
            conn.execute("DELETE FROM queue_positions WHERE id = ?", (row["id"],))
            keys.add(key_of(ref))
            removed += 1
    for key in keys:
        db.delete_setting(conn, _cycle_setting_key(key))
    return removed


def invalidate_playlist_sources(conn: sqlite3.Connection, playlist_id: str) -> int:
    """Drop stale future positions for every source backed by a playlist."""

    keys: set[str] = set()
    for row in future_positions(conn):
        if row["manual"]:
            continue
        ref = row["source_ref"] or ""
        parts = ref.split(":")
        if "playlist" in parts:
            index = parts.index("playlist")
            if index + 1 < len(parts) and parts[index + 1] == playlist_id:
                keys.add(key_of(ref))
        if "item" in parts:
            index = parts.index("item")
            if index + 1 < len(parts):
                item = conn.execute(
                    "SELECT playlist_id FROM rotation_items WHERE id = ?",
                    (parts[index + 1],),
                ).fetchone()
                if item is not None and str(item["playlist_id"]) == playlist_id:
                    keys.add(key_of(ref))
    if playlist_id == SYSTEM_PLAYLIST_ID:
        keys.add(SYSTEM_PLAYLIST_ID)
    removed = 0
    for key in keys:
        removed += _invalidate_source_key(conn, key)
    db.delete_setting(conn, _source_setting_key(playlist_id))
    return removed


def _apply_source_signature(conn: sqlite3.Connection, source: Source) -> None:
    signature = _source_signature(conn, source)
    stored = db.get_json_setting(conn, _source_setting_key(source.key), None)
    if stored is not None and stored != signature:
        if source.is_system and (
            bool(stored.get("shuffle")) != signature["shuffle"]
            or stored.get("repeat") != signature["repeat"]
        ):
            _invalidate_source_key(conn, source.key)
        elif source.is_system:
            removed_ids = set(stored.get("ids") or []) - set(signature["ids"])
            if removed_ids:
                for row in future_positions(conn):
                    if (
                        not row["manual"]
                        and key_of(row["source_ref"]) == source.key
                        and str(row["track_id"]) in removed_ids
                    ):
                        conn.execute(
                            "DELETE FROM queue_positions WHERE id = ?", (row["id"],)
                        )
        else:
            _invalidate_source_key(conn, source.key)
    if stored != signature:
        db.set_json_setting(conn, _source_setting_key(source.key), signature)


def source_diagnostic(conn: sqlite3.Connection, now: datetime) -> dict:
    """Reason code + source metadata for status/no_music decisions."""

    source = resolve_source(conn, now)
    if source is None:
        return {"source": None, "reason": "no_source"}
    ids = playlist_track_ids(conn, source.playlist_id) if source.playlist_id else []
    if not ids:
        reason = "library_empty" if source.is_system else "playlist_empty"
        return {"source": source, "reason": reason}
    if source.repeat_mode not in ("all", "one"):
        state = db.get_json_setting(conn, _cycle_setting_key(source.key), None)
        if state is not None and state.get("started") and not state.get("remaining"):
            queued = [
                row
                for row in future_positions(conn)
                if key_of(row["source_ref"]) == source.key and not row["manual"]
            ]
            if not queued:
                return {"source": source, "reason": "playlist_exhausted"}
    return {"source": source, "reason": None}


def _current_source_has_future(conn: sqlite3.Connection, key: str) -> bool:
    return any(
        key_of(row["source_ref"]) == key for row in future_positions(conn)
    )



def _cycle_positions(
    conn: sqlite3.Connection, key: str, cycle: int, states: tuple[str, ...]
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in states)
    return conn.execute(
        f"SELECT * FROM queue_positions WHERE source_ref = ? AND state IN ({placeholders})",
        (f"{key}:cycle:{cycle}", *states),
    ).fetchall()


def _cycle_next(
    conn: sqlite3.Connection,
    key: str,
    ids: list[str],
    *,
    shuffle: bool,
    repeat: str,
    exclude_ids: set[str],
    allow_duplicates: bool,
) -> Choice:
    state = db.get_json_setting(conn, _cycle_setting_key(key), None)
    valid = set(ids)

    for attempt in range(2):
        if state is None:
            cycle = 0
            previous_tail = None
            remaining: list[str] = []
            finished = False
        else:
            cycle = int(state.get("cycle") or 0)
            remaining = [item for item in state.get("remaining") or [] if item in valid]
            previous_tail = state.get("tail")
            finished = bool(state.get("started")) and not remaining

        if state is None or finished:
            if finished:
                if not allow_duplicates and _cycle_positions(
                    conn, key, cycle, ("pending", "prepared")
                ):
                    return Choice(None, cycle, "blocked")
                if repeat == "none":
                    return Choice(None, cycle, "exhausted")
            order = list(ids)
            if shuffle:
                random.SystemRandom().shuffle(order)
            if len(order) > 1 and previous_tail and order[0] == previous_tail:
                swap_index = random.SystemRandom().randrange(1, len(order))
                order[0], order[swap_index] = order[swap_index], order[0]
            remaining = order
            cycle += 1
            previous_tail = order[-1] if order else None
            state = {"cycle": cycle, "remaining": remaining, "started": True, "tail": previous_tail}

        while remaining:
            candidate = remaining[0]
            if candidate not in valid:
                remaining.pop(0)
                continue
            if not allow_duplicates and candidate in exclude_ids:
                remaining.pop(0)
                continue
            remaining.pop(0)
            db.set_json_setting(
                conn,
                _cycle_setting_key(key),
                {
                    "cycle": cycle,
                    "remaining": remaining,
                    "started": True,
                    "tail": state.get("tail"),
                },
            )
            return Choice(candidate, cycle, "ok")

        db.set_json_setting(
            conn,
            _cycle_setting_key(key),
            {"cycle": cycle, "remaining": [], "started": True, "tail": state.get("tail")},
        )
        if not allow_duplicates and _cycle_positions(
            conn, key, cycle, ("pending", "prepared")
        ):
            return Choice(None, cycle, "blocked")
        if repeat == "all" and attempt == 0:
            continue
        return Choice(None, cycle, "blocked")
    return Choice(None, 0, "blocked")


def _append_library_tracks(conn: sqlite3.Connection, key: str, ids: list[str]) -> None:
    """Append new library tracks to the remaining part of the current cycle."""

    state = db.get_json_setting(conn, _cycle_setting_key(key), None)
    if not state or not state.get("started"):
        return
    valid = set(ids)
    cycle = int(state.get("cycle") or 0)
    remaining = [item for item in state.get("remaining") or [] if item in valid]
    played = {
        str(row["track_id"])
        for row in conn.execute(
            "SELECT track_id FROM history WHERE source_ref LIKE ?",
            (f"{key}:cycle:{cycle}",),
        ).fetchall()
        if row["track_id"]
    }
    queued = _all_queued_track_ids(conn)
    appended = False
    for track_id in ids:
        if (
            track_id not in remaining
            and track_id not in played
            and track_id not in queued
        ):
            remaining.append(track_id)
            appended = True
    db.set_json_setting(
        conn,
        _cycle_setting_key(key),
        {
            **state,
            "remaining": remaining,
            "tail": remaining[-1] if appended and remaining else state.get("tail"),
        },
    )


def next_track(
    conn: sqlite3.Connection,
    source: Source,
    now: datetime,
    exclude_ids: set[str],
) -> Choice:
    if source.playlist_id == SYSTEM_PLAYLIST_ID and not source.rotation_item_id:
        ids = playlist_track_ids(conn, SYSTEM_PLAYLIST_ID)
        if not ids:
            return Choice(None, reason="empty")
        _append_library_tracks(conn, source.key, ids)
        return _cycle_next(
            conn,
            source.key,
            ids,
            shuffle=source.shuffle,
            repeat=source.repeat_mode,
            exclude_ids=exclude_ids,
            allow_duplicates=source.repeat_mode == "one",
        )

    if not source.playlist_id:
        return Choice(None, reason="exhausted")
    ids = playlist_track_ids(conn, source.playlist_id)
    if not ids:
        return Choice(None, reason="exhausted")
    if source.playlist_id == SYSTEM_PLAYLIST_ID:
        _append_library_tracks(conn, source.key, ids)

    if source.rotation_item_id:
        rotation = get_rotation(conn)
        if source.by_count:
            count = max(1, _item_track_count(conn, source))
            if _played(conn, source.rotation_item_id) + _queued_for_item(
                conn, source.rotation_item_id
            ) >= count:
                _advance_rotation(conn, rotation, source.rotation_item_id)
                return Choice(None, reason="advanced")
        choice = _cycle_next(
            conn,
            source.key,
            ids,
            shuffle=source.shuffle,
            repeat="all" if source.by_count else "none",
            exclude_ids=exclude_ids,
            allow_duplicates=source.by_count,
        )
        if choice.reason == "ok":
            return choice
        if choice.reason == "blocked" and not source.by_count:
            # The current playlist pass is scheduled; plan the next rotation item.
            advanced = _advance_rotation(conn, rotation, source.rotation_item_id)
            if advanced is not None and str(advanced["id"]) != source.rotation_item_id:
                return Choice(None, reason="advanced")
            return choice
        if choice.reason == "blocked":
            return choice
        advanced = _advance_rotation(conn, rotation, source.rotation_item_id)
        if advanced is None:
            return Choice(None, reason="exhausted")
        if str(advanced["id"]) == source.rotation_item_id:
            # Single item rotation: restart the playlist cycle immediately.
            db.delete_setting(conn, _cycle_setting_key(source.key))
            return _cycle_next(
                conn,
                source.key,
                ids,
                shuffle=source.shuffle,
                repeat="all",
                exclude_ids=exclude_ids,
                allow_duplicates=True,
            )
        return Choice(None, reason="advanced")

    if source.repeat_mode == "one":
        last = _last_played_for_key(conn, source.key) or ids[0]
        if last not in ids:
            last = ids[0]
        state = db.get_json_setting(conn, _cycle_setting_key(source.key), None) or {}
        cycle = int(state.get("cycle") or 0)
        return Choice(last, cycle, "ok")

    repeat = source.repeat_mode
    return _cycle_next(
        conn,
        source.key,
        ids,
        shuffle=source.shuffle,
        repeat=repeat,
        exclude_ids=exclude_ids,
        allow_duplicates=repeat == "one",
    )


def _item_track_count(conn: sqlite3.Connection, source: Source) -> int:
    row = conn.execute(
        "SELECT track_count FROM rotation_items WHERE id = ?", (source.rotation_item_id,)
    ).fetchone()
    try:
        return max(1, int(row["track_count"])) if row else 1
    except (TypeError, ValueError):
        return 1


# ---------------------------------------------------------------------------
# Queue rows
# ---------------------------------------------------------------------------


def future_positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM queue_positions WHERE state IN ('pending', 'prepared') ORDER BY seq ASC"
    ).fetchall()


def current_position(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM queue_positions WHERE state = 'current' ORDER BY seq ASC LIMIT 1"
    ).fetchone()


def _renumber(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id FROM queue_positions WHERE state IN ('pending', 'prepared') "
        "ORDER BY seq ASC, created_at ASC, id ASC"
    ).fetchall()
    for index, row in enumerate(rows):
        conn.execute("UPDATE queue_positions SET seq = ? WHERE id = ?", (index, row["id"]))


def _insert_position(
    conn: sqlite3.Connection,
    track_id: str,
    source_kind: str,
    source_ref: str,
    *,
    manual: bool,
    now: datetime,
    at_head: bool = False,
) -> str:
    position_id = uuid.uuid4().hex
    rows = conn.execute(
        "SELECT COALESCE(MAX(seq), -1) AS seq, COALESCE(MIN(seq), 0) AS first "
        "FROM queue_positions WHERE state IN ('pending', 'prepared')"
    ).fetchone()
    current_max = int(rows["seq"]) if rows["seq"] is not None else -1
    current_first = int(rows["first"]) if rows["first"] is not None else 0
    seq = current_first - 1 if at_head else current_max + 1
    conn.execute(
        "INSERT INTO queue_positions(id, track_id, source_kind, source_ref, manual, seq, state, "
        "created_at) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?)",
        (
            position_id,
            track_id,
            source_kind,
            source_ref,
            1 if manual else 0,
            seq,
            to_iso(now),
        ),
    )
    _renumber(conn)
    return position_id


@_locked
def ensure_queue(conn: sqlite3.Connection, settings: Settings, now: datetime) -> list[str]:
    """Fill future positions to ``queue_depth`` and keep the current source stable."""

    depth = max(1, db.get_int_setting(conn, "queue_depth", 6))
    source = resolve_source(conn, now)
    if source is None:
        conn.execute(
            "DELETE FROM queue_positions WHERE state IN ('pending', 'prepared') AND manual = 0"
        )
        _renumber(conn)
        conn.commit()
        return []
    _apply_source_signature(conn, source)
    future = future_positions(conn)
    planned_keys = {key_of(row["source_ref"]) for row in future if not row["manual"]}
    head = next(
        (
            row
            for row in future
            if not row["manual"] and row["state"] == "pending"
        ),
        None,
    )
    stale = head is not None and not _source_key_valid(
        conn, key_of(head["source_ref"]), now
    )
    if planned_keys and (source.key not in planned_keys or stale):
        dropped_keys = {
            key_of(row["source_ref"])
            for row in future
            if row["state"] in ("pending", "prepared") and not row["manual"]
        }
        conn.execute(
            "DELETE FROM queue_positions WHERE state IN ('pending', 'prepared') AND manual = 0"
        )
        for key in dropped_keys:
            if key:
                db.delete_setting(conn, f"_cycle:{key}")
        _renumber(conn)
        future = future_positions(conn)

    created: list[str] = []
    excluded_sources: set[str] = set()
    guard = 0
    while True:
        future = future_positions(conn)
        generated = [row for row in future if not row["manual"]]
        if len(generated) > depth:
            extras = [row for row in generated[depth:] if row["state"] == "pending"]
            for row in extras:
                conn.execute("DELETE FROM queue_positions WHERE id = ?", (row["id"],))
            if extras:
                _renumber(conn)
            break
        if len(generated) >= depth:
            break
        guard += 1
        if guard > depth * 4 + 8:
            break
        source = resolve_source(conn, now, excluded_sources)
        if source is None:
            break
        exclude_tracks = set()
        if source.repeat_mode != "one" and not source.by_count:
            exclude_tracks = {str(row["track_id"]) for row in future if not row["manual"]}
        choice = next_track(conn, source, now, exclude_tracks)
        if choice.reason == "advanced":
            continue
        if choice.reason == "blocked":
            break
        if choice.reason in ("exhausted", "empty") or not choice.track_id:
            if source.fallback_allowed:
                excluded_sources.add(source.key)
                continue
            break
        created.append(
            _insert_position(
                conn,
                choice.track_id,
                source.kind,
                source.ref(choice.cycle),
                manual=False,
                now=now,
            )
        )
    conn.commit()
    return created


def _all_queued_track_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT track_id FROM queue_positions WHERE state IN ('pending', 'prepared', 'current')"
    ).fetchall()
    return {str(row["track_id"]) for row in rows}


@_locked
def enqueue_manual(
    conn: sqlite3.Connection,
    settings: Settings,
    track_id: str,
    mode: str,
    now: datetime,
) -> str:
    track = conn.execute(
        "SELECT * FROM tracks WHERE id = ? AND pending_delete = 0", (track_id,)
    ).fetchone()
    if track is None:
        raise ApiError(404, "track_not_found", "Трек не найден.")
    if mode not in ("play-now", "play-next", "queue"):
        raise ApiError(422, "invalid_mode", "Недопустимый способ добавления в очередь.")
    position_id = _insert_position(
        conn,
        track_id,
        "manual",
        f"manual:{mode}",
        manual=True,
        now=now,
        at_head=mode in ("play-now", "play-next"),
    )
    conn.commit()
    return position_id


@_locked
def move_position(conn: sqlite3.Connection, position_id: str, move_to: int) -> None:
    row = conn.execute("SELECT * FROM queue_positions WHERE id = ?", (position_id,)).fetchone()
    if row is None:
        raise ApiError(404, "position_not_found", "Позиция очереди не найдена.")
    if row["state"] != "pending":
        raise ApiError(
            409,
            "position_locked",
            "Позиция уже подготовлена движком и не может быть изменена.",
        )
    ordered = [
        str(item["id"])
        for item in conn.execute(
            "SELECT id FROM queue_positions WHERE state IN ('pending', 'prepared') "
            "ORDER BY seq ASC, created_at ASC, id ASC"
        ).fetchall()
    ]
    if position_id not in ordered:
        raise ApiError(404, "position_not_found", "Позиция очереди не найдена.")
    ordered.remove(position_id)
    target = max(0, min(int(move_to), len(ordered)))
    ordered.insert(target, position_id)
    for index, identifier in enumerate(ordered):
        conn.execute("UPDATE queue_positions SET seq = ? WHERE id = ?", (index, identifier))
    conn.commit()


@_locked
def delete_position(conn: sqlite3.Connection, settings: Settings, position_id: str) -> None:
    row = conn.execute("SELECT * FROM queue_positions WHERE id = ?", (position_id,)).fetchone()
    if row is None:
        raise ApiError(404, "position_not_found", "Позиция очереди не найдена.")
    if row["state"] != "pending":
        raise ApiError(
            409,
            "position_locked",
            "Позиция уже подготовлена движком и не может быть удалена.",
        )
    conn.execute("DELETE FROM queue_positions WHERE id = ?", (position_id,))
    _renumber(conn)
    conn.commit()
    from .util import remove_queue_link

    remove_queue_link(settings, position_id)


@_locked
def shuffle_pending(conn: sqlite3.Connection) -> int:
    prepared = conn.execute(
        "SELECT id FROM queue_positions WHERE state = 'prepared' ORDER BY seq ASC"
    ).fetchall()
    pending = conn.execute(
        "SELECT id FROM queue_positions WHERE state = 'pending' AND manual = 0 ORDER BY seq ASC"
    ).fetchall()
    manual = conn.execute(
        "SELECT id FROM queue_positions WHERE state = 'pending' AND manual = 1 ORDER BY seq ASC"
    ).fetchall()
    pending_ids = [str(item["id"]) for item in pending]
    random.SystemRandom().shuffle(pending_ids)
    manual_ids = [str(item["id"]) for item in manual]
    ordered = [str(item["id"]) for item in prepared] + manual_ids + pending_ids
    for index, identifier in enumerate(ordered):
        conn.execute("UPDATE queue_positions SET seq = ? WHERE id = ?", (index, identifier))
    conn.commit()
    return len(pending_ids)


@_locked
def clear_manual(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "DELETE FROM queue_positions WHERE state = 'pending' AND manual = 1"
    )
    _renumber(conn)
    conn.commit()
    return cursor.rowcount or 0


# ---------------------------------------------------------------------------
# Engine sync
# ---------------------------------------------------------------------------


def build_engine_lines(conn: sqlite3.Connection, settings: Settings) -> list[str]:
    """Prepare symlinks and return the absolute link paths for Liquidsoap."""

    from .util import ensure_queue_link

    lines: list[str] = []
    for row in future_positions(conn)[:ENGINE_PREFETCH]:
        track = conn.execute(
            "SELECT * FROM tracks WHERE id = ? AND pending_delete = 0", (row["track_id"],)
        ).fetchone()
        if track is None:
            continue
        try:
            path = media.resolve_track_path(settings, track)
        except ApiError:
            continue
        if not path.exists():
            continue
        link = ensure_queue_link(settings, str(row["id"]), path)
        lines.append(str(link))
    return lines


def prepared_signature(conn: sqlite3.Connection) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(row["id"]), str(row["track_id"]))
        for row in future_positions(conn)[:ENGINE_PREFETCH]
    )


def _cleanup_stale_links(settings: Settings, keep_ids: list[str]) -> None:
    link_dir = Path(settings.queue_link_dir)
    if not link_dir.exists():
        return
    keep = set(keep_ids)
    for entry in link_dir.glob("*.mp3"):
        if entry.stem in keep:
            continue
        try:
            entry.unlink()
        except OSError:
            continue


@_locked
def _drop_unplayable_positions(conn: sqlite3.Connection, settings: Settings) -> int:
    """Remove queued positions whose track row or file no longer exists."""

    removed = 0
    for row in future_positions(conn):
        track = conn.execute("SELECT * FROM tracks WHERE id = ?", (row["track_id"],)).fetchone()
        playable = False
        if track is not None and not track["pending_delete"]:
            try:
                playable = media.resolve_track_path(settings, track).exists()
            except ApiError:
                playable = False
        if playable:
            continue
        conn.execute("DELETE FROM queue_positions WHERE id = ?", (row["id"],))
        from .util import remove_queue_link

        remove_queue_link(settings, str(row["id"]))
        removed += 1
        _warn_once(
            conn,
            f"missing:{row['track_id']}",
            "Файл трека недоступен, позиция удалена из очереди",
            {"position_id": row["id"], "track_id": row["track_id"]},
        )
    if removed:
        _renumber(conn)
    return removed


@_locked
def sync_engine(
    conn: sqlite3.Connection,
    settings: Settings,
    engine,
    *,
    now: datetime | None = None,
    force: bool = False,
    sync_state: dict | None = None,
) -> bool:
    """Refresh runtime symlinks, write the engine queue file and call ``vd_sync``."""

    from .util import utcnow

    now = now or utcnow()
    removed = _drop_unplayable_positions(conn, settings)
    signature = prepared_signature(conn)
    if sync_state is not None and not force and not removed:
        last_signature = sync_state.get("signature")
        last_at = sync_state.get("at")
        if (
            not sync_state.get("failed")
            and not db.get_setting(conn, "_engine_sync_error")
            and signature == last_signature
            and isinstance(last_at, datetime)
            and 0 <= (now - last_at).total_seconds() < ENGINE_SYNC_MIN_INTERVAL
        ):
            return True
    lines = build_engine_lines(conn, settings)
    content = "\n".join(lines)
    if content:
        content += "\n"
    diagnostic = source_diagnostic(conn, now)
    source = diagnostic.get("source")
    playable_source = bool(
        source is not None
        and diagnostic.get("reason") is None
        and playlist_track_ids(conn, source.playlist_id)
    )
    empty_valid = False
    if not lines:
        if playable_source:
            db.set_setting(conn, "_engine_sync_error", to_iso(now) or "")
            db.log_event(
                conn,
                "error",
                "engine",
                "Пустая синхронизация при наличии источника отклонена",
                None,
            )
            if sync_state is not None:
                sync_state["failed"] = True
                sync_state.pop("signature", None)
                sync_state.pop("at", None)
            conn.commit()
            return False
        # No playable source (empty library, empty/exhausted user source):
        # an empty queue is the correct state, not a sync failure.
        empty_valid = True
    current = current_position(conn)
    keep_ids = [Path(line).stem for line in lines]
    if current is not None:
        keep_ids.append(str(current["id"]))
    try:
        settings.ensure_directories()
        from .util import atomic_write_text

        atomic_write_text(settings.engine_queue_file, content)
        _cleanup_stale_links(settings, keep_ids)
    except OSError:
        db.log_event(conn, "error", "engine", "Не удалось записать файл очереди движка", None)
        if sync_state is not None:
            sync_state["failed"] = True
            sync_state.pop("signature", None)
            sync_state.pop("at", None)
        conn.commit()
        return False
    if hasattr(engine, "sync_report"):
        report = engine.sync_report()
        ok = bool(report.get("ok"))
        resolved = report.get("resolved")
        failed = report.get("failed")
    else:
        ok = bool(engine and engine.sync())
        resolved = len(lines)
        failed = 0
    if ok and resolved is None:
        resolved = len(lines)
    if ok and lines and (resolved or 0) == 0:
        ok = False
        db.set_setting(conn, "_engine_sync_error", to_iso(now) or "")
        db.log_event(
            conn,
            "error",
            "engine",
            "Движок не разрешил ни одной позиции очереди",
            {"lines": len(lines), "failed": failed},
        )
    if empty_valid:
        # A failed command while there is legitimately no playable source does
        # not make the station unhealthy; the engine will start silence or keep
        # its empty state. Clear the failure and wait for a real source/track.
        ok = True
        resolved = 0
        failed = 0
    if ok:
        if sync_state is not None:
            sync_state["signature"] = signature
            sync_state["at"] = now
            sync_state["failed"] = False
        ids = [Path(line).stem for line in lines]
        prepared_ids = ids[: int(resolved or 0)] if resolved is not None else ids
        if prepared_ids:
            placeholders = ",".join("?" for _ in prepared_ids)
            conn.execute(
                f"UPDATE queue_positions SET state = 'prepared' WHERE id IN ({placeholders})",
                prepared_ids,
            )
            conn.execute(
                f"UPDATE queue_positions SET state = 'pending' WHERE state = 'prepared' "
                f"AND id NOT IN ({placeholders})",
                prepared_ids,
            )
        else:
            conn.execute("UPDATE queue_positions SET state = 'pending' WHERE state = 'prepared'")
        if failed:
            db.log_event(
                conn,
                "warning",
                "engine",
                "Движок не подготовил часть очереди",
                {"failed": failed, "resolved": resolved, "positions": len(ids)},
            )
        db.delete_setting(conn, "_engine_sync_error")
        db.set_setting(conn, "engine_last_error", "")
        db.set_setting(conn, "_engine_start_synced", "1")
        if db.get_setting(conn, "_sync_warned") == "1":
            db.delete_setting(conn, "_sync_warned")
            db.log_event(conn, "info", "engine", "Синхронизация очереди с движком восстановлена", None)
    else:
        conn.execute("UPDATE queue_positions SET state = 'pending' WHERE state = 'prepared'")
        db.set_setting(conn, "_engine_sync_error", to_iso(now) or "")
        if sync_state is not None:
            sync_state["failed"] = True
            sync_state.pop("signature", None)
            sync_state.pop("at", None)
        if db.get_setting(conn, "_sync_warned") != "1":
            db.set_setting(conn, "_sync_warned", "1")
            db.log_event(
                conn,
                "warning",
                "engine",
                "Движок недоступен, очередь сохранена и будет синхронизирована позже",
                None,
            )
    conn.commit()
    return ok


# ---------------------------------------------------------------------------
# Track start callback
# ---------------------------------------------------------------------------


@_locked
def handle_track_start(
    conn: sqlite3.Connection,
    settings: Settings,
    position_id: str,
    started_at: datetime,
    now: datetime,
) -> dict:
    position = conn.execute(
        "SELECT * FROM queue_positions WHERE id = ?", (position_id,)
    ).fetchone()
    if position is None:
        db.log_event(
            conn,
            "warning",
            "engine",
            "Движок сообщил о неизвестной позиции",
            {"position_id": position_id},
        )
        conn.commit()
        return {"ok": False, "reason": "unknown_position"}

    if position["state"] == "current":
        return {"ok": True, "duplicate": True, "track_id": position["track_id"]}

    track = conn.execute("SELECT * FROM tracks WHERE id = ?", (position["track_id"],)).fetchone()
    previous = conn.execute("SELECT * FROM playback_state WHERE singleton = 1").fetchone()
    stamp = to_iso(started_at)
    if previous is not None and previous["position_id"]:
        conn.execute(
            "UPDATE history SET ended_at = ? WHERE position_id = ? AND ended_at IS NULL",
            (stamp, previous["position_id"]),
        )
        conn.execute(
            "DELETE FROM queue_positions WHERE id = ? AND state = 'current'",
            (previous["position_id"],),
        )
    conn.execute(
        "UPDATE queue_positions SET state = 'current', started_at = ?, engine_ref = ? WHERE id = ?",
        (stamp, position_id, position_id),
    )
    title = track["title"] if track else None
    artist = track["artist"] if track else None
    album = track["album"] if track else None
    conn.execute(
        "INSERT INTO history(track_id, position_id, started_at, ended_at, source_kind, source_ref, "
        "title, artist, album) VALUES(?, ?, ?, NULL, ?, ?, ?, ?, ?)",
        (
            position["track_id"],
            position_id,
            stamp,
            position["source_kind"],
            position["source_ref"],
            title,
            artist,
            album,
        ),
    )
    conn.execute(
        "INSERT INTO playback_state(singleton, position_id, track_id, started_at, source_kind, "
        "source_ref, updated_at) VALUES(1, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(singleton) DO UPDATE SET position_id = excluded.position_id, "
        "track_id = excluded.track_id, started_at = excluded.started_at, "
        "source_kind = excluded.source_kind, source_ref = excluded.source_ref, "
        "updated_at = excluded.updated_at",
        (
            position_id,
            position["track_id"],
            stamp,
            position["source_kind"],
            position["source_ref"],
            stamp,
        ),
    )
    conn.execute(
        "UPDATE tracks SET play_count = COALESCE(play_count, 0) + 1 WHERE id = ?",
        (position["track_id"],),
    )

    override_id = parse_override_id(position["source_ref"])
    if override_id:
        row = conn.execute("SELECT scope FROM overrides WHERE id = ?", (override_id,)).fetchone()
        if row is not None and row["scope"] == "one_track":
            conn.execute("UPDATE overrides SET active = 0 WHERE id = ?", (override_id,))
    item_id = parse_item_id(position["source_ref"])
    if item_id and position["source_kind"] in ("rotation", "schedule", "override"):
        _set_played(conn, item_id, _played(conn, item_id) + 1)

    media.delete_pending_after_change(conn, settings)
    conn.commit()
    return {
        "ok": True,
        "duplicate": False,
        "track_id": position["track_id"],
        "source_kind": position["source_kind"],
        "source_ref": position["source_ref"],
    }


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def queue_snapshot(conn: sqlite3.Connection, settings: Settings, now: datetime) -> dict:
    state = conn.execute("SELECT * FROM playback_state WHERE singleton = 1").fetchone()
    current = None
    remaining_current = 0.0
    if state is not None and state["track_id"]:
        track = conn.execute("SELECT * FROM tracks WHERE id = ?", (state["track_id"],)).fetchone()
        if track is not None:
            started = parse_iso(state["started_at"])
            elapsed = max(0.0, (now - started).total_seconds()) if started else 0.0
            duration = float(track["duration"] or 0.0)
            remaining_current = max(0.0, duration - elapsed) if duration else 0.0
            current = {
                "id": state["position_id"],
                "position_id": state["position_id"],
                "track": track_payload(track, settings),
                "source": source_payload(conn, state["source_kind"], state["source_ref"]),
                "started_at": state["started_at"],
                "elapsed": round(elapsed, 1),
            }
    items = []
    offset = remaining_current
    for index, row in enumerate(future_positions(conn)):
        track = conn.execute("SELECT * FROM tracks WHERE id = ?", (row["track_id"],)).fetchone()
        if track is None:
            continue
        estimated = now.timestamp() + offset
        items.append(
            {
                "id": row["id"],
                "position": index,
                "track": track_payload(track, settings),
                "source": source_payload(conn, row["source_kind"], row["source_ref"]),
                "prepared": row["state"] == "prepared",
                "manual": bool(row["manual"]),
                "estimated_start": datetime.fromtimestamp(estimated, tz=now.tzinfo).isoformat()
                if now.tzinfo
                else None,
            }
        )
        offset += float(track["duration"] or 0.0)
    return {
        "current": current,
        "items": items,
        "depth": db.get_int_setting(conn, "queue_depth", 6),
    }


def playback_payload(conn: sqlite3.Connection, settings: Settings, now: datetime) -> dict | None:
    state = conn.execute("SELECT * FROM playback_state WHERE singleton = 1").fetchone()
    if state is None or not state["track_id"]:
        return None
    track = conn.execute("SELECT * FROM tracks WHERE id = ?", (state["track_id"],)).fetchone()
    if track is None:
        return None
    started = parse_iso(state["started_at"])
    elapsed = max(0.0, (now - started).total_seconds()) if started else 0.0
    duration = float(track["duration"] or 0.0)
    return {
        "track": track_payload(track, settings),
        "source": source_payload(conn, state["source_kind"], state["source_ref"]),
        "started_at": state["started_at"],
        "elapsed": round(min(elapsed, duration) if duration else elapsed, 1),
        "duration": duration,
        "position_id": state["position_id"],
    }


def next_track_preview(conn: sqlite3.Connection, settings: Settings) -> dict | None:
    future = future_positions(conn)
    if not future:
        return None
    row = future[0]
    track = conn.execute("SELECT * FROM tracks WHERE id = ?", (row["track_id"],)).fetchone()
    if track is None:
        return None
    return {
        "id": row["id"],
        "track": track_payload(track, settings),
        "source": source_payload(conn, row["source_kind"], row["source_ref"]),
    }


def rotation_preview(conn: sqlite3.Connection) -> dict:
    rotation = get_rotation(conn)
    items = _rotation_items(conn, str(rotation["id"]))
    cursor = _cursor_item(conn, str(rotation["id"]))
    current = next((item for item in items if str(item["id"]) == cursor), None)
    if current is None and items:
        current = items[0]
    return {
        "mode": rotation["mode"] or "sequential",
        "current": {
            "item_id": current["id"] if current else None,
            "playlist_id": current["playlist_id"] if current else None,
            "playlist_name": current["playlist_name"] if current else None,
        },
        "items": [
            {
                "id": item["id"],
                "playlist_id": item["playlist_id"],
                "playlist_name": item["playlist_name"],
                "position": item["position"],
                "track_count": item["track_count"],
                "enabled": bool(item["enabled"]),
                "played": _played(conn, str(item["id"])),
            }
            for item in conn.execute(
                "SELECT ri.*, p.name AS playlist_name FROM rotation_items ri "
                "LEFT JOIN playlists p ON p.id = ri.playlist_id WHERE ri.rotation_id = ? "
                "ORDER BY ri.position ASC, ri.id ASC",
                (rotation["id"],),
            ).fetchall()
        ],
    }

