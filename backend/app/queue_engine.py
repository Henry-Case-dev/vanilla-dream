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
from .config import Settings
from .models import source_payload, track_payload
from .util import ApiError, parse_iso, to_iso

LOGGER = logging.getLogger("vdradio.queue")

ENGINE_PREFETCH = 2
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
    rows = conn.execute(
        "SELECT pt.track_id FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id "
        "WHERE pt.playlist_id = ? AND t.pending_delete = 0 ORDER BY pt.position ASC",
        (playlist_id,),
    ).fetchall()
    return [str(row["track_id"]) for row in rows]


def library_track_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT id FROM tracks WHERE pending_delete = 0 ORDER BY created_at ASC, id ASC"
    ).fetchall()
    return [str(row["id"]) for row in rows]


def get_playlist(conn: sqlite3.Connection, playlist_id: str | None) -> sqlite3.Row | None:
    if not playlist_id:
        return None
    return conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()


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
) -> Source | None:
    playlist = get_playlist(conn, playlist_id)
    if playlist is None:
        return None
    if not playlist_track_ids(conn, playlist_id):
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

    rule = scheduler.active_rule(conn, now)
    if rule is not None:
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
    if default_id:
        key = f"playlist:{default_id}"
        if key not in excluded:
            source = _playlist_source(conn, default_id, "playlist", key)
            if source is not None:
                return source

    if "library" not in excluded and library_track_ids(conn):
        return Source(kind="library", key="library", name="Вся медиатека", shuffle=True)
    return None


# ---------------------------------------------------------------------------
# Cycles (shuffle / repeat)
# ---------------------------------------------------------------------------


def _last_played_for_key(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT track_id FROM history WHERE source_ref LIKE ? ORDER BY started_at DESC, id DESC LIMIT 1",
        (key + ":cycle:%",),
    ).fetchone()
    return str(row["track_id"]) if row and row["track_id"] else None


def _cycle_setting_key(key: str) -> str:
    return f"_cycle:{key}"


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
            if finished and repeat == "none":
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
        if repeat == "all" and attempt == 0:
            continue
        return Choice(None, cycle, "blocked")
    return Choice(None, 0, "blocked")


def next_track(
    conn: sqlite3.Connection,
    source: Source,
    now: datetime,
    exclude_ids: set[str],
) -> Choice:
    if source.kind == "library":
        ids = library_track_ids(conn)
        if not ids:
            return Choice(None, reason="exhausted")
        return _cycle_next(
            conn,
            "library",
            ids,
            shuffle=True,
            repeat="none",
            exclude_ids=exclude_ids,
            allow_duplicates=False,
        )

    if not source.playlist_id:
        return Choice(None, reason="exhausted")
    ids = playlist_track_ids(conn, source.playlist_id)
    if not ids:
        return Choice(None, reason="exhausted")

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
        if choice.reason in ("ok", "blocked"):
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
    if source.kind == "override" and repeat == "none":
        # An owner override stays in force until its scope expires; a single
        # playlist pass must not silently fall back to the scheduled source.
        repeat = "all"
    return _cycle_next(
        conn,
        source.key,
        ids,
        shuffle=source.shuffle,
        repeat=repeat,
        exclude_ids=exclude_ids,
        allow_duplicates=False,
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
        conn.execute("DELETE FROM queue_positions WHERE state = 'pending' AND manual = 0")
        _renumber(conn)
        conn.commit()
        return []
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
            if row["state"] == "pending" and not row["manual"]
        }
        conn.execute("DELETE FROM queue_positions WHERE state = 'pending' AND manual = 0")
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
        if choice.reason == "exhausted" or not choice.track_id:
            excluded_sources.add(source.key)
            continue
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


def sync_engine(conn: sqlite3.Connection, settings: Settings, engine) -> bool:
    """Refresh runtime symlinks, write the engine queue file and call ``vd_sync``."""

    _drop_unplayable_positions(conn, settings)
    lines = build_engine_lines(conn, settings)
    content = "\n".join(lines)
    if content:
        content += "\n"
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
        conn.commit()
        return False
    ok = bool(engine and engine.sync())
    if ok:
        ids = [Path(line).stem for line in lines]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE queue_positions SET state = 'prepared' WHERE id IN ({placeholders})",
                ids,
            )
            conn.execute(
                f"UPDATE queue_positions SET state = 'pending' WHERE state = 'prepared' "
                f"AND id NOT IN ({placeholders})",
                ids,
            )
        else:
            conn.execute("UPDATE queue_positions SET state = 'pending' WHERE state = 'prepared'")
        if db.get_setting(conn, "_sync_warned") == "1":
            db.delete_setting(conn, "_sync_warned")
            db.log_event(conn, "info", "engine", "Синхронизация очереди с движком восстановлена", None)
    else:
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

