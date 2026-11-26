"""Admin API: library, playlists, rotation, schedule, queue, station, settings."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from . import db, exports, media, playlists, queue_engine, scheduler, uploads
from .auth import get_conn, require_csrf, require_session
from .config import SUPPORTED_BITRATES, SYSTEM_PLAYLIST_ID, Settings
from .models import (
    REPEAT_MODES,
    ROTATION_MODES,
    TRANSITIONS,
    hhmm_to_minutes,
    minutes_to_hhmm,
    playlist_membership_counts,
    playlist_payload,
    public_cover_url,
    schedule_rule_payload,
    source_payload,
    track_payload,
)
from .util import ApiError, atomic_write_text, escape_liq_string, is_relative_to, parse_iso, to_iso

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_session)])


def _now(request: Request) -> datetime:
    return request.app.state.clock.now()


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _sync(request: Request, conn) -> bool:
    """Only talk to the engine for sync when the station is meant to be playing."""

    if request.app.state.station.desired(conn) != "playing":
        return False
    return queue_engine.sync_engine(
        conn,
        request.app.state.settings,
        request.app.state.engine,
        now=request.app.state.clock.now(),
        sync_state=getattr(request.app.state, "sync_state", None),
    )


def _queue_sync(request: Request, conn) -> bool:
    if request.app.state.station.desired(conn) != "playing":
        return False
    if not _sync(request, conn):
        raise ApiError(
            502,
            "engine_sync_failed",
            "Движок не подтвердил изменение очереди. Повторите попытку.",
        )
    return True


def _applied_revision(request: Request) -> int | None:
    value = getattr(request.app.state, "sync_state", {}).get("applied_revision")
    if value is None:
        conn = db.open_db(request.app.state.settings)
        try:
            raw = db.get_setting(conn, "applied_revision", "0")
            value = int(raw or 0)
        except (TypeError, ValueError, sqlite3.Error):
            value = None
        finally:
            conn.close()
    return int(value) if value is not None else None


def _library_membership_changed(request: Request, conn, now) -> str:
    """Return applied/pending/saved for a library membership change."""

    if request.app.state.station.desired(conn) != "playing":
        return "saved"
    if queue_engine.program_playlist_id(conn, now) != SYSTEM_PLAYLIST_ID:
        return "saved"
    queue_engine.ensure_queue(conn, request.app.state.settings, now)
    conn.commit()
    synced = _sync(request, conn)
    request.app.state.scheduler.wake()
    return "applied" if synced else "pending"


def _replace_application(request: Request, conn, changed: bool) -> dict:
    """Rebuild the queue and force one sync after replacing audio files.

    The replace can affect any source, so this is not limited to the library
    playlist. When the station is stopped nothing has to be confirmed: the
    on-disk queue is authoritative and the next start refreshes every link.
    """

    now = request.app.state.clock.now()
    if changed:
        queue_engine.bump_state_revision(conn)
        queue_engine.bump_queue_revision(conn)
        conn.commit()
        queue_engine.ensure_queue(conn, request.app.state.settings, now)
        conn.commit()
    if not changed or request.app.state.station.desired(conn) != "playing":
        synced = True
    else:
        synced = queue_engine.sync_engine(
            conn,
            request.app.state.settings,
            request.app.state.engine,
            now=now,
            force=True,
            sync_state=getattr(request.app.state, "sync_state", None),
        )
    request.app.state.scheduler.wake()
    if not changed or request.app.state.station.desired(conn) != "playing":
        application = "saved"
    else:
        application = "applied" if synced else "pending"
    return {
        "application": application,
        "engine_synced": bool(synced),
        "state_revision": queue_engine.state_revision(conn),
        "applied_revision": _applied_revision(request),
    }


ENGINE_SETTING_KEYS = ("station_name", "station_description", "station_genre", "bitrate")


def _write_engine_files(settings: Settings, values: dict, changed: dict) -> None:
    """Rewrite Liquidsoap include files that actually changed.

    The engine script includes these files and expects plain bindings
    (`encoder_bitrate = 128`, `station_name = "..."`); the `:=` operator is
    rejected by liquidsoap 2.2.4.
    """

    if "bitrate" in changed:
        atomic_write_text(
            settings.encoder_file, f"encoder_bitrate = {int(values['bitrate'])}\n"
        )
    station_keys = {"station_name", "station_description", "station_genre"}
    if station_keys & set(changed):
        text = (
            f'station_name = "{escape_liq_string(values.get("station_name", ""))}"\n'
            f'station_description = "{escape_liq_string(values.get("station_description", ""))}"\n'
            f'station_genre = "{escape_liq_string(values.get("station_genre", ""))}"\n'
        )
        atomic_write_text(settings.station_file, text)


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------


SORT_COLUMNS = {
    "title": "py_lower(COALESCE(title, ''))",
    "artist": "py_lower(COALESCE(artist, ''))",
    "album": "py_lower(COALESCE(album, ''))",
    "duration": "COALESCE(duration, 0)",
    "created": "created_at",
}


@router.get("/tracks")
def list_tracks(
    request: Request,
    q: str = "",
    sort: str = "created",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
    conn: sqlite3.Connection = Depends(get_conn),
):
    settings = _settings(request)
    column = SORT_COLUMNS.get(sort)
    if column is None:
        raise ApiError(422, "invalid_sort", "Недопустимая сортировка.")
    direction = "ASC" if str(order).lower() == "asc" else "DESC"
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    where = "WHERE pending_delete = 0"
    params: list = []
    text = (q or "").strip().lower()
    if text:
        escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        where += (
            " AND (py_lower(title) LIKE ? ESCAPE '\\' OR py_lower(COALESCE(artist, '')) LIKE ? ESCAPE '\\' "
            "OR py_lower(COALESCE(album, '')) LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern, pattern])
    total = conn.execute(f"SELECT COUNT(*) AS count FROM tracks {where}", params).fetchone()["count"]
    rows = conn.execute(
        f"SELECT * FROM tracks {where} ORDER BY {column} {direction}, rowid ASC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    counts = playlist_membership_counts(conn, [row["id"] for row in rows])
    return {
        "items": [
            track_payload(row, settings, playlists_count=counts.get(str(row["id"]), 0))
            for row in rows
        ],
        "total": int(total or 0),
        "library": db.library_totals(conn),
    }


@router.get("/tracks/ids")
def list_track_ids(
    request: Request,
    q: str = "",
    exclude_playlist_id: str = "",
    conn: sqlite3.Connection = Depends(get_conn),
):
    where = "WHERE pending_delete = 0"
    params: list = []
    text = (q or "").strip().lower()
    if text:
        escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        where += (
            " AND (py_lower(title) LIKE ? ESCAPE '\\' "
            "OR py_lower(COALESCE(artist, '')) LIKE ? ESCAPE '\\' "
            "OR py_lower(COALESCE(album, '')) LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern, pattern])
    if exclude_playlist_id:
        if exclude_playlist_id == "library":
            where += " AND 0"
        else:
            where += (
                " AND id NOT IN (SELECT track_id FROM playlist_tracks WHERE playlist_id = ?)"
            )
            params.append(exclude_playlist_id)
    rows = conn.execute(
        f"SELECT id FROM tracks {where} ORDER BY created_at ASC, id ASC", params
    ).fetchall()
    ids = [str(row["id"]) for row in rows]
    return {"ids": ids, "total": len(ids)}


@router.post("/tracks/upload")
def upload_track(
    request: Request,
    file: UploadFile = File(...),
    batch_id: str | None = Form(None),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    settings = _settings(request)
    result = uploads.receive_upload(
        conn, settings, file.file, file.filename, _now(request), batch_id=batch_id
    )
    if result["status"] == "added":
        now = _now(request)
        queue_engine.bump_state_revision(conn)
        conn.commit()
        application = _library_membership_changed(request, conn, now)
        synced = application == "applied"
        active_library = bool(
            request.app.state.station.desired(conn) == "playing"
            and queue_engine.program_playlist_id(conn, now) == SYSTEM_PLAYLIST_ID
        )
        payload = {
            "status": "added",
            "track": track_payload(result["track"], settings, include_source=True),
            "batch_id": result["batch_id"],
            "application": application,
            "in_current_cycle": active_library,
            "engine_synced": synced,
            "state_revision": queue_engine.state_revision(conn),
            "applied_revision": _applied_revision(request),
        }
        return JSONResponse(payload, status_code=201)
    return JSONResponse(
        {
            "status": "pending",
            "pending": result["pending"],
            "batch_id": result["batch_id"],
        },
        status_code=202,
    )


@router.get("/uploads/{batch_id}")
def get_upload_batch(
    batch_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    payload = uploads.batch_payload(conn, _settings(request), batch_id)
    if payload is None:
        raise ApiError(404, "batch_not_found", "Загрузка не найдена.")
    return {"batch": payload}


@router.get("/uploads/{pending_id}/cover")
def get_pending_cover(
    pending_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    row = conn.execute(
        "SELECT cover_temp FROM pending_uploads WHERE id = ?", (pending_id,)
    ).fetchone()
    if row is None or not row["cover_temp"]:
        raise ApiError(404, "cover_not_found", "Обложка не найдена.")
    path = Path(str(row["cover_temp"]))
    if not is_relative_to(path, _settings(request).pending_dir) or not path.exists():
        raise ApiError(404, "cover_not_found", "Обложка не найдена.")
    from PIL import Image

    try:
        with Image.open(path) as image:
            image_format = image.format
    except Exception:  # noqa: BLE001 - malformed pending cover
        raise ApiError(404, "cover_not_found", "Обложка не найдена.")
    media_type = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
    }.get(str(image_format or ""), "application/octet-stream")
    return FileResponse(
        path, media_type=media_type, headers={"Cache-Control": "private, max-age=0"}
    )


@router.post("/uploads/batch/{batch_id}/decide")
def decide_upload_batch(
    batch_id: str,
    request: Request,
    body: dict = Body(default={}),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    data = body or {}
    action = str(data.get("action") or "")
    result = uploads.batch_decide(
        conn,
        _settings(request),
        batch_id,
        action,
        str(data.get("scope") or "all"),
        str(data.get("query") or ""),
        _now(request),
    )
    if action == "save" and result.get("processed"):
        queue_engine.bump_state_revision(conn)
        conn.commit()
        application = _library_membership_changed(request, conn, _now(request))
        result["application"] = application
        result["engine_synced"] = application == "applied"
        result["in_current_cycle"] = bool(
            request.app.state.station.desired(conn) == "playing"
            and queue_engine.program_playlist_id(conn, _now(request)) == SYSTEM_PLAYLIST_ID
        )
        result["state_revision"] = queue_engine.state_revision(conn)
        result["applied_revision"] = _applied_revision(request)
    elif action == "replace":
        result.update(_replace_application(request, conn, bool(result.get("replaced"))))
    return result


@router.post("/uploads/batch/{batch_id}/cancel")
def cancel_upload_batch(
    batch_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    return uploads.batch_cancel(conn, _settings(request), batch_id, _now(request))


@router.post("/uploads/{pending_id}/decide")
def decide_upload(
    pending_id: str,
    request: Request,
    body: dict = Body(default={}),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    data = body or {}
    action = str(data.get("action") or "")
    track_id = data.get("track_id")
    result = uploads.decide(
        conn,
        _settings(request),
        pending_id,
        action,
        _now(request),
        track_id=str(track_id) if track_id else None,
    )
    if action == "save":
        queue_engine.bump_state_revision(conn)
        conn.commit()
        application = _library_membership_changed(request, conn, _now(request))
        result["application"] = application
        result["engine_synced"] = application == "applied"
        result["in_current_cycle"] = bool(
            request.app.state.station.desired(conn) == "playing"
            and queue_engine.program_playlist_id(conn, _now(request)) == SYSTEM_PLAYLIST_ID
        )
        result["state_revision"] = queue_engine.state_revision(conn)
        result["applied_revision"] = _applied_revision(request)
    elif action == "replace":
        result.update(
            _replace_application(request, conn, result.get("status") == "replaced")
        )
    return result


@router.patch("/tracks/{track_id}")
def patch_track(
    track_id: str,
    request: Request,
    body: dict = Body(...),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    row = media.update_metadata(conn, track_id, body or {}, _now(request))
    return {"track": track_payload(row, _settings(request), include_source=True)}


@router.put("/tracks/{track_id}")
def put_track_atomic(
    track_id: str,
    request: Request,
    title: str | None = Form(None),
    artist: str | None = Form(None),
    album: str | None = Form(None),
    cover_action: str = Form("keep"),
    cover: UploadFile | None = File(None),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    fields: dict = {}
    if title is not None:
        fields["title"] = title
    if artist is not None:
        fields["artist"] = artist
    if album is not None:
        fields["album"] = album
    cover_stream = cover.file if cover is not None else None
    row = media.update_track_atomic(
        conn,
        _settings(request),
        track_id,
        fields,
        cover_stream,
        cover_action,
        _now(request),
    )
    return {"track": track_payload(row, _settings(request), include_source=True)}


@router.delete("/tracks/{track_id}")
def delete_track(
    track_id: str,
    request: Request,
    body: dict = Body(default={}),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    settings = _settings(request)
    mode = str((body or {}).get("mode") or "after")
    if mode not in ("after", "now"):
        raise ApiError(422, "invalid_delete_mode", "Режим удаления должен быть after или now.")
    now = _now(request)
    result = media.delete_track(conn, settings, track_id, now, mode=mode)
    queue_engine.ensure_queue(conn, settings, now)
    synced = _sync(request, conn)
    playing = request.app.state.station.desired(conn) == "playing"
    skipped = False
    if result.get("current") and mode == "now" and playing:
        if not synced:
            raise ApiError(
                502,
                "engine_sync_failed",
                "Не удалось подтвердить очередь перед удалением. Трек исключён и будет удалён позже.",
            )
        request.app.state.station.skip(conn)
        skipped = True
    purged = False
    if not result.get("current") and (synced or not playing):
        purged = media.purge_confirmed(conn, settings, track_id)
        conn.commit()
    request.app.state.scheduler.wake()
    return {
        **result,
        "deleted": purged,
        "pending": not purged,
        "skipped": skipped,
    }


@router.post("/tracks/bulk-delete")
def bulk_delete(
    request: Request,
    body: dict = Body(...),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(ids, list) or not ids:
        raise ApiError(422, "invalid_ids", "Передайте список треков для удаления.")
    if len(ids) > 500:
        raise ApiError(422, "too_many_ids", "За один раз можно удалить не более 500 треков.")
    mode = str((body or {}).get("mode") or "after")
    if mode not in ("after", "now"):
        raise ApiError(422, "invalid_delete_mode", "Режим удаления должен быть after или now.")
    settings = _settings(request)
    now = _now(request)
    result = media.bulk_delete(conn, settings, [str(raw or "") for raw in ids], now)
    queue_engine.ensure_queue(conn, settings, now)
    synced = _sync(request, conn)
    playing = request.app.state.station.desired(conn) == "playing"
    skipped = False
    if result["current"] and mode == "now" and playing:
        if not synced:
            raise ApiError(
                502,
                "engine_sync_failed",
                "Не удалось подтвердить очередь перед удалением. Треки исключены и будут удалены позже.",
            )
        request.app.state.station.skip(conn)
        skipped = True
    purged: list[str] = []
    current_ids = set(result["current"])
    if synced or not playing:
        for track_id in result["marked"]:
            if track_id in current_ids:
                continue
            if media.purge_confirmed(conn, settings, track_id):
                purged.append(track_id)
    if purged or not playing:
        conn.commit()
    pending = [track_id for track_id in result["marked"] if track_id not in set(purged)]
    request.app.state.scheduler.wake()
    return {
        **result,
        "deleted": purged,
        "purged": purged,
        "pending": pending,
        "mode": mode,
        "skipped": skipped,
    }


AUDIO_TYPES = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
}


@router.get("/tracks/{track_id}/stream")
def stream_track(
    track_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if row is None:
        raise ApiError(404, "track_not_found", "Трек не найден.")
    path = media.resolve_track_path(_settings(request), row)
    if not path.exists():
        raise ApiError(404, "file_not_found", "Файл трека не найден.")
    return FileResponse(
        path,
        media_type=AUDIO_TYPES.get(str(row["ext"] or ""), "application/octet-stream"),
        headers={"Accept-Ranges": "bytes"},
    )


@router.get("/tracks/{track_id}/cover")
def track_cover(
    track_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    row = conn.execute(
        "SELECT * FROM tracks WHERE id = ?", (track_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "cover_not_found", "Обложка не найдена.")
    path = media.effective_cover_path(_settings(request), row)
    if path is None:
        raise ApiError(404, "cover_not_found", "Обложка не найдена.")
    return FileResponse(path, media_type="image/jpeg")


@router.post("/tracks/{track_id}/cover")
def upload_track_cover(
    track_id: str,
    request: Request,
    file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    settings = _settings(request)
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if row is None:
        raise ApiError(404, "track_not_found", "Трек не найден.")
    name = media.save_custom_cover(settings, track_id, file.file)
    old = row["custom_cover_path"]
    conn.execute(
        "UPDATE tracks SET custom_cover_path = ?, updated_at = ? WHERE id = ?",
        (name, to_iso(_now(request)), track_id),
    )
    conn.commit()
    if old and old != name:
        media.remove_custom_cover(settings, old)
    updated = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    return {"track": track_payload(updated, settings, include_source=True)}


@router.delete("/tracks/{track_id}/cover")
def delete_track_cover(
    track_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    settings = _settings(request)
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if row is None:
        raise ApiError(404, "track_not_found", "Трек не найден.")
    media.remove_custom_cover(settings, row["custom_cover_path"])
    conn.execute(
        "UPDATE tracks SET custom_cover_path = NULL, updated_at = ? WHERE id = ?",
        (to_iso(_now(request)), track_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    return {"track": track_payload(updated, settings, include_source=True)}


@router.get("/tracks/{track_id}/download")
def download_track(
    track_id: str,
    request: Request,
    mode: str = "tags",
    conn: sqlite3.Connection = Depends(get_conn),
):
    path, media_type, disposition, cleanup = exports.single_download(
        conn, _settings(request), track_id, mode, _now(request)
    )
    background = BackgroundTask(cleanup.unlink, missing_ok=True) if cleanup else None
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Content-Disposition": disposition},
        background=background,
    )


def _after_manual_change(
    request: Request,
    conn,
    settings,
    skip: bool,
    position_id: str | None = None,
) -> dict:
    now = _now(request)
    queue_engine.ensure_queue(conn, settings, now)
    synced = _sync(request, conn)
    if skip:
        if not synced:
            # Never skip into a track the engine did not confirm. Roll the
            # manual position back so the previous queue stays intact.
            if position_id:
                conn.execute("DELETE FROM queue_positions WHERE id = ?", (position_id,))
                conn.commit()
                queue_engine.ensure_queue(conn, settings, now)
                conn.commit()
                # Restore the previous queue file even though the engine is
                # currently refusing the command.
                _sync(request, conn)
            raise ApiError(
                502,
                "engine_sync_failed",
                "Движок не подтвердил очередь. Трек не запущен, попробуйте снова.",
            )
        request.app.state.station.skip(conn)
    request.app.state.scheduler.wake()
    payload = queue_engine.queue_snapshot(conn, settings, now)
    payload["engine_synced"] = synced
    payload["applied_revision"] = _applied_revision(request)
    return payload


def _manual_add(request: Request, conn, track_id: str, mode: str, skip: bool) -> dict:
    settings = _settings(request)
    position_id = queue_engine.enqueue_manual(conn, settings, track_id, mode, _now(request))
    should_skip = skip and request.app.state.station.desired(conn) == "playing"
    return _after_manual_change(
        request, conn, settings, skip=should_skip, position_id=position_id
    )


@router.post("/exports")
def create_export(
    request: Request,
    background: BackgroundTasks,
    body: dict = Body(default={}),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    settings = _settings(request)
    now = _now(request)
    status, payload = exports.create_export(conn, settings, body or {}, now)
    if status == 202:
        background.add_task(exports.process_job, settings, payload["job"]["id"], now)
    return JSONResponse(payload, status_code=status)


@router.get("/exports/{job_id}")
def get_export(
    job_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    return {"job": exports.get_job(conn, job_id)}


@router.get("/exports/{job_id}/archive")
def get_export_archive(
    job_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    settings = _settings(request)
    path, name = exports.archive_file(conn, settings, job_id)
    exports.mark_downloading(conn, job_id, _now(request))
    return FileResponse(
        path,
        media_type="application/zip",
        headers={"Content-Disposition": exports.content_disposition(name)},
        background=BackgroundTask(exports.finish_download, settings, job_id),
    )


@router.post("/exports/{job_id}/cancel")
def cancel_export(
    job_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    return exports.cancel_job(conn, _settings(request), job_id, _now(request))


@router.post("/exports/{job_id}/retry")
def retry_export(
    job_id: str,
    request: Request,
    background: BackgroundTasks,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    settings = _settings(request)
    now = _now(request)
    result = exports.retry_job(conn, settings, job_id, now)
    background.add_task(exports.process_job, settings, job_id, now)
    return result


@router.post("/tracks/{track_id}/play-now")
def track_play_now(
    track_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    return _manual_add(request, conn, track_id, "play-now", skip=True)


@router.post("/tracks/{track_id}/play-next")
def track_play_next(
    track_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    return _manual_add(request, conn, track_id, "play-next", skip=False)


@router.post("/tracks/{track_id}/queue")
def track_queue(
    track_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    return _manual_add(request, conn, track_id, "queue", skip=False)


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------


@router.get("/playlists")
def get_playlists(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return {"items": playlists.list_playlists(conn, _settings(request), _now(request))}


@router.post("/playlists", status_code=201)
def post_playlist(
    request: Request,
    body: dict = Body(...),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    payload = playlists.create_playlist(
        conn,
        _settings(request),
        name=body.get("name"),
        description=body.get("description"),
        shuffle=bool(body.get("shuffle")),
        repeat_mode=body.get("repeat_mode") or "none",
        now=_now(request),
    )
    return {"playlist": payload}

@router.get("/playlists/{playlist_id}")
def get_playlist(
    playlist_id: str,
    request: Request,
    tracks_limit: int = 500,
    tracks_offset: int = 0,
    conn: sqlite3.Connection = Depends(get_conn),
):
    return {
        "playlist": playlists.playlist_detail(
            conn,
            _settings(request),
            playlist_id,
            tracks_limit=tracks_limit,
            tracks_offset=tracks_offset,
            now=_now(request),
        )
    }


@router.patch("/playlists/{playlist_id}")
def patch_playlist(
    playlist_id: str,
    request: Request,
    body: dict = Body(...),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    settings = _settings(request)
    now = _now(request)
    provided_revision = (body or {}).get("state_revision") if isinstance(body, dict) else None
    if provided_revision is not None:
        try:
            provided_revision = int(provided_revision)
        except (TypeError, ValueError):
            raise ApiError(422, "invalid_revision", "Некорректная ревизия состояния.")
        if provided_revision < queue_engine.state_revision(conn):
            raise ApiError(
                409,
                "stale_state",
                "Настройки уже изменились в другой вкладке. Обновите данные.",
            )
    before = playlists.require_playlist(conn, playlist_id)
    payload = playlists.update_playlist(conn, settings, playlist_id, body or {}, now)
    shuffle_changed = isinstance(body, dict) and "shuffle" in body and bool(body.get("shuffle")) != bool(before["shuffle"])
    repeat_changed = isinstance(body, dict) and "repeat_mode" in body and str(body.get("repeat_mode") or "") != str(before["repeat_mode"] or "")
    if shuffle_changed or repeat_changed:
        queue_engine.bump_state_revision(conn)
        conn.commit()
    application = "saved"
    synced = False
    if (shuffle_changed or repeat_changed) and request.app.state.station.desired(conn) == "playing":
        diagnostic = queue_engine.source_diagnostic(conn, now)
        source = diagnostic.get("source")
        if source is not None and source.playlist_id == playlist_id:
            queue_engine.reconcile_playlist_settings(
                conn,
                playlist_id,
                shuffle=bool(payload["shuffle"]),
                repeat=str(payload["repeat_mode"]),
                shuffle_changed=shuffle_changed,
                repeat_changed=repeat_changed,
                now=now,
                cycle_key=source.key,
            )
            queue_engine.rebuild_active_future(conn, settings, now, source.key)
            conn.commit()
            synced = _sync(request, conn)
            application = "applied" if synced else "pending"
            request.app.state.scheduler.wake()
    elif shuffle_changed or repeat_changed:
        application = "saved"
    return {
        "playlist": playlist_payload(
            conn, playlists.require_playlist(conn, playlist_id), settings, _now(request)
        ),
        "application": application,
        "engine_synced": synced,
        "state_revision": queue_engine.state_revision(conn),
        "applied_revision": _applied_revision(request),
    }


@router.delete("/playlists/{playlist_id}")
def delete_playlist(
    playlist_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    settings = _settings(request)
    rotation_item_ids = [
        str(row["id"])
        for row in conn.execute(
            "SELECT id FROM rotation_items WHERE playlist_id = ?", (playlist_id,)
        ).fetchall()
    ]
    pending = queue_engine.pending_source(conn)
    if pending is not None and str(pending.get("playlist_id")) == playlist_id:
        queue_engine.cancel_pending_source(conn)
    result = playlists.delete_playlist(conn, settings, playlist_id, _now(request))
    queue_engine.bump_state_revision(conn)
    queue_engine.invalidate_playlist_sources(conn, playlist_id)
    queue_engine.invalidate_position_refs(
        conn, [f":item:{item_id}:" for item_id in rotation_item_ids]
    )
    queue_engine.ensure_queue(conn, settings, _now(request))
    _sync(request, conn)
    request.app.state.scheduler.wake()
    return result


@router.post("/playlists/{playlist_id}/tracks")
def add_playlist_tracks(
    playlist_id: str,
    request: Request,
    body: dict = Body(...),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    ids = body.get("track_ids") if isinstance(body, dict) else None
    settings = _settings(request)
    now = _now(request)
    result = playlists.add_tracks(
        conn, settings, playlist_id, ids or [], now
    )
    application = "saved"
    synced = False
    if result.get("added"):
        queue_engine.bump_state_revision(conn)
        conn.commit()
        diagnostic = queue_engine.source_diagnostic(conn, now)
        source = diagnostic.get("source")
        if source is not None and source.playlist_id == playlist_id:
            queue_engine.ensure_queue(conn, settings, now)
            conn.commit()
            if request.app.state.station.desired(conn) == "playing":
                synced = queue_engine.sync_engine(
                    conn,
                    settings,
                    request.app.state.engine,
                    now=now,
                    sync_state=getattr(request.app.state, "sync_state", None),
                )
                application = "applied" if synced else "pending"
                request.app.state.scheduler.wake()
            else:
                application = "saved"
    return {
        **result,
        "application": application,
        "in_current_cycle": bool(result.get("added")) and application in ("applied", "pending"),
        "engine_synced": synced,
        "state_revision": queue_engine.state_revision(conn),
        "applied_revision": _applied_revision(request),
    }


@router.delete("/playlists/{playlist_id}/tracks/{track_id}")
def remove_playlist_track(
    playlist_id: str,
    track_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    settings = _settings(request)
    now = _now(request)
    result = playlists.remove_track(
        conn, settings, playlist_id, track_id, now
    )
    queue_engine.bump_state_revision(conn)
    conn.commit()
    diagnostic = queue_engine.source_diagnostic(conn, now)
    source = diagnostic.get("source")
    if source is not None and source.playlist_id == playlist_id:
        queue_engine.ensure_queue(conn, settings, now)
        conn.commit()
        synced = _sync(request, conn)
        request.app.state.scheduler.wake()
        result["engine_synced"] = synced
        result["application"] = "applied" if synced or request.app.state.station.desired(conn) != "playing" else "pending"
    else:
        result["engine_synced"] = False
        result["application"] = "saved"
    result["state_revision"] = queue_engine.state_revision(conn)
    result["applied_revision"] = _applied_revision(request)
    return result


@router.put("/playlists/{playlist_id}/order")
def reorder_playlist(
    playlist_id: str,
    request: Request,
    body: dict = Body(...),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    settings = _settings(request)
    now = _now(request)
    result = playlists.reorder(
        conn,
        settings,
        playlist_id,
        body.get("track_ids") or [],
        now,
    )
    queue_engine.bump_state_revision(conn)
    conn.commit()
    diagnostic = queue_engine.source_diagnostic(conn, now)
    source = diagnostic.get("source")
    if source is not None and source.playlist_id == playlist_id:
        queue_engine.reorder_cycle_to_playlist(conn, playlist_id, source.key)
        queue_engine.rebuild_active_future(conn, settings, now, source.key)
        conn.commit()
        synced = _sync(request, conn)
        request.app.state.scheduler.wake()
        result["engine_synced"] = synced
        result["application"] = "applied" if synced or request.app.state.station.desired(conn) != "playing" else "pending"
    else:
        result["engine_synced"] = False
        result["application"] = "saved"
    result["state_revision"] = queue_engine.state_revision(conn)
    result["applied_revision"] = _applied_revision(request)
    return result


@router.post("/playlists/{playlist_id}/cover")
def upload_playlist_cover(
    playlist_id: str,
    request: Request,
    file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    settings = _settings(request)
    row = playlists.require_playlist(conn, playlist_id)
    old_cover = row["cover_path"]
    name = media.save_playlist_cover(settings, playlist_id, file.file)
    if old_cover and old_cover != name:
        playlists.remove_playlist_cover(settings, {"cover_path": old_cover})
    conn.execute(
        "UPDATE playlists SET cover_path = ?, updated_at = ? WHERE id = ?",
        (name, to_iso(_now(request)), playlist_id),
    )
    conn.commit()
    return {
        "playlist": playlist_payload(
            conn, playlists.require_playlist(conn, playlist_id), settings, _now(request)
        )
    }


@router.delete("/playlists/{playlist_id}/cover")
def delete_playlist_cover(
    playlist_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    settings = _settings(request)
    row = playlists.require_playlist(conn, playlist_id)
    playlists.remove_playlist_cover(settings, row)
    conn.execute(
        "UPDATE playlists SET cover_path = NULL, updated_at = ? WHERE id = ?",
        (to_iso(_now(request)), playlist_id),
    )
    conn.commit()
    return {
        "playlist": playlist_payload(
            conn, playlists.require_playlist(conn, playlist_id), settings, _now(request)
        )
    }


@router.post("/playlists/{playlist_id}/play")
def play_playlist(
    playlist_id: str,
    request: Request,
    body: dict = Body(default={}),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    settings = _settings(request)
    now = _now(request)
    data = body or {}
    mode = str(data.get("mode") or "now")
    restart = bool(data.get("restart"))
    result = queue_engine.select_playlist(
        conn, settings, playlist_id, mode, restart, now
    )
    started = False
    synced = False
    unit_active = bool(request.app.state.station.status(conn, now).get("unit_active"))
    start_needed = mode == "now" and (
        request.app.state.station.desired(conn) != "playing" or not unit_active
    )
    if start_needed:
        request.app.state.station.start(conn, now)
        started = True
        synced = True
    elif result["status"] != "already_active":
        if request.app.state.station.desired(conn) == "playing":
            if not result["pending"]:
                queue_engine.ensure_queue(conn, settings, now)
                conn.commit()
            synced = _sync(request, conn)
            if not synced:
                if result["pending"]:
                    queue_engine.cancel_pending_source(conn)
                    queue_engine.ensure_queue(conn, settings, now)
                    conn.commit()
                    # Restore the old queue file even though the engine refused
                    # the command; the next scheduler retry will confirm it.
                    _sync(request, conn)
                raise ApiError(
                    502,
                    "engine_sync_failed",
                    "Движок не подтвердил новую очередь. Переключение не выполнено.",
                )
    if result.get("skip") and not started and request.app.state.station.desired(conn) == "playing":
        request.app.state.station.skip(conn)
    status = request.app.state.station.status(conn, now)
    request.app.state.station_status = status
    request.app.state.scheduler.wake()
    response_status = result["status"]
    if started and response_status == "already_active":
        response_status = "switching" if status.get("has_current") else "applied"
    return {
        "status": response_status,
        "started": started,
        "engine_synced": synced,
        "station": status,
        "playlist": playlist_payload(
            conn, playlists.require_playlist(conn, playlist_id), settings, _now(request)
        ),
        "state_revision": queue_engine.state_revision(conn),
        "applied_revision": _applied_revision(request),
    }


@router.post("/playlists/{playlist_id}/restart")
def restart_playlist(
    playlist_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    return play_playlist(
        playlist_id,
        request,
        body={"mode": "now", "restart": True},
        conn=conn,
        session=session,
    )


@router.post("/playlists/{playlist_id}/rotation")
def add_playlist_to_rotation(
    playlist_id: str,
    request: Request,
    body: dict = Body(default={}),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    playlists.require_playlist(conn, playlist_id)
    rotation = queue_engine.get_rotation(conn)
    existing = conn.execute(
        "SELECT * FROM rotation_items WHERE rotation_id = ? AND playlist_id = ?",
        (rotation["id"], playlist_id),
    ).fetchone()
    if existing is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(position), -1) AS position FROM rotation_items WHERE rotation_id = ?",
            (rotation["id"],),
        ).fetchone()
        position = int(body.get("position", int(row["position"]) + 1))
        track_count = max(1, int(body.get("track_count") or 1))
        conn.execute(
            "INSERT INTO rotation_items(id, rotation_id, playlist_id, position, enabled, track_count) "
            "VALUES(?, ?, ?, ?, 1, ?)",
            (uuid.uuid4().hex, rotation["id"], playlist_id, position, track_count),
        )
    else:
        conn.execute(
            "UPDATE rotation_items SET enabled = 1 WHERE id = ?", (existing["id"],)
        )
    conn.commit()
    return {"rotation": _rotation_payload(conn)}


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def _rotation_payload(conn) -> dict:
    rotation = queue_engine.get_rotation(conn)
    preview = queue_engine.rotation_preview(conn)
    return {
        "id": rotation["id"],
        "name": rotation["name"],
        "mode": rotation["mode"],
        "items": preview["items"],
        "current": preview["current"],
        "modes": list(ROTATION_MODES),
    }


@router.get("/rotation")
def get_rotation(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    payload = _rotation_payload(conn)
    return {**payload, "rotation": payload}


@router.put("/rotation")
def put_rotation(
    request: Request,
    body: dict = Body(...),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    mode = str(body.get("mode") or "sequential")
    if mode not in ROTATION_MODES:
        raise ApiError(422, "invalid_mode", "Недопустимый режим ротации.")
    items = body.get("items")
    if not isinstance(items, list):
        raise ApiError(422, "invalid_items", "Передайте список элементов ротации.")
    rotation = queue_engine.get_rotation(conn)
    seen: set[str] = set()
    prepared = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ApiError(422, "invalid_items", "Некорректный элемент ротации.")
        playlist_id = str(item.get("playlist_id") or "")
        playlists.require_playlist(conn, playlist_id)
        if playlist_id in seen:
            raise ApiError(422, "duplicate_playlist", "Плейлист повторяется в ротации.")
        seen.add(playlist_id)
        try:
            track_count = max(1, int(item.get("track_count") or 1))
        except (TypeError, ValueError):
            raise ApiError(422, "invalid_track_count", "Количество треков должно быть числом.")
        if track_count > 100:
            raise ApiError(
                422, "invalid_track_count", "Количество треков не может превышать 100."
            )
        prepared.append(
            {
                "playlist_id": playlist_id,
                "position": int(item.get("position", index)),
                "enabled": 0 if item.get("enabled") is False else 1,
                "track_count": track_count,
            }
        )
    conn.execute("DELETE FROM rotation_items WHERE rotation_id = ?", (rotation["id"],))
    for item in prepared:
        conn.execute(
            "INSERT INTO rotation_items(id, rotation_id, playlist_id, position, enabled, track_count) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                rotation["id"],
                item["playlist_id"],
                item["position"],
                item["enabled"],
                item["track_count"],
            ),
        )
    conn.execute(
        "UPDATE rotations SET mode = ?, updated_at = ? WHERE id = ?",
        (mode, to_iso(_now(request)), rotation["id"]),
    )
    conn.execute("DELETE FROM rotation_state WHERE id LIKE 'cursor:%'")
    conn.commit()
    queue_engine.ensure_queue(conn, _settings(request), _now(request))
    _sync(request, conn)
    payload = _rotation_payload(conn)
    return {**payload, "rotation": payload}


@router.get("/rotation/next-preview")
def rotation_next_preview(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return queue_engine.rotation_preview(conn)


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def _rule_payload_from_body(conn, body: dict, existing: sqlite3.Row | None, now: datetime):
    merged = {}
    for key in (
        "name",
        "playlist_id",
        "use_rotation",
        "days",
        "start",
        "end",
        "enabled",
        "priority",
        "transition",
    ):
        if key in (body or {}):
            merged[key] = body[key]
    if existing is not None:
        merged.setdefault("name", existing["name"])
        merged.setdefault("playlist_id", existing["playlist_id"])
        merged.setdefault("use_rotation", bool(existing["rotation"]))
        merged.setdefault("days", scheduler.parse_days(existing["days"]))
        merged.setdefault("start", minutes_to_hhmm(int(existing["start_min"])))
        merged.setdefault("end", minutes_to_hhmm(int(existing["end_min"])))
        merged.setdefault("enabled", bool(existing["enabled"]))
        merged.setdefault("priority", int(existing["priority"]))
        merged.setdefault("transition", existing["transition"])
    name = str(merged.get("name") or "").strip()
    if not name:
        raise ApiError(422, "invalid_name", "Укажите название правила.", {"name": "Укажите название."})
    playlist_id = merged.get("playlist_id") or None
    use_rotation = merged.get("use_rotation")
    if playlist_id:
        playlists.require_playlist(conn, str(playlist_id))
        rotation_flag = 1 if use_rotation else 0
    else:
        if use_rotation is False:
            raise ApiError(
                422,
                "invalid_rule_source",
                "Правило должно ссылаться на существующий плейлист "
                "или использовать ротацию.",
                {"playlist_id": "Выберите плейлист."},
            )
        rotation_flag = 1
    days = merged.get("days")
    if not isinstance(days, list):
        raise ApiError(422, "invalid_days", "Передайте список дней недели.")
    parsed_days = []
    for value in days:
        try:
            day = int(value)
        except (TypeError, ValueError):
            raise ApiError(422, "invalid_days", "Дни недели должны быть числами 0–6.")
        if not (0 <= day <= 6):
            raise ApiError(422, "invalid_days", "Дни недели должны быть числами 0–6.")
        if day not in parsed_days:
            parsed_days.append(day)
    if not parsed_days:
        raise ApiError(422, "invalid_days", "Выберите хотя бы один день недели.")
    try:
        start_min = hhmm_to_minutes(str(merged.get("start") or ""))
        end_min = hhmm_to_minutes(str(merged.get("end") or ""))
    except (TypeError, ValueError):
        raise ApiError(422, "invalid_time", "Время указывается в формате ЧЧ:ММ.")
    if start_min == end_min:
        raise ApiError(422, "invalid_time", "Начало и конец правила не могут совпадать.")
    transition = str(
        merged.get("transition")
        or db.get_setting(conn, "default_transition", "after_track")
        or "after_track"
    )
    if transition not in TRANSITIONS:
        raise ApiError(422, "invalid_transition", "Недопустимый режим перехода.")
    try:
        priority = int(merged.get("priority") or 0)
    except (TypeError, ValueError):
        raise ApiError(422, "invalid_priority", "Приоритет должен быть числом.")
    return {
        "name": name,
        "playlist_id": str(playlist_id) if playlist_id else None,
        "rotation": rotation_flag,
        "days": scheduler.encode_days(parsed_days),
        "start_min": start_min,
        "end_min": end_min,
        "enabled": 0 if merged.get("enabled") is False else 1,
        "priority": priority,
        "transition": transition,
        "updated_at": to_iso(now),
    }


@router.get("/schedule")
def get_schedule(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    rows = conn.execute("SELECT * FROM schedule_rules ORDER BY priority DESC, start_min ASC").fetchall()
    now = _now(request)
    active = scheduler.active_rule(conn, now)
    change_at, rule_name = scheduler.next_schedule_change(conn, now)
    return {
        "rules": [schedule_rule_payload(conn, row) for row in rows],
        "conflicts": scheduler.conflicts(conn),
        "schedule_enabled": db.get_setting(conn, "schedule_enabled", "1") == "1",
        "timezone": db.get_setting(conn, "timezone"),
        "active_rule_id": active["id"] if active else None,
        "next_change": (
            {"at": change_at.isoformat(), "rule_name": rule_name} if change_at else None
        ),
    }


@router.post("/schedule", status_code=201)
def post_schedule(
    request: Request,
    body: dict = Body(...),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    now = _now(request)
    values = _rule_payload_from_body(conn, body, None, now)
    rule_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO schedule_rules(id, name, playlist_id, rotation, days, start_min, end_min, "
        "enabled, priority, transition, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rule_id,
            values["name"],
            values["playlist_id"],
            values["rotation"],
            values["days"],
            values["start_min"],
            values["end_min"],
            values["enabled"],
            values["priority"],
            values["transition"],
            to_iso(now),
            values["updated_at"],
        ),
    )
    db.set_setting(conn, "last_schedule_change", to_iso(now) or "")
    conn.commit()
    queue_engine.ensure_queue(conn, _settings(request), now)
    _sync(request, conn)
    row = conn.execute("SELECT * FROM schedule_rules WHERE id = ?", (rule_id,)).fetchone()
    return {"rule": schedule_rule_payload(conn, row)}


@router.patch("/schedule/{rule_id}")
def patch_schedule(
    rule_id: str,
    request: Request,
    body: dict = Body(...),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    existing = conn.execute("SELECT * FROM schedule_rules WHERE id = ?", (rule_id,)).fetchone()
    if existing is None:
        raise ApiError(404, "rule_not_found", "Правило расписания не найдено.")
    now = _now(request)
    values = _rule_payload_from_body(conn, body, existing, now)
    conn.execute(
        "UPDATE schedule_rules SET name = ?, playlist_id = ?, rotation = ?, days = ?, "
        "start_min = ?, end_min = ?, enabled = ?, priority = ?, transition = ?, updated_at = ? "
        "WHERE id = ?",
        (
            values["name"],
            values["playlist_id"],
            values["rotation"],
            values["days"],
            values["start_min"],
            values["end_min"],
            values["enabled"],
            values["priority"],
            values["transition"],
            values["updated_at"],
            rule_id,
        ),
    )
    db.set_setting(conn, "last_schedule_change", to_iso(now) or "")
    conn.commit()
    queue_engine.ensure_queue(conn, _settings(request), now)
    _sync(request, conn)
    row = conn.execute("SELECT * FROM schedule_rules WHERE id = ?", (rule_id,)).fetchone()
    return {"rule": schedule_rule_payload(conn, row)}


@router.delete("/schedule/{rule_id}")
def delete_schedule(
    rule_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    existing = conn.execute("SELECT id FROM schedule_rules WHERE id = ?", (rule_id,)).fetchone()
    if existing is None:
        raise ApiError(404, "rule_not_found", "Правило расписания не найдено.")
    conn.execute("DELETE FROM schedule_rules WHERE id = ?", (rule_id,))
    now = _now(request)
    db.set_setting(conn, "last_schedule_change", to_iso(now) or "")
    conn.commit()
    queue_engine.ensure_queue(conn, _settings(request), now)
    _sync(request, conn)
    return {"deleted": True}


@router.get("/schedule/conflicts")
def schedule_conflicts(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return {"conflicts": scheduler.conflicts(conn)}


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


@router.get("/queue")
def get_queue(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return queue_engine.queue_snapshot(conn, _settings(request), _now(request))


@router.patch("/queue/{position_id}")
def patch_queue(
    position_id: str,
    request: Request,
    body: dict = Body(...),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    try:
        move_to = int(body.get("move_to"))
    except (TypeError, ValueError):
        raise ApiError(422, "invalid_move", "Укажите новую позицию в очереди.")
    try:
        queue_engine.move_position(conn, position_id, move_to)
    except ApiError as exc:
        if exc.code == "position_started":
            exc.extra["queue"] = queue_engine.queue_snapshot(
                conn, _settings(request), _now(request)
            )
        raise
    synced = _queue_sync(request, conn)
    payload = queue_engine.queue_snapshot(conn, _settings(request), _now(request))
    payload["engine_synced"] = synced
    payload["applied_revision"] = _applied_revision(request)
    return payload


@router.post("/queue/{position_id}/move-next")
def move_queue_next(
    position_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    try:
        queue_engine.move_position_next(conn, position_id)
    except ApiError as exc:
        if exc.code == "position_started":
            exc.extra["queue"] = queue_engine.queue_snapshot(
                conn, _settings(request), _now(request)
            )
        raise
    synced = _queue_sync(request, conn)
    payload = queue_engine.queue_snapshot(conn, _settings(request), _now(request))
    payload["engine_synced"] = synced
    payload["applied_revision"] = _applied_revision(request)
    return payload


@router.delete("/queue/{position_id}")
def delete_queue(
    position_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    try:
        queue_engine.delete_position(conn, _settings(request), position_id)
    except ApiError as exc:
        if exc.code == "position_started":
            exc.extra["queue"] = queue_engine.queue_snapshot(
                conn, _settings(request), _now(request)
            )
        raise
    queue_engine.ensure_queue(conn, _settings(request), _now(request))
    synced = _queue_sync(request, conn)
    payload = queue_engine.queue_snapshot(conn, _settings(request), _now(request))
    payload["engine_synced"] = synced
    payload["applied_revision"] = _applied_revision(request)
    return payload


@router.post("/queue/shuffle")
def post_queue_shuffle(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    count = queue_engine.shuffle_pending(conn)
    synced = _queue_sync(request, conn)
    payload = queue_engine.queue_snapshot(conn, _settings(request), _now(request))
    payload["shuffled"] = count
    payload["engine_synced"] = synced
    payload["applied_revision"] = _applied_revision(request)
    return payload


@router.post("/queue/clear-manual")
def post_queue_clear_manual(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    removed = queue_engine.clear_manual(conn)
    queue_engine.ensure_queue(conn, _settings(request), _now(request))
    synced = _queue_sync(request, conn)
    payload = queue_engine.queue_snapshot(conn, _settings(request), _now(request))
    payload["removed"] = removed
    payload["engine_synced"] = synced
    payload["applied_revision"] = _applied_revision(request)
    return payload


# ---------------------------------------------------------------------------
# Station control and overrides
# ---------------------------------------------------------------------------


@router.get("/station/state")
def station_state(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    now = _now(request)
    status = request.app.state.station.status(conn, now)
    request.app.state.station_status = status
    state = queue_engine.station_state(
        conn,
        _settings(request),
        now,
        status=status,
        engine_instance=request.app.state.station.engine_instance(),
    )
    state["applied_revision"] = _applied_revision(request)
    return state


@router.put("/station/mode")
def station_mode(
    request: Request,
    body: dict = Body(...),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    settings = _settings(request)
    now = _now(request)
    mode = str((body or {}).get("mode") or "")
    playlist_id = (body or {}).get("playlist_id")
    if queue_engine.pending_source(conn) is not None:
        queue_engine.cancel_pending_source(conn)
    queue_engine.set_playback_mode(conn, mode, str(playlist_id) if playlist_id else None)
    queue_engine.rebuild_source_queue(conn, settings, now)
    conn.commit()
    synced = False
    if request.app.state.station.desired(conn) == "playing":
        synced = _sync(request, conn)
    request.app.state.scheduler.wake()
    status = request.app.state.station.status(conn, now)
    request.app.state.station_status = status
    state = queue_engine.station_state(
        conn,
        settings,
        now,
        status=status,
        engine_instance=request.app.state.station.engine_instance(),
    )
    return {
        **state,
        "station": status,
        "engine_synced": synced,
        "applied_revision": _applied_revision(request),
    }


@router.get("/station/status")
def station_status(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    status = request.app.state.station.status(conn, _now(request))
    request.app.state.station_status = status
    status["queue"] = queue_engine.queue_snapshot(conn, _settings(request), _now(request))
    status["applied_revision"] = _applied_revision(request)
    return status


@router.post("/station/start")
def station_start(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    status = request.app.state.station.start(conn, _now(request))
    request.app.state.station_status = status
    request.app.state.scheduler.wake()
    return status


@router.post("/station/stop")
def station_stop(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    status = request.app.state.station.stop(conn, _now(request))
    request.app.state.station_status = status
    request.app.state.scheduler.wake()
    return status


@router.post("/station/restart")
def station_restart(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    status = request.app.state.station.restart(conn, _now(request))
    request.app.state.station_status = status
    request.app.state.scheduler.wake()
    return status


@router.post("/station/skip")
def station_skip(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    settings = _settings(request)
    now = _now(request)
    moved_target = queue_engine.advance_repeat_target(conn, now)
    if moved_target:
        diagnostic = queue_engine.source_diagnostic(conn, now)
        source = diagnostic.get("source")
        if source is not None:
            queue_engine.rebuild_active_future(conn, settings, now, source.key)
            conn.commit()
            if request.app.state.station.desired(conn) == "playing" and not _sync(request, conn):
                raise ApiError(
                    502,
                    "engine_sync_failed",
                    "Движок не подтвердил новую цель повтора. Пропуск не выполнен.",
                )
    request.app.state.station.skip(conn)
    request.app.state.scheduler.wake()
    payload = queue_engine.queue_snapshot(conn, settings, now)
    payload["repeat_target_moved"] = moved_target
    return payload


@router.post("/station/play-now/{track_id}")
def station_play_now(
    track_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    return _manual_add(request, conn, track_id, "play-now", skip=True)


@router.post("/station/play-next/{track_id}")
def station_play_next(
    track_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    return _manual_add(request, conn, track_id, "play-next", skip=False)


def _override_payload(conn, override_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM overrides WHERE id = ?", (override_id,)).fetchone()
    if row is None:
        return None
    playlist = (
        conn.execute("SELECT name FROM playlists WHERE id = ?", (row["playlist_id"],)).fetchone()
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


def _parse_until(conn, value, now: datetime) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ApiError(422, "invalid_until", "Укажите время окончания переопределения.")
    if "T" in text or "-" in text:
        moment = parse_iso(text)
        if moment is None:
            raise ApiError(422, "invalid_until", "Некорректное время окончания.")
        if moment <= now:
            raise ApiError(422, "invalid_until", "Время окончания уже прошло.")
        return moment
    try:
        minutes = hhmm_to_minutes(text)
    except (TypeError, ValueError):
        raise ApiError(422, "invalid_until", "Время указывается в формате ЧЧ:ММ или ISO.")
    zone = scheduler.station_tz(conn)
    local = now.astimezone(zone)
    candidate = local.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)
    if candidate <= local:
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(now.tzinfo)


@router.post("/station/override")
def create_station_override(
    request: Request,
    body: dict = Body(...),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    settings = _settings(request)
    now = _now(request)
    kind = str(body.get("kind") or "playlist")
    if kind not in ("playlist", "rotation"):
        raise ApiError(422, "invalid_kind", "Недопустимый тип переопределения.")
    scope = str(body.get("scope") or "indefinite")
    if scope not in ("one_track", "until_time", "until_schedule_change", "indefinite"):
        raise ApiError(422, "invalid_scope", "Недопустимый срок переопределения.")
    playlist_id = body.get("playlist_id") or None
    if kind == "playlist":
        if not playlist_id:
            raise ApiError(422, "invalid_playlist", "Выберите плейлист для переопределения.")
        playlists.require_playlist(conn, str(playlist_id))
        if not queue_engine.playlist_track_ids(conn, str(playlist_id)):
            raise ApiError(
                422, "empty_playlist", "Плейлист пуст: добавьте треки перед переопределением."
            )
    else:
        playlist_id = None
    expires_at = None
    if scope == "until_time":
        expires_at = to_iso(_parse_until(conn, body.get("until"), now))
    elif scope == "until_schedule_change":
        change_at, _name = scheduler.next_schedule_change(conn, now)
        if change_at is None:
            raise ApiError(422, "no_schedule", "Нет предстоящей смены расписания.")
        expires_at = to_iso(change_at)

    conn.execute("UPDATE overrides SET active = 0 WHERE active = 1")
    override_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO overrides(id, kind, playlist_id, scope, expires_at, active, created_at, note) "
        "VALUES(?, ?, ?, ?, ?, 1, ?, ?)",
        (
            override_id,
            kind,
            str(playlist_id) if playlist_id else None,
            scope,
            expires_at,
            to_iso(now),
            str(body.get("note") or "").strip() or None,
        ),
    )
    queue_engine.bump_state_revision(conn)
    conn.commit()
    queue_engine.ensure_queue(conn, settings, now)
    _sync(request, conn)
    request.app.state.scheduler.wake()
    status = request.app.state.station.status(conn, now)
    return {
        "override": _override_payload(conn, override_id),
        "queue": queue_engine.queue_snapshot(conn, settings, now),
        "mode": queue_engine.playback_mode(conn),
        "state_revision": queue_engine.state_revision(conn),
        "applied_revision": _applied_revision(request),
        "station": status,
    }


@router.delete("/station/override")
def delete_station_override(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    conn.execute("UPDATE overrides SET active = 0 WHERE active = 1")
    queue_engine.bump_state_revision(conn)
    conn.commit()
    settings = _settings(request)
    now = _now(request)
    queue_engine.ensure_queue(conn, settings, now)
    _sync(request, conn)
    request.app.state.scheduler.wake()
    status = request.app.state.station.status(conn, now)
    return {
        "cleared": True,
        "queue": queue_engine.queue_snapshot(conn, settings, now),
        "mode": queue_engine.playback_mode(conn),
        "effective_source": queue_engine.station_state(
            conn, settings, now, status=status, public=True
        ).get("effective_source"),
        "state_revision": queue_engine.state_revision(conn),
        "applied_revision": _applied_revision(request),
        "station": status,
    }


# ---------------------------------------------------------------------------
# Settings, storage, services, history, links
# ---------------------------------------------------------------------------


WRITABLE_SETTINGS = {
    "station_name",
    "station_description",
    "station_genre",
    "public_base_url",
    "bitrate",
    "timezone",
    "schedule_enabled",
    "default_transition",
    "default_playlist_id",
    "desired_state",
    "queue_depth",
    "max_upload_mb",
    "max_library_gb",
}


READONLY_SETTINGS = {"admin_password_set", "engine_last_start", "engine_last_error", "last_schedule_change"}


def _validate_settings(conn, body: dict, now: datetime) -> dict:
    if not isinstance(body, dict):
        raise ApiError(422, "invalid_body", "Некорректный запрос.")
    unknown = [key for key in body if key not in WRITABLE_SETTINGS and key not in READONLY_SETTINGS]
    if unknown:
        raise ApiError(
            422,
            "unknown_setting",
            "Неизвестный параметр настроек.",
            {key: "Неизвестный параметр." for key in unknown},
        )
    readonly = [key for key in body if key in READONLY_SETTINGS]
    if readonly:
        raise ApiError(
            422,
            "readonly_setting",
            "Этот параметр изменяется только системой.",
            {key: "Только для чтения." for key in readonly},
        )
    values: dict[str, str] = {}
    if "station_name" in body:
        name = str(body["station_name"] or "").strip()
        if not name:
            raise ApiError(422, "invalid_station_name", "Укажите название станции.", {"station_name": "Обязательное поле."})
        if len(name) > 100:
            raise ApiError(422, "invalid_station_name", "Название станции слишком длинное.")
        values["station_name"] = name
    if "station_description" in body:
        description = str(body["station_description"] or "").strip()
        if len(description) > 300:
            raise ApiError(422, "invalid_station_description", "Описание слишком длинное.")
        values["station_description"] = description
    if "station_genre" in body:
        genre = str(body["station_genre"] or "").strip()
        if len(genre) > 60:
            raise ApiError(422, "invalid_station_genre", "Жанр слишком длинный.")
        values["station_genre"] = genre
    if "bitrate" in body:
        try:
            bitrate = int(body["bitrate"])
        except (TypeError, ValueError):
            raise ApiError(422, "invalid_bitrate", "Битрейт должен быть числом.")
        if bitrate not in SUPPORTED_BITRATES:
            raise ApiError(
                422,
                "invalid_bitrate",
                "Недопустимый битрейт. Доступны: "
                + ", ".join(str(value) for value in SUPPORTED_BITRATES)
                + " кбит/с.",
                {"bitrate": "Недопустимое значение."},
            )
        values["bitrate"] = str(bitrate)
    if "timezone" in body:
        timezone_name = str(body["timezone"] or "").strip()
        if not scheduler.valid_timezone(timezone_name):
            raise ApiError(422, "invalid_timezone", "Неизвестный часовой пояс.", {"timezone": "Выберите пояс из списка IANA."})
        values["timezone"] = timezone_name
    if "schedule_enabled" in body:
        values["schedule_enabled"] = "1" if body["schedule_enabled"] else "0"
    if "default_transition" in body:
        transition = str(body["default_transition"] or "after_track")
        if transition not in TRANSITIONS:
            raise ApiError(422, "invalid_transition", "Недопустимый режим перехода.")
        values["default_transition"] = transition
    if "default_playlist_id" in body:
        playlist_id = str(body["default_playlist_id"] or "")
        if playlist_id:
            playlists.require_playlist(conn, playlist_id)
        values["default_playlist_id"] = playlist_id
    if "queue_depth" in body:
        try:
            depth = int(body["queue_depth"])
        except (TypeError, ValueError):
            raise ApiError(422, "invalid_queue_depth", "Глубина очереди должна быть числом.")
        if not (1 <= depth <= 20):
            raise ApiError(422, "invalid_queue_depth", "Глубина очереди должна быть от 1 до 20.")
        values["queue_depth"] = str(depth)
    if "max_upload_mb" in body:
        try:
            size = int(body["max_upload_mb"])
        except (TypeError, ValueError):
            raise ApiError(422, "invalid_max_upload", "Лимит загрузки должен быть числом.")
        if not (1 <= size <= 2000):
            raise ApiError(422, "invalid_max_upload", "Лимит загрузки должен быть от 1 до 2000 МБ.")
        values["max_upload_mb"] = str(size)
    if "max_library_gb" in body:
        try:
            limit = int(body["max_library_gb"])
        except (TypeError, ValueError):
            raise ApiError(422, "invalid_max_library", "Лимит медиатеки должен быть числом.")
        if not (0 <= limit <= 10000):
            raise ApiError(422, "invalid_max_library", "Лимит медиатеки должен быть от 0 до 10000 ГБ.")
        values["max_library_gb"] = str(limit)
    if "public_base_url" in body:
        url = str(body["public_base_url"] or "").strip().rstrip("/")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ApiError(422, "invalid_public_url", "Укажите полный адрес вида https://example.com.", {"public_base_url": "Некорректный адрес."})
        values["public_base_url"] = url
    if "desired_state" in body:
        desired = str(body["desired_state"] or "")
        if desired not in ("playing", "stopped"):
            raise ApiError(422, "invalid_desired_state", "Недопустимое желаемое состояние.")
        values["desired_state"] = desired
    return values


@router.get("/settings")
def get_settings(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    settings_map = db.all_settings(conn)
    return {
        **settings_map,
        "settings": settings_map,
        "supported": {
            "bitrates": list(SUPPORTED_BITRATES),
            "transitions": list(TRANSITIONS),
            "repeat_modes": list(REPEAT_MODES),
            "rotation_modes": list(ROTATION_MODES),
        },
    }


@router.put("/settings")
def put_settings(
    request: Request,
    body: dict = Body(...),
    conn: sqlite3.Connection = Depends(get_conn),
    session: dict = Depends(require_csrf),
):
    settings = _settings(request)
    now = _now(request)
    if isinstance(body, dict) and isinstance(body.get("settings"), dict):
        body = body["settings"]
    values = _validate_settings(conn, body, now)
    previous = db.all_settings(conn)
    engine_values = {
        key: values.get(key, previous.get(key, "")) for key in ENGINE_SETTING_KEYS
    }
    changed_engine_fields = {
        key: values[key]
        for key in ENGINE_SETTING_KEYS
        if key in values and previous.get(key, "") != values[key]
    }
    engine_restarted = False
    if changed_engine_fields:
        try:
            _write_engine_files(settings, engine_values, changed_engine_fields)
        except OSError:
            raise ApiError(
                500,
                "engine_config_write_failed",
                "Не удалось записать конфигурацию движка. Изменения не сохранены.",
            )
    desired_change = values.pop("desired_state", None)
    for key, value in values.items():
        db.set_setting(conn, key, value)
    if "public_base_url" in values:
        import dataclasses

        request.app.state.settings = dataclasses.replace(
            settings, public_base_url=values["public_base_url"]
        )
        request.app.state.station.settings = request.app.state.settings
        request.app.state.scheduler.settings = request.app.state.settings
    conn.commit()
    if changed_engine_fields:
        engine_restarted = request.app.state.station.restart_for_settings(conn, now)
    if desired_change is not None:
        if desired_change == "playing":
            request.app.state.station.start(conn, now)
        else:
            request.app.state.station.stop(conn, now)
    queue_engine.ensure_queue(conn, request.app.state.settings, now)
    _sync(request, conn)
    request.app.state.scheduler.wake()
    status = request.app.state.station.status(conn, now)
    request.app.state.station_status = status
    settings_map = db.all_settings(conn)
    return {
        **settings_map,
        "settings": settings_map,
        "station": status,
        "engine_restarted": engine_restarted,
        "engine_fields_changed": sorted(changed_engine_fields),
    }


@router.get("/storage")
def storage(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    settings = _settings(request)
    totals = db.library_totals(conn)
    cover_bytes = 0
    for directory in (
        settings.cover_dir,
        settings.playlist_cover_dir,
        settings.cover_dir / "custom",
    ):
        try:
            for entry in os.scandir(directory):
                if entry.is_file():
                    cover_bytes += entry.stat().st_size
        except OSError:
            continue
    free_bytes = total_bytes = 0
    try:
        statvfs = os.statvfs(str(settings.music_dir))
        free_bytes = statvfs.f_bavail * statvfs.f_frsize
        total_bytes = statvfs.f_blocks * statvfs.f_frsize
    except (AttributeError, OSError):
        usage = shutil.disk_usage(settings.music_dir)
        free_bytes, total_bytes = usage.free, usage.total
    try:
        db_bytes = settings.db_path.stat().st_size
    except OSError:
        db_bytes = 0
    return {
        "tracks": totals["count"],
        "bytes": totals["bytes"],
        "cover_bytes": cover_bytes,
        "db_bytes": db_bytes,
        "free_bytes": free_bytes,
        "total_bytes": total_bytes,
        "limit_gb": db.get_float_setting(conn, "max_library_gb", 0.0),
    }


@router.get("/services")
def services(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return {"services": request.app.state.station.services(conn)}


@router.get("/history")
def admin_history(
    request: Request,
    limit: int = 50,
    conn: sqlite3.Connection = Depends(get_conn),
):
    limit = max(1, min(int(limit or 50), 200))
    rows = conn.execute(
        "SELECT h.*, COALESCE(t.custom_cover_path, t.cover_path) AS cover_path, "
        "t.cover_path AS track_cover_path, t.custom_cover_path AS track_custom_cover_path, "
        "t.updated_at AS track_updated_at, t.replaced_at AS track_replaced_at "
        "FROM history h LEFT JOIN tracks t ON t.id = h.track_id "
        "ORDER BY h.started_at DESC, h.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    settings = _settings(request)
    base = db.get_setting(conn, "public_base_url") or settings.public_base_url
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
                    public_cover_url(
                        settings,
                        row["track_id"],
                        {
                            "cover_path": row["track_cover_path"],
                            "custom_cover_path": row["track_custom_cover_path"],
                            "updated_at": row["track_updated_at"],
                            "replaced_at": row["track_replaced_at"],
                        },
                        base=base,
                    )
                    if row["track_id"] and row["cover_path"]
                    else None
                ),
            }
        )
    return {"items": items}


@router.get("/links")
def links(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    settings = _settings(request)
    base = (db.get_setting(conn, "public_base_url") or settings.public_base_url).rstrip("/")
    stream = f"{base}/radio.mp3"
    items = [
        {
            "key": "mp3",
            "label": "Прямая ссылка на поток",
            "url": stream,
            "description": "Прямая ссылка на аудиопоток для интернет-радиоплееров и совместимых приложений",
            "primary": True,
        }
    ]
    if settings.public_http_base_url:
        items.append(
            {
                "key": "http_mp3",
                "label": "HTTP-поток",
                "url": f"{settings.public_http_base_url}/radio.mp3",
                "description": "Дополнительная HTTP-ссылка на тот же аудиопоток",
            }
        )
    items.extend(
        [
            {
                "key": "player",
                "label": "Публичный плеер",
                "url": f"{base}/",
                "description": "Страница прослушивания в браузере",
            },
            {
                "key": "m3u",
                "label": "Плейлист M3U",
                "url": f"{base}/radio.m3u",
                "description": "Файл плейлиста для VLC и других плееров",
            },
            {
                "key": "pls",
                "label": "Плейлист PLS",
                "url": f"{base}/radio.pls",
                "description": "Файл плейлиста PLS для совместимых плееров",
            },
            {
                "key": "metadata",
                "label": "Метаданные эфира",
                "url": f"{base}/api/public/now-playing",
                "description": "Текущий трек, источник и статистика станции в JSON",
            },
        ]
    )
    return {"links": items}
