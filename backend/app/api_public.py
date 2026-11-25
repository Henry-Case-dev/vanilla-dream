"""Public API: now playing, history, SSE, player link files and covers."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from . import db, media, queue_engine, scheduler
from .auth import get_conn
from .models import STATE_LABELS, playlist_auto_cover, source_payload
from .util import ApiError, clamp, is_relative_to, parse_iso

router = APIRouter(tags=["public"])

SSE_INTERVAL = 5
SSE_HEARTBEAT = 15
SSE_TICK = 1.0
COVER_CACHE = "public, max-age=300"


def public_base(conn: sqlite3.Connection, settings) -> str:
    value = db.get_setting(conn, "public_base_url") or settings.public_base_url
    return value.rstrip("/")


def _public_track(track: sqlite3.Row, settings, base: str) -> dict:
    effective_cover = track["custom_cover_path"] or track["cover_path"]
    cover_url = (
        f"{base}/api/public/cover/{track['id']}" if effective_cover else None
    )
    return {
        "id": track["id"],
        "title": track["title"],
        "artist": track["artist"],
        "album": track["album"],
        "genre": track["genre"],
        "year": track["year"],
        "cover_url": cover_url,
        "duration": track["duration"],
    }


def _no_source() -> dict:
    return {
        "kind": "none",
        "label": "Источник не выбран",
        "playlist_id": None,
        "name": None,
    }


def build_now_playing(conn: sqlite3.Connection, app, now: datetime | None = None) -> dict:
    settings = app.state.settings
    clock = app.state.clock
    now = now or clock.now()
    base = public_base(conn, settings)
    status = getattr(app.state, "station_status", None)
    if status is None:
        status = app.state.station.status(conn, now)
    stats = getattr(app.state, "stats", None)
    if stats is None:
        stats = app.state.icecast.stats() if app.state.icecast is not None else {"available": False}

    playback = queue_engine.playback_payload(conn, settings, now)
    if status.get("desired") != "playing" or status.get("has_current") is False:
        playback = None
    track_payload: dict | None = None
    source = _no_source()
    if playback is not None:
        row = conn.execute("SELECT * FROM tracks WHERE id = ?", (playback["track"]["id"],)).fetchone()
        if row is not None:
            track_payload = _public_track(row, settings, base)
            track_payload.update(
                {
                    "started_at": playback["started_at"],
                    "elapsed": playback["elapsed"],
                    "progress": round(
                        clamp(
                            playback["elapsed"] / playback["duration"]
                            if playback["duration"]
                            else 0.0,
                            0.0,
                            1.0,
                        ),
                        4,
                    ),
                }
            )
            source = playback["source"]

    preview = queue_engine.next_track_preview(conn, settings)
    if status.get("desired") != "playing":
        preview = None
    next_track = None
    if preview is not None:
        next_track = {
            "id": preview["track"]["id"],
            "title": preview["track"]["title"],
            "artist": preview["track"]["artist"],
            "album": preview["track"]["album"],
            "duration": preview["track"]["duration"],
            "cover_url": preview["track"]["cover_url"],
            "source": preview["source"],
        }

    change_at, rule_name = scheduler.next_schedule_change(conn, now)
    stats_available = bool(stats.get("available"))
    bitrate = stats.get("bitrate") if stats_available else None
    state = status.get("state", "off")
    return {
        "station": {
            "name": db.get_setting(conn, "station_name"),
            "description": db.get_setting(conn, "station_description"),
            "status": state,
            "status_label": STATE_LABELS.get(state, state),
            "reason": status.get("reason"),
            "desired": status.get("desired", db.get_setting(conn, "desired_state")),
            "stream_url": f"{base}/radio.mp3",
            "m3u_url": f"{base}/radio.m3u",
            "pls_url": f"{base}/radio.pls",
            "base_url": base,
            "bitrate": bitrate,
        },
        "track": track_payload,
        "source": source,
        "listeners": {
            "available": stats_available,
            "count": int(stats.get("listeners") or 0) if stats_available else None,
        },
        "next_track": next_track,
        "next_schedule_change": (
            {"at": change_at.isoformat(), "rule_name": rule_name} if change_at else None
        ),
        "stats_available": stats_available,
        "server_time": now.isoformat(),
    }


@router.get("/api/public/now-playing")
def now_playing(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return build_now_playing(conn, request.app)


@router.get("/api/public/history")
def history(
    request: Request,
    limit: int = 20,
    conn: sqlite3.Connection = Depends(get_conn),
):
    limit = max(1, min(int(limit or 20), 100))
    settings = request.app.state.settings
    base = public_base(conn, settings)
    rows = conn.execute(
        "SELECT h.*, COALESCE(t.custom_cover_path, t.cover_path) AS cover_path "
        "FROM history h LEFT JOIN tracks t ON t.id = h.track_id "
        "ORDER BY h.started_at DESC, h.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    items = []
    for row in rows:
        items.append(
            {
                "id": row["id"],
                "track_id": row["track_id"],
                "title": row["title"],
                "artist": row["artist"],
                "album": row["album"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "source": source_payload(conn, row["source_kind"], row["source_ref"]),
                "cover_url": (
                    f"{base}/api/public/cover/{row['track_id']}"
                    if row["cover_path"] and row["track_id"]
                    else None
                ),
            }
        )
    return {"items": items}


async def _event_source(request: Request, *, shutdown_event=None, snapshot=None):
    """Yield SSE snapshots until shutdown or client disconnect.

    The shutdown event is set from the SIGTERM/SIGINT handler installed in the
    lifespan, so an open player tab stops the stream immediately instead of
    waiting for the uvicorn graceful-shutdown timeout.
    """

    app = request.app
    loop = asyncio.get_running_loop()
    shutdown = shutdown_event
    if shutdown is None:
        shutdown = getattr(app.state, "shutdown_event", None)
    if shutdown is None:
        shutdown = asyncio.Event()
    changed = getattr(app.state, "now_playing_changed", None) or asyncio.Event()
    take_snapshot = snapshot or (lambda: _snapshot(app))
    heartbeat_at = loop.time()
    last_sent = 0.0
    try:
        while True:
            if shutdown.is_set():
                return
            if await request.is_disconnected():
                return
            now = loop.time()
            if last_sent == 0.0 or now - last_sent >= SSE_INTERVAL:
                payload = await asyncio.to_thread(take_snapshot)
                yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
                last_sent = loop.time()
                heartbeat_at = max(heartbeat_at, last_sent)
            if changed.is_set():
                changed.clear()
            if loop.time() - heartbeat_at >= SSE_HEARTBEAT:
                heartbeat_at = loop.time()
                yield ": heartbeat\n\n"
            try:
                await asyncio.wait_for(changed.wait(), timeout=SSE_TICK)
            except (asyncio.TimeoutError, TimeoutError):
                pass
    finally:
        # No external resources to close here; shutdown cleanup must not raise.
        pass


def _snapshot(app) -> dict:
    conn = db.open_db(app.state.settings)
    try:
        return build_now_playing(conn, app)
    finally:
        conn.close()


@router.get("/api/public/events")
async def events(request: Request):
    return StreamingResponse(
        _event_source(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/public/cover/{track_id}")
def public_cover(
    track_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    row = conn.execute(
        "SELECT cover_path, custom_cover_path FROM tracks WHERE id = ?", (track_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "cover_not_found", "Обложка не найдена.")
    path = media.effective_cover_path(request.app.state.settings, row)
    if path is None:
        raise ApiError(404, "cover_not_found", "Обложка не найдена.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": COVER_CACHE})


@router.get("/api/public/playlist-cover/{playlist_id}")
def playlist_cover(
    playlist_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    settings = request.app.state.settings
    row = conn.execute(
        "SELECT cover_path FROM playlists WHERE id = ?", (playlist_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "playlist_not_found", "Плейлист не найден.")
    if row["cover_path"]:
        path = settings.playlist_cover_dir / str(row["cover_path"])
        if is_relative_to(path, settings.playlist_cover_dir) and path.exists():
            return FileResponse(
                path, media_type="image/jpeg", headers={"Cache-Control": COVER_CACHE}
            )
    auto = playlist_auto_cover(conn, playlist_id)
    if not auto:
        raise ApiError(404, "cover_not_found", "Обложка не найдена.")
    path = settings.cover_dir / str(auto)
    if not is_relative_to(path, settings.cover_dir) or not path.exists():
        raise ApiError(404, "cover_not_found", "Обложка не найдена.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": COVER_CACHE})


def _stream_links(conn: sqlite3.Connection, settings) -> tuple[str, str]:
    base = public_base(conn, settings)
    name = db.get_setting(conn, "station_name") or "Vanilla Dream Radio"
    return base, name


@router.get("/radio.m3u")
def m3u(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    base, name = _stream_links(conn, request.app.state.settings)
    body = f"#EXTM3U\n#EXTINF:-1,{name}\n{base}/radio.mp3\n"
    return PlainTextResponse(body, media_type="audio/x-mpegurl")


@router.get("/radio.pls")
def pls(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    base, name = _stream_links(conn, request.app.state.settings)
    body = (
        "[playlist]\n"
        "NumberOfEntries=1\n"
        f"File1={base}/radio.mp3\n"
        f"Title1={name}\n"
        "Length1=-1\n"
        "Version=2\n"
    )
    return PlainTextResponse(body, media_type="audio/x-scpls")

