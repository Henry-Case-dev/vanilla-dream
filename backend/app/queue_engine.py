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
from .util import ApiError, parse_iso, to_iso, utcnow

LOGGER = logging.getLogger("vdradio.queue")

ENGINE_PREFETCH = 2
ENGINE_SYNC_MIN_INTERVAL = 5.0
ROTATION_ID = "default"
LIVE_STATES = ("pending", "prepared")
STATE_MODES = ("playlist", "auto")

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
# Playback mode / state revision
# ---------------------------------------------------------------------------


def state_revision(conn: sqlite3.Connection) -> int:
    try:
        return int(db.get_setting(conn, "state_revision", "0") or 0)
    except (TypeError, ValueError):
        return 0


def bump_state_revision(conn: sqlite3.Connection) -> int:
    revision = state_revision(conn) + 1
    db.set_setting(conn, "state_revision", str(revision))
    return revision


def queue_revision(conn: sqlite3.Connection) -> int:
    try:
        return int(db.get_setting(conn, "queue_revision", "0") or 0)
    except (TypeError, ValueError):
        return 0


def bump_queue_revision(conn: sqlite3.Connection) -> int:
    revision = queue_revision(conn) + 1
    db.set_setting(conn, "queue_revision", str(revision))
    return revision


def history_revision(conn: sqlite3.Connection) -> int:
    try:
        return int(db.get_setting(conn, "history_revision", "0") or 0)
    except (TypeError, ValueError):
        return 0


def bump_history_revision(conn: sqlite3.Connection) -> int:
    revision = history_revision(conn) + 1
    db.set_setting(conn, "history_revision", str(revision))
    return revision


def playback_mode(conn: sqlite3.Connection) -> str:
    mode = db.get_setting(conn, "playback_mode", "playlist") or "playlist"
    return mode if mode in STATE_MODES else "playlist"


def selected_playlist_id(conn: sqlite3.Connection) -> str:
    value = db.get_setting(conn, "selected_playlist_id", SYSTEM_PLAYLIST_ID)
    return value or SYSTEM_PLAYLIST_ID


def effective_selected_playlist(conn: sqlite3.Connection) -> tuple[str, bool]:
    """Return (playlist id, fallback_used). A deleted selection falls back."""

    selected = selected_playlist_id(conn)
    if selected == SYSTEM_PLAYLIST_ID or get_playlist(conn, selected) is not None:
        return selected, False
    return SYSTEM_PLAYLIST_ID, True


def pending_source(conn: sqlite3.Connection) -> dict | None:
    value = db.get_json_setting(conn, "pending_source", None)
    return value if isinstance(value, dict) and value.get("playlist_id") else None


def pending_head_position_id(conn: sqlite3.Connection) -> str | None:
    pending = pending_source(conn)
    if pending is None:
        return None
    head_id = str(pending.get("head_position_id") or "")
    return head_id or None


def pending_head_state(conn: sqlite3.Connection) -> str | None:
    head_id = pending_head_position_id(conn)
    if not head_id:
        return None
    row = conn.execute(
        "SELECT state FROM queue_positions WHERE id = ?", (head_id,)
    ).fetchone()
    return str(row["state"]) if row is not None else None


def set_pending_source(
    conn: sqlite3.Connection,
    playlist_id: str,
    mode: str,
    now: datetime,
    head_position_id: str | None = None,
) -> int:
    payload = {
        "playlist_id": playlist_id,
        "mode": mode if mode in STATE_MODES else "playlist",
        "requested_at": to_iso(now),
    }
    if head_position_id:
        payload["head_position_id"] = head_position_id
    db.set_json_setting(conn, "pending_source", payload)
    return bump_state_revision(conn)


def clear_pending_source(conn: sqlite3.Connection) -> bool:
    had_pending = bool(db.get_setting(conn, "pending_source"))
    if not had_pending:
        return False
    db.set_setting(conn, "pending_source", "")
    bump_state_revision(conn)
    return True


def _pending_target_cycle_setting() -> str:
    return "_pending_target_cycle"


def _save_pending_target_cycle(conn: sqlite3.Connection, source: Source) -> None:
    db.set_json_setting(
        conn,
        _pending_target_cycle_setting(),
        {
            "key": source.key,
            "state": db.get_json_setting(conn, _cycle_setting_key(source.key), None),
        },
    )


def _restore_pending_target_cycle(conn: sqlite3.Connection) -> None:
    data = db.get_json_setting(conn, _pending_target_cycle_setting(), None)
    if not isinstance(data, dict) or not data.get("key"):
        db.delete_setting(conn, _pending_target_cycle_setting())
        return
    key = str(data["key"])
    state = data.get("state")
    if state is None:
        db.delete_setting(conn, _cycle_setting_key(key))
    else:
        db.set_json_setting(conn, _cycle_setting_key(key), state)
    db.delete_setting(conn, _pending_target_cycle_setting())


def _unpop_future_automatic(
    conn: sqlite3.Connection, *, restore: bool = True
) -> None:
    """Return scheduled-but-unplayed automatic tracks to their cycle tails.

    The old source's cycle had already consumed these tracks when the queue was
    filled. Keeping them unplayed preserves the pass if the pending switch is
    cancelled or the same playlist is selected again later.
    """

    grouped: dict[str, list[str]] = {}
    for row in future_positions(conn):
        if row["manual"]:
            continue
        key = key_of(row["source_ref"])
        if key:
            grouped.setdefault(key, []).append(str(row["track_id"]))
        conn.execute("DELETE FROM queue_positions WHERE id = ?", (row["id"],))
        bump_queue_revision(conn)
    for key, track_ids in grouped.items():
        if not restore:
            continue
        state = db.get_json_setting(conn, _cycle_setting_key(key), None)
        if not state or not state.get("started") or state.get("repeat_one"):
            continue
        remaining = [str(item) for item in state.get("remaining") or []]
        restored: list[str] = []
        for track_id in track_ids:
            if track_id not in remaining and track_id not in restored:
                restored.append(track_id)
        if restored:
            state["remaining"] = restored + remaining
            db.set_json_setting(conn, _cycle_setting_key(key), state)
    _renumber(conn)


