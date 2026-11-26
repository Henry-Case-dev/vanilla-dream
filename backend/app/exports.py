"""Media export: single-file downloads, tag rewriting and ZIP archives."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import unicodedata
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import mutagen
from mutagen.flac import Picture
from mutagen.id3 import APIC, TALB, TIT2, TPE1
from mutagen.mp4 import MP4Cover

from . import db, media
from .config import SYSTEM_PLAYLIST_ID, SYSTEM_PLAYLIST_NAME, Settings
from .models import playlist_available_ids
from .util import ApiError, is_relative_to, parse_iso, to_iso

CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
}
INVALID_FILENAME_CHARS = set('<>:"/\\|?*')
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}
_ARCHIVE_LOCK = threading.Lock()


class _Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


def sanitize_component(value, fallback: str = "") -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(
        char for char in text if ord(char) >= 32 and char not in INVALID_FILENAME_CHARS
    )
    text = " ".join(text.split()).strip(" .")
    if not text:
        text = fallback
    return text[:120]


def download_name(row) -> str:
    artist = sanitize_component(row["artist"], "") if row["artist"] else ""
    title = sanitize_component(row["title"], "Без названия")
    base = f"{artist} - {title}" if artist else title
    ext = str(row["ext"] or "")
    return f"{base}{ext}"


def content_disposition(filename: str) -> str:
    fallback = filename.encode("ascii", "ignore").decode("ascii")
    fallback = re.sub(r'["\\\r\n]', "_", fallback).strip() or "track"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


def _windows_reserved(base: str) -> str:
    if base.upper() in WINDOWS_RESERVED:
        return f"_{base}"
    return base


def _collision_key(name: str) -> str:
    # The numeric prefix is only ordering; equal artist/title/ext still collide.
    return re.sub(r"^\d{3,} - ", "", name, count=1).casefold()


def _unique_entry(name: str, used: set[str]) -> str:
    stem, ext = os.path.splitext(name)
    candidate = name
    counter = 2
    while _collision_key(candidate) in used:
        candidate = f"{stem} ({counter}){ext}"
        counter += 1
    used.add(_collision_key(candidate))
    return candidate


def _entry_name(index: int, row, ext: str, used: set[str]) -> str:
    artist = sanitize_component(row["artist"], "") if row["artist"] else ""
    title = sanitize_component(row["title"], "Без названия")
    base = f"{index:03d} - " + (f"{artist} - " if artist else "") + title
    base = _windows_reserved(base).strip(" .")
    return _unique_entry(f"{base}{ext}", used)


def _archive_name(scope_name: str | None, now: datetime) -> str:
    base = sanitize_component(scope_name or "Экспорт", "Экспорт")
    stamp = now.strftime("%Y%m%d-%H%M%S")
    return f"{base} - {stamp}.zip"


# ---------------------------------------------------------------------------
# Cover / tag rewriting
# ---------------------------------------------------------------------------


def _cover_bytes(settings: Settings, row) -> bytes | None:
    if isinstance(row, dict) and row.get("cover_data"):
        try:
            return base64.b64decode(row["cover_data"])
        except (TypeError, ValueError):
            return None
    path = media.effective_cover_path(settings, row)
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _rewrite_tags(settings: Settings, row, source: Path, destination: Path) -> None:
    """Copy ``source`` to ``destination`` and rewrite only its metadata tags."""

    shutil.copyfile(source, destination)
    ext = str(row["ext"] or "")
    title = str(row["title"] or "Без названия")
    artist = str(row["artist"] or "")
    album = str(row["album"] or "")
    cover = _cover_bytes(settings, row)
    try:
        audio = mutagen.File(str(destination))
        if audio is None:
            raise ApiError(422, "corrupt_audio", "Файл не является читаемым аудио или повреждён.")
        if ext == ".mp3":
            tags = audio.tags
            if tags is None:
                audio.add_tags()
                tags = audio.tags
            frames = (("TIT2", TIT2, title), ("TPE1", TPE1, artist), ("TALB", TALB, album))
            for frame_id, frame_type, value in frames:
                tags.delall(frame_id)
                if value:
                    tags.add(frame_type(encoding=3, text=[value]))
            tags.delall("APIC")
            if cover:
                tags.add(
                    APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover)
                )
            audio.save(v2_version=3)
        elif ext == ".m4a":
            tags = audio.tags
            if tags is None:
                audio.add_tags()
                tags = audio.tags
            for key, value in (
                ("\xa9nam", title),
                ("\xa9ART", artist),
                ("\xa9alb", album),
            ):
                if value:
                    tags[key] = [value]
                elif key in tags:
                    del tags[key]
            if cover:
                tags["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
            elif "covr" in tags:
                del tags["covr"]
            audio.save()
        elif ext == ".flac":
            for key, value in (("title", title), ("artist", artist), ("album", album)):
                if value:
                    audio[key] = [value]
                elif key in audio:
                    del audio[key]
            audio.clear_pictures()
            if cover:
                picture = Picture()
                picture.type = 3
                picture.mime = "image/jpeg"
                picture.desc = "Cover"
                picture.data = cover
                audio.add_picture(picture)
            audio.save()
        elif ext == ".ogg":
            for key, value in (("title", title), ("artist", artist), ("album", album)):
                if value:
                    audio[key] = [value]
                elif key in audio:
                    del audio[key]
            if cover:
                picture = Picture()
                picture.type = 3
                picture.mime = "image/jpeg"
                picture.desc = "Cover"
                picture.data = cover
                audio["metadata_block_picture"] = [
                    base64.b64encode(picture.write()).decode("ascii")
                ]
            elif "metadata_block_picture" in audio:
                del audio["metadata_block_picture"]
            audio.save()
        else:
            raise ApiError(422, "unsupported_format", "Формат файла не поддерживает теги.")
    except ApiError:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001 - mutagen raises many parser errors
        destination.unlink(missing_ok=True)
        raise ApiError(
            500,
            "tag_write_failed",
            "Не удалось подготовить файл с тегами. Скачайте оригинал.",
        ) from exc


def single_download(
    conn: sqlite3.Connection,
    settings: Settings,
    track_id: str,
    mode: str,
    now: datetime,
) -> tuple[Path, str, str, Path | None]:
    if mode not in ("tags", "original"):
        raise ApiError(422, "invalid_mode", "Режим скачивания должен быть tags или original.")
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if row is None:
        raise ApiError(404, "track_not_found", "Трек не найден.")
    source = media.resolve_track_path(settings, row)
    if not source.exists():
        raise ApiError(404, "file_not_found", "Файл трека не найден.")
    name = download_name(row)
    content_type = CONTENT_TYPES.get(str(row["ext"] or ""), "application/octet-stream")
    if mode == "original":
        return source, content_type, content_disposition(name), None

    settings.export_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = settings.export_dir / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        free = shutil.disk_usage(temp_dir).free
        incoming = source.stat().st_size
    except OSError:
        free, incoming = 0, 0
    if settings.min_free_bytes and free - incoming < settings.min_free_bytes:
        raise ApiError(
            507,
            "not_enough_space",
            "Недостаточно свободного места для подготовки файла с тегами.",
        )
    fd, temp_name = tempfile.mkstemp(
        prefix="tagged-", suffix=str(row["ext"] or ""), dir=str(temp_dir)
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        _rewrite_tags(settings, row, source, temp_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path, content_type, content_disposition(name), temp_path


# ---------------------------------------------------------------------------
# Batch composition
# ---------------------------------------------------------------------------


def _resolve_scope(
    conn: sqlite3.Connection, scope: str, body: dict
) -> tuple[list[str], str | None, str | None]:
    if scope == "tracks":
        raw = body.get("ids")
        if not isinstance(raw, list) or not raw:
            raise ApiError(422, "invalid_ids", "Выберите треки для скачивания.")
        if len(raw) > 10000:
            raise ApiError(422, "too_many_ids", "Слишком много треков для одного экспорта.")
        ids: list[str] = []
        for value in raw:
            track_id = str(value or "")
            if track_id and track_id not in ids:
                ids.append(track_id)
        return ids, None, "Выбранные треки"
    if scope == "playlist":
        playlist_id = str(body.get("playlist_id") or "")
        if not playlist_id:
            raise ApiError(422, "invalid_playlist", "Выберите плейлист для скачивания.")
        row = conn.execute(
            "SELECT id, name FROM playlists WHERE id = ?", (playlist_id,)
        ).fetchone()
        if row is None:
            raise ApiError(404, "playlist_not_found", "Плейлист не найден.")
        name = SYSTEM_PLAYLIST_NAME if playlist_id == SYSTEM_PLAYLIST_ID else row["name"]
        return playlist_available_ids(conn, playlist_id), playlist_id, name
    rows = conn.execute(
        "SELECT id FROM tracks WHERE pending_delete = 0 ORDER BY created_at ASC, id ASC"
    ).fetchall()
    return [str(row["id"]) for row in rows], None, "Вся медиатека"


def _existing_rows(conn: sqlite3.Connection, ids: list[str]) -> list:
    rows = []
    for track_id in ids:
        row = conn.execute(
            "SELECT * FROM tracks WHERE id = ? AND pending_delete = 0", (track_id,)
        ).fetchone()
        if row is not None:
            rows.append(row)
    return rows


ACTIVE_EXPORT_STATES = ("queued", "preparing", "ready", "downloading")


def track_is_exported(conn: sqlite3.Connection, track_id: str) -> bool:
    placeholders = ",".join("?" for _ in ACTIVE_EXPORT_STATES)
    rows = conn.execute(
        f"SELECT track_ids FROM export_jobs WHERE state IN ({placeholders})",
        ACTIVE_EXPORT_STATES,
    ).fetchall()
    for row in rows:
        try:
            ids = json.loads(row["track_ids"] or "[]")
        except (TypeError, ValueError):
            continue
        if track_id in {str(value) for value in ids}:
            return True
    return False


def path_is_exported(conn: sqlite3.Connection, path) -> bool:
    """True when an active export snapshot still references this absolute path."""

    target = Path(str(path))
    try:
        target_resolved = target.resolve()
    except OSError:
        target_resolved = target
    placeholders = ",".join("?" for _ in ACTIVE_EXPORT_STATES)
    rows = conn.execute(
        f"SELECT snapshot FROM export_jobs WHERE state IN ({placeholders})",
        ACTIVE_EXPORT_STATES,
    ).fetchall()
    for row in rows:
        try:
            snapshot = json.loads(row["snapshot"] or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(snapshot, list):
            continue
        for item in snapshot:
            if not isinstance(item, dict):
                continue
            stored = str(item.get("path") or "")
            if not stored:
                continue
            candidate = Path(stored)
            try:
                candidate_resolved = candidate.resolve()
            except OSError:
                candidate_resolved = candidate
            if candidate == target or candidate_resolved == target_resolved:
                return True
    return False


def _snapshot_track(settings: Settings, row) -> dict:
    source = media.resolve_track_path(settings, row)
    cover_path = media.effective_cover_path(settings, row)
    cover_data = None
    if cover_path is not None:
        try:
            cover_data = base64.b64encode(cover_path.read_bytes()).decode("ascii")
        except OSError:
            cover_data = None
    return {
        "track_id": str(row["id"]),
        "title": row["title"],
        "artist": row["artist"],
        "album": row["album"],
        "ext": row["ext"],
        "duration": row["duration"],
        "bitrate": row["bitrate"],
        "size": row["size"],
        "cover_path": media.effective_cover_name(row),
        "cover_data": cover_data,
        "path": str(source),
    }


def _snapshot_source(settings: Settings, snapshot: dict) -> Path:
    path = Path(str(snapshot.get("path") or ""))
    if not path.is_absolute():
        path = settings.music_dir / path
    if not is_relative_to(path, settings.music_dir):
        raise ApiError(404, "file_not_found", "Файл трека не найден.")
    return path


def disk_free(settings: Settings) -> int:
    directory = settings.export_dir
    directory.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(directory).free


def create_export(
    conn: sqlite3.Connection, settings: Settings, body: dict, now: datetime
) -> tuple[int, dict]:
    if not isinstance(body, dict):
        raise ApiError(422, "invalid_body", "Некорректный запрос.")
    scope = str(body.get("scope") or "tracks")
    if scope not in ("tracks", "playlist", "library"):
        raise ApiError(422, "invalid_scope", "Область экспорта должна быть tracks, playlist или library.")
    export_format = str(body.get("format") or "zip")
    if export_format not in ("zip", "files"):
        raise ApiError(422, "invalid_format", "Формат экспорта должен быть zip или files.")
    metadata_mode = str(body.get("metadata") or "tags")
    if metadata_mode not in ("tags", "original"):
        raise ApiError(422, "invalid_metadata", "Режим метаданных должен быть tags или original.")

    ids, scope_ref, scope_name = _resolve_scope(conn, scope, body)
    rows = _existing_rows(conn, ids)
    if not rows:
        raise ApiError(422, "export_empty", "Выбранные треки не найдены или пусты.")

    if export_format == "files":
        files = []
        for row in rows:
            source = media.resolve_track_path(settings, row)
            if not source.exists():
                continue
            files.append(
                {
                    "track_id": row["id"],
                    "name": download_name(row),
                    "size": int(row["size"] or 0),
                    "url": f"/api/admin/tracks/{row['id']}/download?mode={metadata_mode}",
                }
            )
        if not files:
            raise ApiError(422, "export_empty", "Файлы выбранных треков не найдены.")
        return 200, {"files": files, "total": len(files)}

    total_bytes = 0
    for row in rows:
        source = media.resolve_track_path(settings, row)
        try:
            total_bytes += source.stat().st_size
        except OSError:
            continue
    required = (
        int(settings.min_free_bytes or 0)
        + int(total_bytes * 1.05)
        + int(settings.export_reserve_bytes or 0)
    )
    free = disk_free(settings)
    if free < required:
        raise ApiError(
            507,
            "export_not_enough_space",
            "Недостаточно свободного места для подготовки архива. "
            "Освободите место и попробуйте снова.",
        )

    snapshot = [_snapshot_track(settings, row) for row in rows]
    job_id = os.urandom(16).hex()
    stamp = to_iso(now)
    conn.execute(
        "INSERT INTO export_jobs(id, scope, scope_ref, track_ids, metadata_mode, format, state, "
        "processed, total, archive_path, archive_name, size, error, snapshot, created_at, "
        "updated_at, expires_at) VALUES(?, ?, ?, ?, ?, 'zip', 'queued', 0, ?, NULL, NULL, NULL, "
        "NULL, ?, ?, ?, NULL)",
        (
            job_id,
            scope,
            scope_ref,
            json.dumps([str(row["id"]) for row in rows], ensure_ascii=False),
            metadata_mode,
            len(rows),
            json.dumps(snapshot, ensure_ascii=False),
            stamp,
            stamp,
        ),
    )
    conn.commit()
    return 202, {
        "job": {
            "id": job_id,
            "state": "queued",
            "processed": 0,
            "total": len(rows),
            "progress": 0.0,
        }
    }


# ---------------------------------------------------------------------------
# Job processing
# ---------------------------------------------------------------------------


def _job_row(conn: sqlite3.Connection, job_id: str):
    return conn.execute("SELECT * FROM export_jobs WHERE id = ?", (job_id,)).fetchone()


def job_payload(row) -> dict:
    state = str(row["state"] or "queued")
    processed = int(row["processed"] or 0)
    total = int(row["total"] or 0)
    if total:
        progress = round(min(1.0, processed / total), 4)
    else:
        progress = 1.0 if state == "ready" else 0.0
    archive = None
    if state in ("ready", "downloading") and row["archive_name"]:
        archive = {"name": row["archive_name"], "size": int(row["size"] or 0)}
    return {
        "id": row["id"],
        "state": state,
        "processed": processed,
        "total": total,
        "progress": progress,
        "archive": archive,
        "error": row["error"],
        "expires_at": row["expires_at"],
        "scope": row["scope"],
        "format": row["format"],
        "metadata": row["metadata_mode"],
    }


def _job_dir(settings: Settings, job_id: str) -> Path:
    return settings.export_dir / job_id


def _cleanup_job_files(settings: Settings, job_id: str) -> None:
    shutil.rmtree(_job_dir(settings, job_id), ignore_errors=True)
    shutil.rmtree(settings.export_dir / "tmp" / job_id, ignore_errors=True)


def process_job(settings: Settings, job_id: str, now: datetime | None = None) -> None:
    with _ARCHIVE_LOCK:
        conn = db.open_db(settings)
        job_dir = _job_dir(settings, job_id)
        tmp_dir = settings.export_dir / "tmp" / job_id
        try:
            row = _job_row(conn, job_id)
            if row is None or row["state"] != "queued":
                return
            now = now or datetime.now(tz=timezone.utc)
            conn.execute(
                "UPDATE export_jobs SET state = 'preparing', updated_at = ? WHERE id = ?",
                (to_iso(now), job_id),
            )
            conn.commit()
            ids = json.loads(row["track_ids"] or "[]")
            snapshot = json.loads(row["snapshot"] or "[]") if row["snapshot"] else []
            if not snapshot:
                snapshot = []
                for track_id in ids:
                    track = conn.execute(
                        "SELECT * FROM tracks WHERE id = ?", (track_id,)
                    ).fetchone()
                    if track is not None:
                        snapshot.append(_snapshot_track(settings, track))
            metadata_mode = row["metadata_mode"] or "tags"
            total_bytes = 0
            for item in snapshot:
                try:
                    total_bytes += _snapshot_source(settings, item).stat().st_size
                except OSError:
                    continue
            required = (
                int(settings.min_free_bytes or 0)
                + int(total_bytes * 1.05)
                + int(settings.export_reserve_bytes or 0)
            )
            if disk_free(settings) < required:
                raise ApiError(
                    507,
                    "export_not_enough_space",
                    "Недостаточно свободного места для подготовки архива. "
                    "Освободите место и попробуйте снова.",
                )
            job_dir.mkdir(parents=True, exist_ok=True)
            tmp_dir.mkdir(parents=True, exist_ok=True)
            scope_name = None
            if row["scope"] == "playlist":
                playlist = conn.execute(
                    "SELECT name FROM playlists WHERE id = ?", (row["scope_ref"],)
                ).fetchone()
                scope_name = (
                    SYSTEM_PLAYLIST_NAME
                    if row["scope_ref"] == SYSTEM_PLAYLIST_ID
                    else (playlist["name"] if playlist else "Плейлист")
                )
            elif row["scope"] == "library":
                scope_name = "Вся медиатека"
            else:
                scope_name = "Выбранные треки"
            archive_name = _archive_name(scope_name, now)
            archive_path = job_dir / archive_name
            used: set[str] = set()
            entries: list[str] = []
            with zipfile.ZipFile(
                archive_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True
            ) as archive:
                for index, item in enumerate(snapshot, start=1):
                    current = _job_row(conn, job_id)
                    if current is None or current["state"] in ("cancelled", "error"):
                        raise _Cancelled()
                    source = _snapshot_source(settings, item)
                    if not source.exists():
                        raise ApiError(404, "file_not_found", "Файл трека не найден.")
                    ext = str(item.get("ext") or "")
                    arcname = _entry_name(index, item, ext, used)
                    if metadata_mode == "tags":
                        prepared = tmp_dir / f"{index}{ext}"
                        _rewrite_tags(settings, item, source, prepared)
                        archive.write(prepared, arcname)
                        prepared.unlink(missing_ok=True)
                    else:
                        archive.write(source, arcname)
                    entries.append(arcname)
                    conn.execute(
                        "UPDATE export_jobs SET processed = ?, updated_at = ? WHERE id = ?",
                        (index, to_iso(now), job_id),
                    )
                    conn.commit()
                archive.writestr(
                    "playlist.m3u8",
                    ("#EXTM3U\n" + "\n".join(entries) + "\n").encode("utf-8"),
                )
            current = _job_row(conn, job_id)
            if current is None or current["state"] in ("cancelled", "error"):
                raise _Cancelled()
            size = archive_path.stat().st_size
            finished = now
            expires = finished + timedelta(
                hours=max(0.0, float(settings.export_ttl_hours or 6.0))
            )
            conn.execute(
                "UPDATE export_jobs SET state = 'ready', processed = ?, archive_path = ?, "
                "archive_name = ?, size = ?, error = NULL, updated_at = ?, finished_at = ?, "
                "expires_at = ? WHERE id = ?",
                (
                    len(snapshot),
                    str(archive_path),
                    archive_name,
                    size,
                    to_iso(finished),
                    to_iso(finished),
                    to_iso(expires),
                    job_id,
                ),
            )
            conn.commit()
        except _Cancelled:
            _cleanup_job_files(settings, job_id)
            conn.execute(
                "UPDATE export_jobs SET state = 'cancelled', updated_at = ? WHERE id = ?",
                (to_iso(now), job_id),
            )
            conn.commit()
        except ApiError as exc:
            _cleanup_job_files(settings, job_id)
            conn.execute(
                "UPDATE export_jobs SET state = 'error', error = ?, updated_at = ? WHERE id = ?",
                (exc.message, to_iso(now), job_id),
            )
            conn.commit()
        except Exception:  # noqa: BLE001 - a background job must not crash the app
            _cleanup_job_files(settings, job_id)
            try:
                conn.execute(
                    "UPDATE export_jobs SET state = 'error', error = ?, updated_at = ? "
                    "WHERE id = ?",
                    (
                        "Не удалось подготовить архив.",
                        to_iso(now),
                        job_id,
                    ),
                )
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            conn.close()


# ---------------------------------------------------------------------------
# Cancellation / lookup / cleanup
# ---------------------------------------------------------------------------


def get_job(conn: sqlite3.Connection, job_id: str) -> dict:
    row = _job_row(conn, job_id)
    if row is None:
        raise ApiError(404, "export_not_found", "Экспорт не найден.")
    return job_payload(row)


def mark_downloading(
    conn: sqlite3.Connection, job_id: str, now: datetime
) -> None:
    row = _job_row(conn, job_id)
    if row is None:
        raise ApiError(404, "export_not_found", "Экспорт не найден.")
    if row["state"] == "ready":
        conn.execute(
            "UPDATE export_jobs SET state = 'downloading', updated_at = ? WHERE id = ?",
            (to_iso(now), job_id),
        )
        conn.commit()


def finish_download(settings: Settings, job_id: str) -> None:
    conn = db.open_db(settings)
    try:
        row = _job_row(conn, job_id)
        if row is not None and row["state"] == "downloading":
            conn.execute(
                "UPDATE export_jobs SET state = 'ready', updated_at = ? WHERE id = ?",
                (to_iso(datetime.now(tz=timezone.utc)), job_id),
            )
            conn.commit()
    finally:
        conn.close()


def cancel_job(
    conn: sqlite3.Connection, settings: Settings, job_id: str, now: datetime
) -> dict:
    row = _job_row(conn, job_id)
    if row is None:
        raise ApiError(404, "export_not_found", "Экспорт не найден.")
    state = str(row["state"] or "")
    if state == "downloading":
        raise ApiError(409, "export_downloading", "Архив уже скачивается.")
    if state in ("cancelled", "error"):
        _cleanup_job_files(settings, job_id)
        return {"job": job_payload(row)}
    _cleanup_job_files(settings, job_id)
    conn.execute(
        "UPDATE export_jobs SET state = 'cancelled', archive_path = NULL, updated_at = ? "
        "WHERE id = ?",
        (to_iso(now), job_id),
    )
    conn.commit()
    return {"job": job_payload(_job_row(conn, job_id))}


def retry_job(
    conn: sqlite3.Connection, settings: Settings, job_id: str, now: datetime
) -> dict:
    row = _job_row(conn, job_id)
    if row is None:
        raise ApiError(404, "export_not_found", "Экспорт не найден.")
    if str(row["state"] or "") != "error":
        raise ApiError(409, "export_not_retryable", "Повторить можно только неудачный экспорт.")
    _cleanup_job_files(settings, job_id)
    conn.execute(
        "UPDATE export_jobs SET state = 'queued', processed = 0, archive_path = NULL, "
        "archive_name = NULL, size = NULL, error = NULL, finished_at = NULL, "
        "expires_at = NULL, updated_at = ? WHERE id = ?",
        (to_iso(now), job_id),
    )
    conn.commit()
    return {"job": {"id": job_id, "state": "queued", "processed": 0, "total": int(row["total"] or 0), "progress": 0.0}}



def archive_file(
    conn: sqlite3.Connection, settings: Settings, job_id: str
) -> tuple[Path, str]:
    row = _job_row(conn, job_id)
    if row is None:
        raise ApiError(404, "export_not_found", "Экспорт не найден.")
    if row["state"] != "ready" or not row["archive_path"]:
        raise ApiError(409, "export_not_ready", "Архив ещё не готов.")
    path = Path(str(row["archive_path"]))
    if not is_relative_to(path, settings.export_dir) or not path.exists():
        raise ApiError(404, "archive_not_found", "Файл архива не найден.")
    return path, str(row["archive_name"] or path.name)


def cleanup_exports(
    conn: sqlite3.Connection, settings: Settings, now: datetime
) -> int:
    removed = 0
    rows = conn.execute("SELECT * FROM export_jobs").fetchall()
    for row in rows:
        job_id = str(row["id"])
        state = str(row["state"] or "")
        expires = parse_iso(row["expires_at"])
        if expires is None:
            finished = parse_iso(row["finished_at"])
            if finished is not None:
                expires = finished + timedelta(
                    hours=max(0.0, float(settings.export_ttl_hours or 6.0))
                )
        expired = expires is not None and expires <= now
        if state == "downloading":
            # Never delete an archive while a response is streaming it.
            if not row["archive_path"] or not Path(str(row["archive_path"])).exists():
                _cleanup_job_files(settings, job_id)
                conn.execute(
                    "UPDATE export_jobs SET state = 'error', error = ?, updated_at = ? "
                    "WHERE id = ?",
                    ("Файл архива не найден.", to_iso(now), job_id),
                )
            else:
                updated = parse_iso(row["updated_at"])
                if updated is not None and now - updated > timedelta(minutes=30):
                    conn.execute(
                        "UPDATE export_jobs SET state = 'ready', updated_at = ? WHERE id = ?",
                        (to_iso(now), job_id),
                    )
            continue
        if expired or state in ("cancelled", "error"):
            _cleanup_job_files(settings, job_id)
            if expired:
                conn.execute("DELETE FROM export_jobs WHERE id = ?", (job_id,))
                removed += 1
        elif state == "ready":
            if not row["archive_path"] or not Path(str(row["archive_path"])).exists():
                _cleanup_job_files(settings, job_id)
                conn.execute(
                    "UPDATE export_jobs SET state = 'error', error = ?, updated_at = ? "
                    "WHERE id = ?",
                    ("Файл архива не найден.", to_iso(now), job_id),
                )
        elif state in ("queued", "preparing"):
            created = parse_iso(row["created_at"])
            ttl = timedelta(hours=max(0.0, float(settings.export_ttl_hours or 6.0)))
            if created is not None and now - created > ttl:
                _cleanup_job_files(settings, job_id)
                conn.execute(
                    "UPDATE export_jobs SET state = 'error', error = ?, updated_at = ? "
                    "WHERE id = ?",
                    ("Экспорт прерван.", to_iso(now), job_id),
                )
    tmp_root = settings.export_dir / "tmp"
    if tmp_root.is_dir():
        for entry in tmp_root.iterdir():
            try:
                modified = datetime.fromtimestamp(entry.stat().st_mtime, tz=now.tzinfo)
            except OSError:
                continue
            ttl = timedelta(hours=max(0.0, float(settings.export_ttl_hours or 6.0)))
            if now - modified > ttl:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    try:
                        entry.unlink(missing_ok=True)
                    except OSError:
                        pass
    conn.commit()
    return removed


def startup_cleanup(conn: sqlite3.Connection, settings: Settings, now: datetime) -> None:
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    tmp_root = settings.export_dir / "tmp"
    if tmp_root.is_dir():
        for entry in list(tmp_root.iterdir()):
            try:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
            except OSError:
                continue
    for row in conn.execute("SELECT * FROM export_jobs").fetchall():
        job_id = str(row["id"])
        state = str(row["state"] or "")
        if state in ("queued", "preparing"):
            _cleanup_job_files(settings, job_id)
            conn.execute(
                "UPDATE export_jobs SET state = 'error', error = ?, updated_at = ? WHERE id = ?",
                ("Экспорт прерван перезапуском сервиса.", to_iso(now), job_id),
            )
        elif state == "downloading":
            missing = not row["archive_path"] or not Path(str(row["archive_path"])).exists()
            if missing:
                _cleanup_job_files(settings, job_id)
                conn.execute(
                    "UPDATE export_jobs SET state = 'error', error = ?, updated_at = ? "
                    "WHERE id = ?",
                    ("Файл архива не найден.", to_iso(now), job_id),
                )
            else:
                conn.execute(
                    "UPDATE export_jobs SET state = 'ready', updated_at = ? WHERE id = ?",
                    (to_iso(now), job_id),
                )
        elif state == "ready":
            expires = parse_iso(row["expires_at"])
            missing = not row["archive_path"] or not Path(str(row["archive_path"])).exists()
            if (expires is not None and expires <= now) or missing:
                _cleanup_job_files(settings, job_id)
                if expires is not None and expires <= now:
                    conn.execute("DELETE FROM export_jobs WHERE id = ?", (job_id,))
                else:
                    conn.execute(
                        "UPDATE export_jobs SET state = 'error', error = ?, updated_at = ? "
                        "WHERE id = ?",
                        ("Файл архива не найден.", to_iso(now), job_id),
                    )
            elif state == "downloading":
                conn.execute(
                    "UPDATE export_jobs SET state = 'ready', updated_at = ? WHERE id = ?",
                    (to_iso(now), job_id),
                )
        elif state in ("cancelled", "error"):
            _cleanup_job_files(settings, job_id)
    conn.commit()