def _program_source_for_playlist(
    conn: sqlite3.Connection, playlist_id: str
) -> Source | None:
    key = (
        SYSTEM_PLAYLIST_ID
        if playlist_id == SYSTEM_PLAYLIST_ID
        else f"playlist:{playlist_id}"
    )
    return _playlist_source(
        conn,
        playlist_id,
        "playlist",
        key,
        allow_empty=True,
        fallback_allowed=False,
    )


def prepare_pending_source(
    conn: sqlite3.Connection,
    settings: Settings,
    playlist_id: str,
    now: datetime,
    *,
    should_reset: bool,
) -> str:
    """Put the first track of the requested source at the head of the queue."""

    source = _program_source_for_playlist(conn, playlist_id)
    if source is None:
        raise ApiError(404, "playlist_not_found", "Плейлист не найден.")
    _save_pending_target_cycle(conn, source)
    if should_reset:
        reset_source_cycle(conn, playlist_id)
    _unpop_future_automatic(
        conn,
        restore=not (
            should_reset
            and playback_mode(conn) == "playlist"
            and selected_playlist_id(conn) == playlist_id
        ),
    )
    choice = next_track(conn, source, now, set())
    if not choice.track_id:
        if should_reset:
            raise ApiError(
                409,
                "playlist_exhausted",
                "В плейлисте нет доступных треков для переключения.",
            )
        reset_source_cycle(conn, playlist_id)
        choice = next_track(conn, source, now, set())
    if not choice.track_id:
        raise ApiError(
            409,
            "playlist_exhausted",
            "В плейлисте нет доступных треков для переключения.",
        )
    head_id = _insert_position(
        conn,
        choice.track_id,
        source.kind,
        source.ref(choice.cycle),
        manual=False,
        now=now,
        at_head=True,
    )
    _place_first_automatic_before_manual(conn)
    set_pending_source(
        conn,
        playlist_id,
        "playlist",
        now,
        head_position_id=head_id,
    )
    conn.commit()
    return head_id


def cancel_pending_source(conn: sqlite3.Connection) -> bool:
    """Cancel a prepared transition and restore the current source's queue."""

    if pending_source(conn) is None:
        return False
    if conn.execute(
        "DELETE FROM queue_positions WHERE state IN ('pending', 'prepared') "
        "AND manual = 0"
    ).rowcount:
        bump_queue_revision(conn)
    _renumber(conn)
    _restore_pending_target_cycle(conn)
    clear_pending_source(conn)
    conn.commit()
    return True


def commit_pending_source_state(
    conn: sqlite3.Connection,
    settings: Settings,
    now: datetime,
    *,
    fill: bool = False,
) -> bool:
    """Commit the prepared source without rebuilding the prepared queue."""

    pending = pending_source(conn)
    if pending is None:
        return False
    playlist_id = str(pending.get("playlist_id") or SYSTEM_PLAYLIST_ID)
    mode = str(pending.get("mode") or "playlist")
    if mode not in STATE_MODES:
        mode = "playlist"
    head_id = pending.get("head_position_id")
    db.set_setting(conn, "playback_mode", mode)
    db.set_setting(conn, "selected_playlist_id", playlist_id)
    db.set_setting(conn, "pending_source", "")
    db.delete_setting(conn, _pending_target_cycle_setting())
    conn.execute("UPDATE overrides SET active = 0 WHERE active = 1")
    bump_state_revision(conn)
    db.log_event(
        conn,
        "info",
        "station",
        "Источник переключён после текущего трека",
        {"playlist_id": playlist_id, "mode": mode, "head_position_id": head_id},
        at=to_iso(now),
    )
    conn.commit()
    if fill:
        ensure_queue(conn, settings, now)
        conn.commit()
    return True


def active_override(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM overrides WHERE active = 1 "
        "ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()


def override_payload(conn: sqlite3.Connection) -> dict | None:
    row = active_override(conn)
    if row is None:
        return None
    playlist = (
        conn.execute(
            "SELECT name FROM playlists WHERE id = ?", (row["playlist_id"],)
        ).fetchone()
        if row["playlist_id"]
        else None
    )
    return {
        "id": row["id"],
        "kind": row["kind"],
        "playlist_id": row["playlist_id"],
        "playlist_name": playlist["name"] if playlist else None,
        "scope": row["scope"],
        "expires_at": row["expires_at"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "note": row["note"],
    }


def _cycle_is_exhausted(conn: sqlite3.Connection, key: str) -> bool:
    state = db.get_json_setting(conn, _cycle_setting_key(key), None)
    if not (
        state
        and state.get("started")
        and not state.get("remaining")
        and state.get("cycle", 0)
    ):
        return False
    return not any(
        key_of(row["source_ref"]) == key for row in future_positions(conn)
    )


def reset_source_cycle(conn: sqlite3.Connection, playlist_id: str) -> None:
    key = SYSTEM_PLAYLIST_ID if playlist_id == SYSTEM_PLAYLIST_ID else f"playlist:{playlist_id}"
    db.delete_setting(conn, _cycle_setting_key(key))
    clear_source_exclusions(conn, key)


def _place_first_automatic_before_manual(conn: sqlite3.Connection) -> None:
    future = future_positions(conn)
    automatic = [row for row in future if not row["manual"]]
    if not automatic:
        return
    first = automatic[0]
    manual_before = [
        row for row in future if row["manual"] and int(row["seq"]) < int(first["seq"])
    ]
    if not manual_before:
        return
    head = min(int(row["seq"]) for row in future) - 1
    conn.execute("UPDATE queue_positions SET seq = ? WHERE id = ?", (head, first["id"]))
    _renumber(conn)


def rebuild_source_queue(
    conn: sqlite3.Connection,
    settings: Settings,
    now: datetime,
    *,
    reset_playlist_cycle: str | None = None,
) -> list[str]:
    """Drop automatic future positions and build the newly resolved source."""

    dropped = {
        key_of(row["source_ref"])
        for row in future_positions(conn)
        if not row["manual"]
    }
    dropped_positions = conn.execute(
        "DELETE FROM queue_positions WHERE state IN ('pending', 'prepared') AND manual = 0"
    ).rowcount
    if dropped_positions:
        bump_queue_revision(conn)
    for key in dropped:
        if key:
            db.delete_setting(conn, _cycle_setting_key(key))
    if reset_playlist_cycle:
        reset_source_cycle(conn, reset_playlist_cycle)
    _renumber(conn)
    conn.commit()
    created = ensure_queue(conn, settings, now)
    _place_first_automatic_before_manual(conn)
    conn.commit()
    return created


def apply_pending_source(conn: sqlite3.Connection, settings: Settings, now: datetime) -> bool:
    """Compatibility wrapper: commit the prepared source and fill the queue."""

    return commit_pending_source_state(conn, settings, now, fill=True)


def apply_pending_if_due(conn: sqlite3.Connection, settings: Settings, now: datetime) -> bool:
    """Commit a prepared switch only if its head already started.

    The prepared queue is never rebuilt here: the head position and the manual
    positions are already the new source. The track-start callback normally
    commits first; this is the durable fallback for a missed callback.
    """

    pending = pending_source(conn)
    if pending is None:
        return False
    head_id = str(pending.get("head_position_id") or "")
    if not head_id:
        return False
    playback = conn.execute(
        "SELECT position_id FROM playback_state WHERE singleton = 1"
    ).fetchone()
    head_state = pending_head_state(conn)
    started = bool(
        (playback is not None and playback["position_id"] == head_id)
        or head_state == "current"
    )
    if started:
        return commit_pending_source_state(conn, settings, now, fill=False)
    if head_state not in ("pending", "prepared"):
        # The prepared head was deleted or invalidated. Do not wait for a
        # callback that can no longer arrive; cancel and let the tick rebuild.
        cancel_pending_source(conn)
        return True
    return False


def select_playlist(
    conn: sqlite3.Connection,
    settings: Settings,
    playlist_id: str,
    mode: str,
    restart: bool,
    now: datetime,
) -> dict:
    """Prepare a permanent playlist selection; the caller starts/syncs/skips."""

    if mode not in ("now", "next"):
        raise ApiError(422, "invalid_mode", "Режим запуска должен быть now или next.")
    playlist = get_playlist(conn, playlist_id)
    if playlist is None:
        raise ApiError(404, "playlist_not_found", "Плейлист не найден.")
    if not playlist_track_ids(conn, playlist_id):
        raise ApiError(422, "playlist_empty", "Плейлист пуст: добавьте треки перед запуском.")

    current = conn.execute(
        "SELECT * FROM playback_state WHERE singleton = 1"
    ).fetchone()
    has_current = bool(current is not None and current["track_id"])
    desired_playing = db.get_setting(conn, "desired_state", "stopped") == "playing"
    diagnostic = source_diagnostic(conn, now)
    source = diagnostic.get("source")
    target_is_current = bool(
        playback_mode(conn) == "playlist"
        and selected_playlist_id(conn) == playlist_id
        and source is not None
        and source.playlist_id == playlist_id
        and diagnostic.get("reason") is None
    )
    effective_active = bool(desired_playing and target_is_current)
    pending = pending_source(conn)
    if pending is not None:
        # Any explicit selection supersedes the prepared transition. This also
        # restores the current source's unplayed queue when the owner cancels.
        cancel_pending_source(conn)
        if target_is_current and not restart:
            return {
                "status": "applied",
                "pending": False,
                "skip": False,
                "need_start": False,
            }
    elif effective_active and not restart:
        return {
            "status": "already_active",
            "pending": False,
            "skip": False,
            "need_start": False,
        }

    cycle_key = SYSTEM_PLAYLIST_ID if playlist_id == SYSTEM_PLAYLIST_ID else f"playlist:{playlist_id}"
    exhausted = _cycle_is_exhausted(conn, cycle_key)
    should_reset = bool(restart or exhausted)

    if mode == "next" and has_current:
        prepare_pending_source(
            conn,
            settings,
            playlist_id,
            now,
            should_reset=should_reset,
        )
        return {
            "status": "switching",
            "pending": True,
            "skip": False,
            "need_start": False,
        }

    db.set_setting(conn, "playback_mode", "playlist")
    db.set_setting(conn, "selected_playlist_id", playlist_id)
    db.set_setting(conn, "pending_source", "")
    conn.execute("UPDATE overrides SET active = 0 WHERE active = 1")
    bump_state_revision(conn)
    if should_reset:
        reset_source_cycle(conn, playlist_id)
    rebuild_source_queue(conn, settings, now)
    conn.commit()
    return {
        "status": "switching" if has_current else "applied",
        "pending": False,
        "skip": has_current,
        "need_start": False,
    }


def set_playback_mode(
    conn: sqlite3.Connection,
    mode: str,
    playlist_id: str | None = None,
) -> None:
    if mode not in STATE_MODES:
        raise ApiError(422, "invalid_mode", "Режим должен быть playlist или auto.")
    if mode == "playlist":
        selected = playlist_id or selected_playlist_id(conn) or SYSTEM_PLAYLIST_ID
        if selected != SYSTEM_PLAYLIST_ID and get_playlist(conn, selected) is None:
            if playlist_id:
                raise ApiError(404, "playlist_not_found", "Плейлист не найден.")
            selected = SYSTEM_PLAYLIST_ID
        db.set_setting(conn, "selected_playlist_id", selected)
    db.set_setting(conn, "playback_mode", mode)
    bump_state_revision(conn)


def _effective_source_payload(
    conn: sqlite3.Connection, now: datetime
) -> tuple[dict | None, str | None, str | None]:
    diagnostic = source_diagnostic(conn, now)
    source = diagnostic.get("source")
    if source is None:
        return None, diagnostic.get("reason"), None
    governed_by = (
        "rotation" if source.rotation_id or source.rotation_item_id else "playlist"
    )
    return (
        {
            "kind": source.kind,
            "playlist_id": source.playlist_id,
            "name": source.name,
            "reason": diagnostic.get("reason"),
        },
        None,
        governed_by,
    )


def station_state(
    conn: sqlite3.Connection,
    settings: Settings,
    now: datetime,
    *,
    status: dict | None = None,
    engine_instance: str = "",
    public: bool = False,
) -> dict:
    mode = playback_mode(conn)
    selected = selected_playlist_id(conn)
    effective, diagnostic_reason, repeat_governed = _effective_source_payload(conn, now)
    fallback_selected, fallback_used = effective_selected_playlist(conn)
    notice = None
    if mode == "playlist" and fallback_used:
        notice = "Выбранный плейлист не найден, включена «Вся медиатека»."
    if fallback_used:
        selected = fallback_selected
    payload = {
        "mode": mode,
        "selected_playlist_id": selected,
        "program_playlist_id": effective.get("playlist_id") if effective else None,
        "repeat_governed_by": repeat_governed,
        "effective_source": effective,
        "pending_source": pending_source(conn),
        "override": None if public else override_payload(conn),
        "desired": (status or {}).get("desired", "stopped"),
        "status": (status or {}).get("state"),
        "reason": (status or {}).get("reason"),
        "engine_instance": engine_instance,
        "state_revision": state_revision(conn),
        "notice": notice if not public else None,
        "diagnostic_reason": diagnostic_reason,
    }
    return payload


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
        configured = {
            selected_playlist_id(conn),
            db.get_setting(conn, "default_playlist_id"),
        }
        return (
            playlist_id in configured
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
        fallback_allowed=False,
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

    mode = playback_mode(conn)
    if mode == "playlist":
        selected, fallback_used = effective_selected_playlist(conn)
        if fallback_used:
            _warn_once(
                conn,
                f"selected-playlist:{selected_playlist_id(conn)}",
                "Выбранный плейлист не найден, включена «Вся медиатека»",
                {"playlist_id": selected_playlist_id(conn)},
            )
        if selected == SYSTEM_PLAYLIST_ID:
            source = _system_source(conn)
            return source if source.key not in excluded else None
        key = f"playlist:{selected}"
        if key in excluded:
            return None
        return _playlist_source(
            conn,
            selected,
            "playlist",
            key,
            allow_empty=True,
            fallback_allowed=False,
        )

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


def rebuild_active_future(
    conn: sqlite3.Connection, settings: Settings, now: datetime, source_key: str
) -> list[str]:
    """Rebuild only the active source's automatic future, keeping its cycle."""

    for row in future_positions(conn):
        if row["manual"]:
            continue
        if key_of(row["source_ref"]) == source_key:
            conn.execute("DELETE FROM queue_positions WHERE id = ?", (row["id"],))
    _renumber(conn)
    conn.commit()
    created = ensure_queue(conn, settings, now)
    conn.commit()
    return created


def _ordinary_unplayed(
    conn: sqlite3.Connection, key: str, state: dict, ids: list[str]
) -> list[str]:
    if state.get("repeat_one"):
        values = list(
            state.get("repeat_saved_remaining")
            or state.get("remaining")
            or []
        )
    else:
        values = [
            str(row["track_id"])
            for row in future_positions(conn)
            if not row["manual"] and key_of(row["source_ref"]) == key
        ] + list(state.get("remaining") or [])
    valid = set(ids)
    result: list[str] = []
    for track_id in values:
        if track_id in valid and track_id not in result:
            result.append(track_id)
    return result


def reconcile_playlist_settings(
    conn: sqlite3.Connection,
    playlist_id: str,
    *,
    shuffle: bool,
    repeat: str,
    shuffle_changed: bool,
    repeat_changed: bool,
    now: datetime,
    cycle_key: str | None = None,
) -> bool:
    """Reorder the remaining pass without resetting it; never starts a new pass."""

    key = cycle_key or (
        SYSTEM_PLAYLIST_ID
        if playlist_id == SYSTEM_PLAYLIST_ID
        else f"playlist:{playlist_id}"
    )
    state = db.get_json_setting(conn, _cycle_setting_key(key), None)
    if not state or not state.get("started"):
        return False
    ids = playlist_track_ids(conn, playlist_id)
    order = [item for item in state.get("order") or [] if item in set(ids)]
    for track_id in ids:
        if track_id not in order:
            order.append(track_id)
    ordinary = _ordinary_unplayed(conn, key, state, ids)

    if repeat_changed:
        if repeat == "none":
            current_cycle = int(state.get("cycle") or 0)
            for row in future_positions(conn):
                if row["manual"] or key_of(row["source_ref"]) != key:
                    continue
                parts = str(row["source_ref"]).rsplit(":cycle:", 1)
                try:
                    cycle = int(parts[1]) if len(parts) == 2 else current_cycle
                except (TypeError, ValueError):
                    cycle = current_cycle
                if cycle > current_cycle:
                    conn.execute("DELETE FROM queue_positions WHERE id = ?", (row["id"],))
            _renumber(conn)
            ordinary = _ordinary_unplayed(conn, key, state, ids)
    if shuffle_changed:
        if shuffle:
            random.SystemRandom().shuffle(ordinary)
        else:
            remaining_set = set(ordinary)
            ordinary = [track_id for track_id in order if track_id in remaining_set]
    if repeat_changed:
        if repeat == "one":
            state_row = conn.execute(
                "SELECT * FROM playback_state WHERE singleton = 1"
            ).fetchone()
            target = None
            if (
                state_row is not None
                and state_row["track_id"]
                and key_of(state_row["source_ref"]) == key
                and str(state_row["track_id"]) in ids
            ):
                target = str(state_row["track_id"])
            if target is None and ids:
                target = ids[0]
            state["repeat_one"] = True
            state["repeat_saved_remaining"] = ordinary
            state["repeat_target_id"] = target
        else:
            state.pop("repeat_one", None)
            state.pop("repeat_saved_remaining", None)
            state.pop("repeat_target_id", None)
    state["order"] = order
    state["remaining"] = ordinary
    if state.get("repeat_one"):
        state["repeat_saved_remaining"] = ordinary
    db.set_json_setting(conn, _cycle_setting_key(key), state)
    return True


def reorder_cycle_to_playlist(
    conn: sqlite3.Connection, playlist_id: str, cycle_key: str | None = None
) -> bool:
    key = cycle_key or (
        SYSTEM_PLAYLIST_ID
        if playlist_id == SYSTEM_PLAYLIST_ID
        else f"playlist:{playlist_id}"
    )
    state = db.get_json_setting(conn, _cycle_setting_key(key), None)
    if not state or not state.get("started"):
        return False
    ids = playlist_track_ids(conn, playlist_id)
    remaining_set = {str(item) for item in state.get("remaining") or []}
    state["order"] = list(ids)
    state["remaining"] = [track_id for track_id in ids if track_id in remaining_set]
    if state.get("repeat_one"):
        saved_set = {str(item) for item in state.get("repeat_saved_remaining") or []}
        state["repeat_saved_remaining"] = [
            track_id for track_id in ids if track_id in saved_set
        ]
    db.set_json_setting(conn, _cycle_setting_key(key), state)
    return True


def program_playlist_id(conn: sqlite3.Connection, now: datetime | None = None) -> str | None:
    diagnostic = source_diagnostic(conn, now or utcnow())
    source = diagnostic.get("source")
    return source.playlist_id if source is not None else None


def repeat_governed_by(
    conn: sqlite3.Connection, playlist_id: str, now: datetime | None = None
) -> str | None:
    diagnostic = source_diagnostic(conn, now or utcnow())
    source = diagnostic.get("source")
    if source is None or source.playlist_id != playlist_id:
        return None
    if source.rotation_id or source.rotation_item_id:
        return "rotation"
    return "playlist"


def finish_silence(conn: sqlite3.Connection, now: datetime) -> bool:
    """Close the current track when the engine has transitioned to silence."""

    state = conn.execute("SELECT * FROM playback_state WHERE singleton = 1").fetchone()
    if state is None or not state["track_id"]:
        return False
    stamp = to_iso(now)
    if state["position_id"]:
        ended = conn.execute(
            "UPDATE history SET ended_at = ? WHERE position_id = ? AND ended_at IS NULL",
            (stamp, state["position_id"]),
        ).rowcount
        if ended:
            bump_history_revision(conn)
        if conn.execute(
            "DELETE FROM queue_positions WHERE id = ?", (state["position_id"],)
        ).rowcount:
            bump_queue_revision(conn)
    conn.execute("DELETE FROM playback_state WHERE singleton = 1")
    bump_state_revision(conn)
    conn.commit()
    return True


def advance_repeat_target(conn: sqlite3.Connection, now: datetime) -> bool:
    diagnostic = source_diagnostic(conn, now)
    source = diagnostic.get("source")
    if source is None or source.repeat_mode != "one" or source.rotation_item_id:
        return False
    ids = playlist_track_ids(conn, source.playlist_id) if source.playlist_id else []
    if not ids:
        return False
    state = db.get_json_setting(conn, _cycle_setting_key(source.key), None) or {
        "cycle": 0,
        "order": list(ids),
        "remaining": list(ids),
        "started": True,
        "tail": None,
    }
    order = [item for item in state.get("order") or [] if item in set(ids)] or list(ids)
    saved = [
        item
        for item in (
            state.get("repeat_saved_remaining")
            or state.get("remaining")
            or order
        )
        if item in set(ids)
    ]
    old_target = state.get("repeat_target_id")
    target = old_target
    alternatives = [item for item in saved if item != target]
    if alternatives:
        target = alternatives[0]
    elif target in order:
        index = order.index(target)
        target = order[(index + 1) % len(order)]
    else:
        target = order[0]
    state["repeat_target_id"] = target
    if state.get("repeat_one"):
        state["repeat_saved_remaining"] = [
            item for item in saved if item != old_target
        ]
    db.set_json_setting(conn, _cycle_setting_key(source.key), state)
    return True


def _remove_cycle_tracks(
    conn: sqlite3.Connection, source: Source, removed_ids: set[str]
) -> None:
    if not removed_ids:
        return
    for row in future_positions(conn):
        if row["manual"]:
            continue
        if key_of(row["source_ref"]) == source.key and str(row["track_id"]) in removed_ids:
            conn.execute("DELETE FROM queue_positions WHERE id = ?", (row["id"],))
    state = db.get_json_setting(conn, _cycle_setting_key(source.key), None)
    if state:
        order = [item for item in state.get("order") or [] if item not in removed_ids]
        remaining = [item for item in state.get("remaining") or [] if item not in removed_ids]
        if state.get("repeat_target_id") in removed_ids:
            # The repeat object is gone: the next unplayed composition becomes
            # the target, otherwise the first remaining one. Never replay a
            # composition that was already part of this pass.
            fallback = remaining or order
            state["repeat_target_id"] = fallback[0] if fallback else None
        state["order"] = order
        state["remaining"] = remaining
        if state.get("repeat_one"):
            state["repeat_saved_remaining"] = [
                item for item in state.get("repeat_saved_remaining") or []
                if item not in removed_ids
            ]
        db.set_json_setting(conn, _cycle_setting_key(source.key), state)
    _renumber(conn)


def replenish_cycle(
    conn: sqlite3.Connection, source: Source, added_ids: list[str]
) -> None:
    """Add new playlist members to the unplayed part of the current cycle."""

    if not added_ids:
        return
    state = db.get_json_setting(conn, _cycle_setting_key(source.key), None)
    if not state or not state.get("started"):
        return
    order = list(state.get("order") or [])
    remaining = list(state.get("remaining") or [])
    saved = list(state.get("repeat_saved_remaining") or [])
    for track_id in added_ids:
        if track_id not in order:
            order.append(track_id)
        if track_id not in remaining:
            remaining.append(track_id)
        if state.get("repeat_one") and track_id not in saved:
            saved.append(track_id)
    if source.shuffle and remaining:
        tail = state.get("tail")
        random.SystemRandom().shuffle(remaining)
        if len(remaining) > 1 and tail and remaining[0] == tail:
            index = random.SystemRandom().randrange(1, len(remaining))
            remaining[0], remaining[index] = remaining[index], remaining[0]
    if source.shuffle and saved:
        random.SystemRandom().shuffle(saved)
    state["order"] = order
    state["remaining"] = remaining
    if state.get("repeat_one"):
        state["repeat_saved_remaining"] = saved
    if remaining:
        state["tail"] = remaining[-1]
    db.set_json_setting(conn, _cycle_setting_key(source.key), state)


def _apply_source_signature(conn: sqlite3.Connection, source: Source) -> None:
    signature = _source_signature(conn, source)
    stored = db.get_json_setting(conn, _source_setting_key(source.key), None)
    if stored is not None:
        old_ids = [str(item) for item in stored.get("ids") or []]
        new_ids = [str(item) for item in signature.get("ids") or []]
        old_set, new_set = set(old_ids), set(new_ids)
        removed = old_set - new_set
        added = [track_id for track_id in new_ids if track_id not in old_set]
        if removed:
            _remove_cycle_tracks(conn, source, removed)
        if added:
            replenish_cycle(conn, source, added)
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
        if not state or not state.get("started"):
            state = {
                "cycle": 0,
                "order": [],
                "remaining": [],
                "started": False,
                "tail": None,
            }
        cycle = int(state.get("cycle") or 0)
        order = [item for item in state.get("order") or [] if item in valid]
        for track_id in ids:
            if track_id not in order:
                order.append(track_id)
        remaining = [item for item in state.get("remaining") or [] if item in valid]
        finished = bool(state.get("started")) and not remaining

        if not state.get("started") or finished:
            if finished and repeat == "none":
                return Choice(None, cycle, "exhausted")
            if finished and not allow_duplicates and _cycle_positions(
                conn, key, cycle, ("pending", "prepared")
            ):
                return Choice(None, cycle, "blocked")
            pass_order = list(ids)
            pass_remaining = list(pass_order)
            previous_tail = state.get("tail")
            if shuffle and len(pass_remaining) > 1:
                random.SystemRandom().shuffle(pass_remaining)
                if previous_tail and pass_remaining[0] == previous_tail:
                    index = random.SystemRandom().randrange(1, len(pass_remaining))
                    pass_remaining[0], pass_remaining[index] = (
                        pass_remaining[index],
                        pass_remaining[0],
                    )
            cycle += 1
            clear_source_exclusions(conn, key)
            state = {
                "cycle": cycle,
                "order": pass_order,
                "remaining": pass_remaining,
                "started": True,
                "tail": previous_tail,
                "repeat_target_id": state.get("repeat_target_id"),
            }
            remaining = pass_remaining

        excluded_now = excluded_tracks_for_ref(conn, f"{key}:cycle:{cycle}")
        while remaining:
            candidate = remaining[0]
            if candidate not in valid:
                remaining.pop(0)
                continue
            if candidate in excluded_now:
                remaining.pop(0)
                continue
            if not allow_duplicates and candidate in exclude_ids:
                remaining.pop(0)
                continue
            remaining.pop(0)
            state["order"] = order
            state["remaining"] = remaining
            state["tail"] = candidate
            db.set_json_setting(conn, _cycle_setting_key(key), state)
            return Choice(candidate, cycle, "ok")

        state["order"] = order
        state["remaining"] = []
        state["started"] = True
        db.set_json_setting(conn, _cycle_setting_key(key), state)
        if repeat == "all" and attempt == 0:
            continue
        if not allow_duplicates and _cycle_positions(
            conn, key, cycle, ("pending", "prepared")
        ):
            return Choice(None, cycle, "blocked")
        return Choice(None, cycle, "blocked")
    return Choice(None, 0, "blocked")


def next_track(
    conn: sqlite3.Connection,
    source: Source,
    now: datetime,
    exclude_ids: set[str],
) -> Choice:
    if not source.playlist_id:
        return Choice(None, reason="exhausted")
    ids = playlist_track_ids(conn, source.playlist_id)
    if not ids:
        return Choice(None, reason="empty" if source.is_system else "exhausted")

    state = db.get_json_setting(conn, _cycle_setting_key(source.key), None) or {}

    if source.repeat_mode == "one" and not source.rotation_item_id:
        if not state.get("started"):
            state = {
                "cycle": int(state.get("cycle") or 1),
                "order": list(ids),
                "remaining": list(ids),
                "started": True,
                "tail": state.get("tail"),
                "repeat_one": True,
                "repeat_saved_remaining": list(ids),
            }
        target = state.get("repeat_target_id")
        if target not in ids:
            target = ids[0]
        state["repeat_one"] = True
        state.setdefault("repeat_saved_remaining", list(state.get("remaining") or ids))
        state["repeat_target_id"] = target
        db.set_json_setting(conn, _cycle_setting_key(source.key), state)
        return Choice(target, int(state.get("cycle") or 0), "ok")

    if source.rotation_item_id:
        return _cycle_next(
            conn,
            source.key,
            ids,
            shuffle=source.shuffle,
            repeat="all" if source.by_count else "none",
            exclude_ids=exclude_ids,
            allow_duplicates=source.by_count,
        )

    repeat = source.repeat_mode
    return _cycle_next(
        conn,
        source.key,
        ids,
        shuffle=source.shuffle,
        repeat=repeat,
        exclude_ids=exclude_ids,
        allow_duplicates=False,
    )


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


def _queue_excluded_key(source_ref: str) -> str:
    return f"_queue_excluded:{source_ref}"


def excluded_tracks_for_ref(conn: sqlite3.Connection, source_ref: str) -> set[str]:
    values = db.get_json_setting(conn, _queue_excluded_key(source_ref), [])
    return {str(value) for value in values} if isinstance(values, list) else set()


def exclude_track_for_ref(conn: sqlite3.Connection, source_ref: str, track_id: str) -> None:
    values = excluded_tracks_for_ref(conn, source_ref)
    values.add(str(track_id))
    db.set_json_setting(conn, _queue_excluded_key(source_ref), sorted(values))


def clear_source_exclusions(conn: sqlite3.Connection, key: str) -> None:
    conn.execute(
        "DELETE FROM settings WHERE key LIKE ?", (f"_queue_excluded:{key}:%",)
    )


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
    bump_queue_revision(conn)
    return position_id


@_locked
def ensure_queue(conn: sqlite3.Connection, settings: Settings, now: datetime) -> list[str]:
    """Fill future positions to ``queue_depth`` and keep the current source stable."""

    depth = max(1, db.get_int_setting(conn, "queue_depth", 6))
    pending = pending_source(conn)
    if pending is not None and pending.get("head_position_id"):
        head_state = pending_head_state(conn)
        if head_state in ("pending", "prepared", "current"):
            # The transition queue is already prepared: its first automatic
            # position belongs to the new source. Keep it (and the manual order)
            # until the head actually starts; never refill from the old source.
            return []
        # The prepared head disappeared (queue deletion, track/playlist
        # removal). Cancel the armed switch and rebuild the current source
        # instead of leaving the station with an empty queue.
        cancel_pending_source(conn)
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
        if conn.execute(
            "DELETE FROM queue_positions WHERE state IN ('pending', 'prepared') AND manual = 0"
        ).rowcount:
            bump_queue_revision(conn)
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
                bump_queue_revision(conn)
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
    bump_state_revision(conn)
    conn.commit()
    return position_id


@_locked
def move_position(conn: sqlite3.Connection, position_id: str, move_to: int) -> None:
    row = conn.execute("SELECT * FROM queue_positions WHERE id = ?", (position_id,)).fetchone()
    if row is None:
        raise ApiError(404, "position_not_found", "Позиция очереди не найдена.")
    if row["state"] == "current":
        raise ApiError(
            409,
            "position_started",
            "Этот трек уже начал играть и не может быть перемещён.",
        )
    if row["state"] not in ("pending", "prepared"):
        raise ApiError(409, "position_started", "Позиция уже недоступна для изменения.")
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
    bump_state_revision(conn)
    bump_queue_revision(conn)
    conn.commit()


@_locked
def move_position_next(conn: sqlite3.Connection, position_id: str) -> None:
    row = conn.execute("SELECT * FROM queue_positions WHERE id = ?", (position_id,)).fetchone()
    if row is None:
        raise ApiError(404, "position_not_found", "Позиция очереди не найдена.")
    if row["state"] == "current":
        raise ApiError(
            409,
            "position_started",
            "Этот трек уже начал играть и не может быть перемещён.",
        )
    if row["state"] not in ("pending", "prepared"):
        raise ApiError(409, "position_started", "Позиция уже недоступна для изменения.")
    move_position(conn, position_id, 0)


@_locked
def delete_position(conn: sqlite3.Connection, settings: Settings, position_id: str) -> None:
    row = conn.execute("SELECT * FROM queue_positions WHERE id = ?", (position_id,)).fetchone()
    if pending_head_position_id(conn) == position_id and (
        row is None or row["state"] in ("pending", "prepared")
    ):
        # Removing the prepared head cancels the armed switch and restores the
        # current source's queue; the caller rebuilds and syncs afterwards.
        cancel_pending_source(conn)
        return
    if row is None:
        raise ApiError(404, "position_not_found", "Позиция очереди не найдена.")
    if row["state"] == "current":
        raise ApiError(
            409,
            "position_started",
            "Этот трек уже начал играть и не может быть убран из текущего прохода.",
        )
    if row["state"] not in ("pending", "prepared"):
        raise ApiError(409, "position_started", "Позиция уже недоступна для удаления.")
    if not row["manual"] and key_of(row["source_ref"]):
        exclude_track_for_ref(conn, str(row["source_ref"]), str(row["track_id"]))
    conn.execute("DELETE FROM queue_positions WHERE id = ?", (position_id,))
    _renumber(conn)
    bump_state_revision(conn)
    bump_queue_revision(conn)
    conn.commit()
    # The runtime symlink is released only after a confirmed sync.


@_locked
def shuffle_pending(conn: sqlite3.Connection) -> int:
    future = future_positions(conn)
    manual_ids = [str(row["id"]) for row in future if row["manual"]]
    automatic_ids = [str(row["id"]) for row in future if not row["manual"]]
    random.SystemRandom().shuffle(automatic_ids)
    ordered = manual_ids + automatic_ids
    for index, identifier in enumerate(ordered):
        conn.execute("UPDATE queue_positions SET seq = ? WHERE id = ?", (index, identifier))
    bump_state_revision(conn)
    bump_queue_revision(conn)
    conn.commit()
    return len(automatic_ids)


@_locked
def clear_manual(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "DELETE FROM queue_positions WHERE state IN ('pending', 'prepared') AND manual = 1"
    )
    _renumber(conn)
    bump_state_revision(conn)
    if cursor.rowcount:
        bump_queue_revision(conn)
    conn.commit()
    return cursor.rowcount or 0


# ---------------------------------------------------------------------------
# Engine sync
# ---------------------------------------------------------------------------


def build_engine_lines(conn: sqlite3.Connection, settings: Settings) -> list[str]:
    """Prepare symlinks and return ``<position_id>\\t<absolute link path>`` lines."""

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
        lines.append(f"{row['id']}\t{link}")
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
            # Nothing changed since the last confirmed queue. Treat the current
            # state as applied so the UI does not wait forever for a command
            # that was never needed.
            current_revision = state_revision(conn)
            sync_state["applied_revision"] = current_revision
            db.set_setting(conn, "applied_revision", str(current_revision))
            conn.commit()
            return True
    lines = build_engine_lines(conn, settings)
    expected_ids = [line.split("\t", 1)[0] for line in lines]
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
    if not lines and playable_source:
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
    try:
        settings.ensure_directories()
        from .util import atomic_write_text

        atomic_write_text(settings.engine_queue_file, content)
    except OSError:
        db.log_event(conn, "error", "engine", "Не удалось записать файл очереди движка", None)
        if sync_state is not None:
            sync_state["failed"] = True
            sync_state.pop("signature", None)
            sync_state.pop("at", None)
        conn.commit()
        return False

    revision = state_revision(conn)
    if hasattr(engine, "sync_report"):
        try:
            report = engine.sync_report(expected_ids)
        except TypeError:
            report = engine.sync_report()
    else:
        engine_ok = bool(engine and engine.sync())
        report = {
            "ok": engine_ok,
            "reply": "\n".join(
                ["OK"] + [f"ok {identifier}" for identifier in expected_ids] + ["END"]
            )
            if engine_ok
            else "",
            "resolved": len(expected_ids) if engine_ok else None,
            "failed": 0,
        }

    statuses: dict[str, str] | None = None
    reply = str(report.get("reply") or "")
    reply_lines = [line.strip() for line in reply.splitlines() if line.strip()]
    header_ok = bool(report.get("ok")) and bool(reply_lines) and reply_lines[0].upper().startswith("OK")
    ended = "END" in reply_lines
    if header_ok and ended:
        parsed: dict[str, str] = {}
        malformed = False
        for line in reply_lines[1:]:
            if line == "END":
                break
            parts = line.split(maxsplit=1)
            if len(parts) != 2 or parts[0] not in ("ok", "fail"):
                malformed = True
                break
            parsed[parts[1]] = parts[0]
        if not malformed and all(identifier in parsed for identifier in expected_ids):
            statuses = parsed
    if state_revision(conn) != revision:
        # A newer state change superseded this sync; never apply stale confirmation.
        db.log_event(
            conn,
            "info",
            "engine",
            "Устаревшее подтверждение синхронизации проигнорировано",
            {"revision": revision, "current": state_revision(conn)},
        )
        conn.commit()
        return False
    ok_ids = {identifier for identifier, status in (statuses or {}).items() if status == "ok"}
    fail_ids = {identifier for identifier, status in (statuses or {}).items() if status == "fail"}
    ok = statuses is not None and (not expected_ids or bool(ok_ids))
    if ok:
        current = current_position(conn)
        keep_ids = expected_ids + ([str(current["id"])] if current is not None else [])
        _cleanup_stale_links(settings, keep_ids)
        if sync_state is not None:
            sync_state["signature"] = signature
            sync_state["at"] = now
            sync_state["failed"] = False
            sync_state["applied_revision"] = revision
        db.set_setting(conn, "applied_revision", str(revision))
        if expected_ids:
            placeholders = ",".join("?" for _ in expected_ids)
            conn.execute(
                f"UPDATE queue_positions SET state = 'pending' WHERE state = 'prepared' "
                f"AND id NOT IN ({placeholders})",
                expected_ids,
            )
        else:
            conn.execute("UPDATE queue_positions SET state = 'pending' WHERE state = 'prepared'")
        if ok_ids:
            placeholders = ",".join("?" for _ in ok_ids)
            conn.execute(
                f"UPDATE queue_positions SET state = 'prepared' WHERE id IN ({placeholders})",
                list(ok_ids),
            )
        if fail_ids:
            placeholders = ",".join("?" for _ in fail_ids)
            conn.execute(
                f"UPDATE queue_positions SET state = 'pending' WHERE id IN ({placeholders})",
                list(fail_ids),
            )
            db.log_event(
                conn,
                "warning",
                "engine",
                "Движок не подготовил часть очереди",
                {"failed": sorted(fail_ids), "positions": expected_ids},
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
        if fail_ids:
            db.log_event(
                conn,
                "warning",
                "engine",
                "Движок не подготовил очередь",
                {"failed": sorted(fail_ids), "positions": expected_ids},
            )
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
        return {"status": "unknown", "track_id": None}

    stamp = to_iso(started_at)
    previous = conn.execute("SELECT * FROM playback_state WHERE singleton = 1").fetchone()
    previous_started = parse_iso(previous["started_at"]) if previous is not None else None
    if previous is not None and previous["position_id"] == position_id:
        if previous_started is not None and started_at < previous_started:
            return {"status": "stale", "track_id": position["track_id"]}
        return {"status": "duplicate", "track_id": position["track_id"]}
    if previous_started is not None and started_at < previous_started:
        return {"status": "stale", "track_id": position["track_id"]}
    if position["state"] == "current":
        return {"status": "duplicate", "track_id": position["track_id"]}

    track = conn.execute("SELECT * FROM tracks WHERE id = ?", (position["track_id"],)).fetchone()
    if previous is not None and previous["position_id"]:
        if conn.execute(
            "UPDATE history SET ended_at = ? WHERE position_id = ? AND ended_at IS NULL",
            (stamp, previous["position_id"]),
        ).rowcount:
            bump_history_revision(conn)
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
    bump_history_revision(conn)
    snapshot_duration = (
        float(track["duration"])
        if track is not None and track["duration"] is not None
        else None
    )
    conn.execute(
        "INSERT INTO playback_state(singleton, position_id, track_id, started_at, source_kind, "
        "source_ref, updated_at, duration) VALUES(1, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(singleton) DO UPDATE SET position_id = excluded.position_id, "
        "track_id = excluded.track_id, started_at = excluded.started_at, "
        "source_kind = excluded.source_kind, source_ref = excluded.source_ref, "
        "updated_at = excluded.updated_at, duration = excluded.duration",
        (
            position_id,
            position["track_id"],
            stamp,
            position["source_kind"],
            position["source_ref"],
            stamp,
            snapshot_duration,
        ),
    )
    conn.execute(
        "UPDATE tracks SET play_count = COALESCE(play_count, 0) + 1 WHERE id = ?",
        (position["track_id"],),
    )
    if not position["manual"]:
        cycle_key = key_of(position["source_ref"])
        cycle_state = db.get_json_setting(conn, _cycle_setting_key(cycle_key), None)
        if cycle_state and cycle_state.get("started"):
            cycle_state["repeat_target_id"] = str(position["track_id"])
            if cycle_state.get("repeat_one"):
                target = str(position["track_id"])
                cycle_state["remaining"] = [
                    item for item in cycle_state.get("remaining") or [] if item != target
                ]
                cycle_state["repeat_saved_remaining"] = [
                    item
                    for item in cycle_state.get("repeat_saved_remaining") or []
                    if item != target
                ]
            db.set_json_setting(conn, _cycle_setting_key(cycle_key), cycle_state)

    override_id = parse_override_id(position["source_ref"])
    if override_id:
        row = conn.execute("SELECT scope FROM overrides WHERE id = ?", (override_id,)).fetchone()
        if row is not None and row["scope"] == "one_track":
            conn.execute("UPDATE overrides SET active = 0 WHERE id = ?", (override_id,))
    item_id = parse_item_id(position["source_ref"])
    if item_id and position["source_kind"] in ("rotation", "schedule", "override"):
        played = _played(conn, item_id) + 1
        _set_played(conn, item_id, played)
        rotation = get_rotation(conn)
        item = conn.execute(
            "SELECT track_count FROM rotation_items WHERE id = ?", (item_id,)
        ).fetchone()
        if (
            str(rotation["mode"] or "sequential") == "by_count"
            and item is not None
            and played >= max(1, int(item["track_count"] or 1))
        ):
            _advance_rotation(conn, rotation, item_id)
        elif not _current_source_has_future(conn, key_of(position["source_ref"])) and (
            _cycle_is_exhausted(conn, key_of(position["source_ref"]))
        ):
            _advance_rotation(conn, rotation, item_id)

    media.delete_pending_after_change(conn, settings)
    pending = pending_source(conn)
    if pending is not None and pending.get("head_position_id") == position_id:
        # The prepared head actually started: commit the source and fill the
        # queue from it without rebuilding away the prepared positions.
        commit_pending_source_state(conn, settings, now, fill=True)
    bump_queue_revision(conn)
    bump_state_revision(conn)
    conn.commit()
    return {
        "status": "ok",
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
            snapshot_duration = state["duration"]
            duration = (
                float(snapshot_duration)
                if snapshot_duration is not None
                else float(track["duration"] or 0.0)
            )
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
        "state_revision": state_revision(conn),
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
    snapshot_duration = state["duration"]
    duration = (
        float(snapshot_duration)
        if snapshot_duration is not None
        else float(track["duration"] or 0.0)
    )
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

