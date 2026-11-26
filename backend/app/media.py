"""Media library: streaming ingest, mutagen metadata, covers, deletion."""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

import mutagen
from mutagen.flac import Picture
from mutagen.id3 import APIC, ID3

from . import db
from .config import Settings
from .util import ApiError, is_relative_to, remove_queue_link, to_iso

LOGGER = logging.getLogger("vdradio.media")

ALLOWED_EXTS = (".mp3", ".flac", ".ogg", ".m4a")
CHUNK_SIZE = 1024 * 1024
MAX_COVER_SIDE = 800
PLAYLIST_COVER_SIDE = 600
PLAYLIST_COVER_MAX_BYTES = 8 * 1024 * 1024
YEAR_MIN = 1900
YEAR_MAX = 2100


def _first(value):
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _year_from_text(text: str | None) -> int | None:
    if not text:
        return None
    digits = ""
    for char in str(text):
        if char.isdigit():
            digits += char
            if len(digits) == 4:
                break
        elif digits:
            break
    if len(digits) >= 4:
        try:
            year = int(digits[:4])
        except ValueError:
            return None
        if YEAR_MIN <= year <= YEAR_MAX:
            return year
    return None


def _id3_text(tags, frame_id: str) -> str | None:
    try:
        frame = tags.get(frame_id)
    except Exception:  # noqa: BLE001 - malformed tag frames
        return None
    if frame is None:
        return None
    text = getattr(frame, "text", None)
    value = _first(text)
    return str(value) if value is not None else None


def read_metadata(audio) -> dict:
    """Read a normalised tag dict from any mutagen file object."""

    result: dict = {
        "title": None,
        "artist": None,
        "album": None,
        "genre": None,
        "year": None,
    }
    tags = getattr(audio, "tags", None)
    if tags is None:
        return result
    try:
        if isinstance(tags, ID3):
            result["title"] = _id3_text(tags, "TIT2")
            result["artist"] = _id3_text(tags, "TPE1")
            result["album"] = _id3_text(tags, "TALB")
            result["genre"] = _id3_text(tags, "TCON")
            year_text = _id3_text(tags, "TDRC") or _id3_text(tags, "TYER")
            result["year"] = _year_from_text(year_text)
        else:
            getter = tags.get
            result["title"] = _first(getter("title"))
            result["artist"] = _first(getter("artist"))
            result["album"] = _first(getter("album"))
            result["genre"] = _first(getter("genre"))
            year_value = (
                _first(getter("date"))
                or _first(getter("year"))
                or _first(getter("originaldate"))
            )
            result["year"] = _year_from_text(str(year_value)) if year_value else None
    except Exception:  # noqa: BLE001 - a broken tag must not break upload
        LOGGER.warning("failed to read tags", exc_info=True)
    for key in ("title", "artist", "album", "genre"):
        if result[key] is not None:
            result[key] = str(result[key]).strip() or None
    return result


def probe_audio(path: Path, ext: str) -> dict:
    """Validate a readable audio file and return normalised technical metadata."""

    try:
        audio = mutagen.File(str(path))
    except Exception as exc:  # noqa: BLE001 - mutagen raises many parser errors
        raise ApiError(
            422,
            "corrupt_audio",
            "Файл не является читаемым аудио или повреждён.",
        ) from exc
    if audio is None or getattr(audio, "info", None) is None:
        raise ApiError(422, "corrupt_audio", "Файл не является читаемым аудио или повреждён.")
    info = audio.info
    try:
        duration = float(getattr(info, "length", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise ApiError(422, "corrupt_audio", "Файл не является читаемым аудио или повреждён.")
    bitrate = getattr(info, "bitrate", None)
    try:
        bitrate_kbps = int(round(float(bitrate) / 1000.0)) if bitrate else None
    except (TypeError, ValueError):
        bitrate_kbps = None
    if not bitrate_kbps:
        size = path.stat().st_size
        bitrate_kbps = int(round(size * 8 / duration / 1000.0)) or None
    metadata = read_metadata(audio)
    return {
        "duration": round(duration, 3),
        "bitrate": bitrate_kbps,
        "tags": metadata,
        "audio": audio,
    }


def extract_cover_bytes(audio, ext: str) -> bytes | None:
    try:
        if ext == ".mp3":
            tags = getattr(audio, "tags", None)
            if isinstance(tags, ID3):
                frames = tags.getall("APIC")
                for frame in frames:
                    if getattr(frame, "data", None):
                        return frame.data
        elif ext == ".flac":
            pictures = getattr(audio, "pictures", None) or []
            if pictures:
                return pictures[0].data
        elif ext == ".m4a":
            tags = getattr(audio, "tags", None)
            covers = tags.get("covr") if tags else None
            if covers:
                return bytes(covers[0])
        elif ext == ".ogg":
            tags = getattr(audio, "tags", None)
            encoded = tags.get("metadata_block_picture") if tags else None
            if encoded:
                picture = Picture(base64.b64decode(_first(encoded)))
                return picture.data
    except Exception:  # noqa: BLE001 - cover extraction is best effort
        LOGGER.warning("cover extraction failed", exc_info=True)
    return None


def cover_path_for(settings: Settings, track_id: str) -> Path:
    return settings.cover_dir / f"{track_id}.jpg"


def effective_cover_path(settings: Settings, row) -> Path | None:
    """Return the effective cover file: custom first, then the extracted one."""

    custom = row["custom_cover_path"] if "custom_cover_path" in row.keys() else None
    if custom:
        candidate = settings.cover_dir / str(custom)
        if is_relative_to(candidate, settings.cover_dir) and candidate.exists():
            return candidate
    extracted = row["cover_path"] if "cover_path" in row.keys() else None
    if extracted:
        candidate = settings.cover_dir / str(extracted)
        if is_relative_to(candidate, settings.cover_dir) and candidate.exists():
            return candidate
    return None


def effective_cover_name(row) -> str | None:
    custom = row["custom_cover_path"] if "custom_cover_path" in row.keys() else None
    if custom:
        return str(custom)
    extracted = row["cover_path"] if "cover_path" in row.keys() else None
    return str(extracted) if extracted else None


def _render_square_cover(stream: BinaryIO) -> bytes:
    from PIL import Image

    data = stream.read(PLAYLIST_COVER_MAX_BYTES + 1)
    if len(data) > PLAYLIST_COVER_MAX_BYTES:
        raise ApiError(
            413,
            "cover_too_large",
            "Обложка больше 8 МБ. Выберите файл меньшего размера.",
        )
    if not data:
        raise ApiError(422, "invalid_cover", "Файл обложки пустой.")
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        if image.format not in ("JPEG", "PNG", "WEBP"):
            raise ValueError("unsupported image format")
    except Exception as exc:  # noqa: BLE001 - Pillow raises many formats
        raise ApiError(
            422,
            "invalid_cover",
            "Не удалось прочитать изображение. Поддерживаются JPEG, PNG и WebP.",
        ) from exc
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    image = image.crop((left, top, left + side, top + side))
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    image = image.resize((PLAYLIST_COVER_SIDE, PLAYLIST_COVER_SIDE), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88, optimize=True)
    return buffer.getvalue()


def prepare_custom_cover(
    settings: Settings, track_id: str, stream: BinaryIO
) -> tuple[Path, str]:
    """Validate and write a new custom cover to a temporary file."""

    rendered = _render_square_cover(stream)
    digest = hashlib.sha256(rendered).hexdigest()[:8]
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(track_id))[:64] or "track"
    custom_dir = settings.cover_dir / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".cover-", suffix=".jpg", dir=str(custom_dir))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return Path(temp_name), f"custom/{safe_id}-{digest}.jpg"


def save_custom_cover(settings: Settings, track_id: str, stream: BinaryIO) -> str:
    """Validate, crop and store a custom square 600x600 JPEG cover."""

    temp_path, relative = prepare_custom_cover(settings, track_id, stream)
    destination = settings.cover_dir / relative
    try:
        os.replace(temp_path, destination)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return relative


def remove_custom_cover(settings: Settings, relative: str | None) -> None:
    if not relative:
        return
    candidate = settings.cover_dir / str(relative)
    if not is_relative_to(candidate, settings.cover_dir / "custom"):
        return
    try:
        candidate.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("failed to delete custom cover %s", candidate, exc_info=True)


def write_cover(settings: Settings, track_id: str, data: bytes) -> str | None:
    """Re-encode an embedded picture to JPEG (max 800x800) and store it."""

    from PIL import Image

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.thumbnail((MAX_COVER_SIDE, MAX_COVER_SIDE))
        settings.cover_dir.mkdir(parents=True, exist_ok=True)
        destination = cover_path_for(settings, track_id)
        temp = destination.with_suffix(".tmp")
        image.save(temp, format="JPEG", quality=85, optimize=True)
        os.replace(temp, destination)
        return destination.name
    except Exception:  # noqa: BLE001 - invalid picture data must not fail ingest
        LOGGER.warning("failed to re-encode cover for %s", track_id, exc_info=True)
        return None


def save_playlist_cover(
    settings: Settings, playlist_id: str, stream: BinaryIO
) -> str:
    """Validate, crop, resize and store a custom playlist cover as JPEG."""

    from PIL import Image

    data = stream.read(PLAYLIST_COVER_MAX_BYTES + 1)
    if len(data) > PLAYLIST_COVER_MAX_BYTES:
        raise ApiError(
            413,
            "cover_too_large",
            "Обложка больше 8 МБ. Выберите файл меньшего размера.",
        )
    if not data:
        raise ApiError(422, "invalid_cover", "Файл обложки пустой.")
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        if image.format not in ("JPEG", "PNG", "WEBP"):
            raise ValueError("unsupported image format")
    except Exception as exc:  # noqa: BLE001 - Pillow raises many formats
        raise ApiError(
            422,
            "invalid_cover",
            "Не удалось прочитать изображение. Поддерживаются JPEG, PNG и WebP.",
        ) from exc
    settings.playlist_cover_dir.mkdir(parents=True, exist_ok=True)
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    image = image.crop((left, top, left + side, top + side))
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    image = image.resize((PLAYLIST_COVER_SIDE, PLAYLIST_COVER_SIDE), Image.LANCZOS)
    digest = hashlib.sha256(data).hexdigest()[:8]
    destination = settings.playlist_cover_dir / f"{playlist_id}-{digest}.jpg"
    fd, temp_name = tempfile.mkstemp(
        prefix=".cover-", suffix=".jpg", dir=str(settings.playlist_cover_dir)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            image.save(handle, format="JPEG", quality=88, optimize=True)
        os.replace(temp_name, destination)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return destination.name


def resolve_track_path(settings: Settings, row: sqlite3.Row) -> Path:
    stored = str(row["path"] or "")
    path = Path(stored)
    if not path.is_absolute():
        path = settings.music_dir / path
    if not is_relative_to(path, settings.music_dir):
        raise ApiError(404, "not_found", "Файл трека не найден.")
    return path


def disk_state(settings: Settings) -> dict:
    usage = shutil.disk_usage(settings.music_dir)
    return {"free_bytes": usage.free, "total_bytes": usage.total, "used_bytes": usage.used}


def _check_upload_limits(conn: sqlite3.Connection, settings: Settings, incoming: int) -> None:
    usage = disk_state(settings)
    if settings.min_free_bytes and usage["free_bytes"] - incoming < settings.min_free_bytes:
        raise ApiError(
            507,
            "not_enough_space",
            "Недостаточно свободного места на диске для загрузки файла.",
        )
    limit_gb = db.get_float_setting(conn, "max_library_gb", 0.0)
    if limit_gb > 0:
        limit_bytes = int(limit_gb * 1024**3)
        used = db.library_totals(conn)["bytes"]
        if used + incoming > limit_bytes:
            raise ApiError(
                507,
                "library_limit",
                "Достигнут настроенный лимит размера медиатеки.",
            )


def _normalise_filename(filename: str | None) -> tuple[str, str]:
    raw = (filename or "").replace("\\", "/")
    base = raw.rsplit("/", 1)[-1]
    base = base.strip()
    ext = Path(base).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise ApiError(
            415,
            "unsupported_format",
            "Поддерживаются только файлы MP3, FLAC, OGG и M4A.",
        )
    return base, ext


def ingest_upload(
    conn: sqlite3.Connection,
    settings: Settings,
    stream: BinaryIO,
    filename: str | None,
    now: datetime,
    batch_id: str | None = None,
) -> dict:
    """Compatibility shim: the two-phase pipeline lives in :mod:`app.uploads`."""

    from . import uploads

    return uploads.receive_upload(conn, settings, stream, filename, now, batch_id=batch_id)


def _export_protects_track(conn: sqlite3.Connection, track_id: str) -> bool:
    try:
        from . import exports

        return exports.track_is_exported(conn, track_id)
    except Exception:  # noqa: BLE001 - protection is best effort, never destructive
        LOGGER.warning("failed to check export protection for %s", track_id, exc_info=True)
        return False


def _export_protects_path(conn: sqlite3.Connection, path: Path) -> bool:
    try:
        from . import exports

        return exports.path_is_exported(conn, path)
    except Exception:  # noqa: BLE001 - protection is best effort, never destructive
        LOGGER.warning("failed to check export path protection for %s", path, exc_info=True)
        return True


def retained_path(settings: Settings, stored) -> Path | None:
    if not stored:
        return None
    path = Path(str(stored))
    if not path.is_absolute():
        path = settings.music_dir / path
    return path


def _queue_link_targets(settings: Settings) -> set[Path]:
    """Absolute targets still referenced by the engine queue symlinks."""

    targets: set[Path] = set()
    link_dir = Path(settings.queue_link_dir)
    try:
        entries = list(link_dir.glob("*.mp3"))
    except OSError:
        return targets
    for entry in entries:
        try:
            if entry.is_symlink():
                targets.add(Path(os.readlink(entry)).resolve())
        except OSError:
            continue
    return targets


def cleanup_retained_files(conn: sqlite3.Connection, settings: Settings) -> int:
    """Delete replaced files once every consumer released them.

    Safe by construction: a failed unlink keeps the row for the next pass, the
    row is removed only after the file is gone, and an unresolved engine sync
    or an active export snapshot blocks deletion entirely.
    """

    rows = conn.execute(
        "SELECT * FROM retained_files ORDER BY id ASC"
    ).fetchall()
    if not rows:
        return 0
    if queue_sync_unresolved(conn):
        return 0
    links = _queue_link_targets(settings)
    removed = 0
    for row in rows:
        path = retained_path(settings, row["path"])
        if path is None:
            conn.execute("DELETE FROM retained_files WHERE id = ?", (row["id"],))
            continue
        if not is_relative_to(path, settings.music_dir):
            # Never delete outside the library; drop only the bookkeeping row.
            LOGGER.warning("retained file outside the library: %s", path)
            conn.execute("DELETE FROM retained_files WHERE id = ?", (row["id"],))
            continue
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in links:
            continue
        if conn.execute(
            "SELECT 1 FROM tracks WHERE path = ? LIMIT 1", (str(row["path"]),)
        ).fetchone() is not None:
            # Defensive: a retained path that some track still uses is never
            # deleted, even if the bookkeeping row is wrong.
            continue
        if _export_protects_path(conn, path):
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("failed to delete retained file %s", path, exc_info=True)
            continue
        conn.execute("DELETE FROM retained_files WHERE id = ?", (row["id"],))
        removed += 1
    conn.commit()
    return removed


def purge_track(conn: sqlite3.Connection, settings: Settings, track_id: str) -> bool:
    """Delete the physical file first; keep the row when deletion fails."""

    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if row is None:
        return True
    if _export_protects_track(conn, track_id):
        warning_key = f"_warn:export-protect:{track_id}"
        if db.get_setting(conn, warning_key) != "1":
            db.set_setting(conn, warning_key, "1")
            db.log_event(
                conn,
                "info",
                "media",
                "Удаление трека отложено: файл используется экспортом",
                {"track_id": track_id},
            )
        return False
    path = Path(str(row["path"] or ""))
    if not path.is_absolute():
        path = settings.music_dir / path
    if not is_relative_to(path, settings.music_dir):
        db.log_event(
            conn,
            "error",
            "media",
            "Удаление трека отменено: путь вне медиатеки",
            {"track_id": track_id, "path": str(row["path"] or "")},
        )
        return False
    try:
        path.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("failed to delete audio file %s", path, exc_info=True)
        db.log_event(
            conn,
            "error",
            "media",
            "Не удалось удалить файл трека, попытка будет повторена",
            {"track_id": track_id, "path": str(path)},
        )
        return False
    cover = row["cover_path"]
    if cover:
        cover_file = settings.cover_dir / str(cover)
        if is_relative_to(cover_file, settings.cover_dir):
            try:
                cover_file.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("failed to delete cover %s", cover_file, exc_info=True)
    if "custom_cover_path" in row.keys():
        remove_custom_cover(settings, row["custom_cover_path"])
    for retained in conn.execute(
        "SELECT id, path FROM retained_files WHERE track_id = ?", (track_id,)
    ).fetchall():
        retained_file = retained_path(settings, retained["path"])
        if retained_file is not None and is_relative_to(retained_file, settings.music_dir):
            if conn.execute(
                "SELECT 1 FROM tracks WHERE path = ? AND id <> ? LIMIT 1",
                (str(retained["path"]), track_id),
            ).fetchone() is not None:
                conn.execute(
                    "DELETE FROM retained_files WHERE id = ?", (retained["id"],)
                )
                continue
            try:
                retained_file.unlink(missing_ok=True)
            except OSError:
                # Keep the row so the shared cleanup pass retries the unlink.
                continue
        conn.execute("DELETE FROM retained_files WHERE id = ?", (retained["id"],))
    conn.execute("DELETE FROM playlist_tracks WHERE track_id = ?", (track_id,))
    if conn.execute(
        "DELETE FROM queue_positions WHERE track_id = ?", (track_id,)
    ).rowcount:
        from . import queue_engine

        queue_engine.bump_queue_revision(conn)
    conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
    return True


def playlists_with_track(conn: sqlite3.Connection, track_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM playlist_tracks WHERE track_id = ?", (track_id,)
    ).fetchone()
    return int(row["count"] or 0)


def is_track_current(conn: sqlite3.Connection, track_id: str) -> bool:
    state = conn.execute("SELECT track_id FROM playback_state WHERE singleton = 1").fetchone()
    if state is not None and state["track_id"] == track_id:
        return True
    row = conn.execute(
        "SELECT 1 FROM queue_positions WHERE state = 'current' AND track_id = ? LIMIT 1",
        (track_id,),
    ).fetchone()
    return row is not None


def track_has_future_positions(conn: sqlite3.Connection, track_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM queue_positions WHERE track_id = ? "
            "AND state IN ('pending', 'prepared') LIMIT 1",
            (track_id,),
        ).fetchone()
        is not None
    )


def queue_sync_unresolved(conn: sqlite3.Connection) -> bool:
    """A failed sync means the engine may still reference the old queue."""

    return bool(db.get_setting(conn, "_engine_sync_error"))


def purge_confirmed(
    conn: sqlite3.Connection, settings: Settings, track_id: str
) -> bool:
    """Purge a pending deletion only when the engine has released the file."""

    row = conn.execute(
        "SELECT pending_delete FROM tracks WHERE id = ?", (track_id,)
    ).fetchone()
    if row is None or not int(row["pending_delete"] or 0):
        return False
    if is_track_current(conn, track_id):
        return False
    if track_has_future_positions(conn, track_id):
        return False
    if queue_sync_unresolved(conn):
        return False
    return purge_track(conn, settings, track_id)


def _cancel_pending_for_tracks(conn: sqlite3.Connection, track_ids: set[str]) -> None:
    from . import queue_engine

    pending = queue_engine.pending_source(conn)
    if pending is None:
        return
    head_id = str(pending.get("head_position_id") or "")
    if not head_id:
        return
    row = conn.execute(
        "SELECT track_id FROM queue_positions WHERE id = ?", (head_id,)
    ).fetchone()
    if row is None or str(row["track_id"]) in track_ids:
        queue_engine.cancel_pending_source(conn)


def delete_track(
    conn: sqlite3.Connection,
    settings: Settings,
    track_id: str,
    now: datetime,
    mode: str = "after",
) -> dict:
    if mode not in ("after", "now"):
        raise ApiError(422, "invalid_delete_mode", "Режим удаления должен быть after или now.")
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if row is None:
        raise ApiError(404, "track_not_found", "Трек не найден.")
    _cancel_pending_for_tracks(conn, {track_id})
    playlists_count = playlists_with_track(conn, track_id)
    was_current = is_track_current(conn, track_id)
    conn.execute(
        "UPDATE tracks SET pending_delete = 1, updated_at = ? WHERE id = ?",
        (to_iso(now), track_id),
    )
    if conn.execute(
        "DELETE FROM queue_positions WHERE track_id = ? AND state IN ('pending', 'prepared')",
        (track_id,),
    ).rowcount:
        from . import queue_engine

        queue_engine.bump_queue_revision(conn)
    db.log_event(
        conn,
        "info",
        "media",
        "Трек исключён из медиатеки",
        {"track_id": track_id, "mode": mode, "current": was_current},
        at=to_iso(now),
    )
    conn.commit()
    # The physical purge happens only after the rebuilt queue is confirmed by
    # the engine (or immediately when the station is stopped).
    return {
        "deleted": False,
        "pending": True,
        "playlists_count": playlists_count,
        "mode": mode,
        "current": was_current,
    }


def bulk_delete(
    conn: sqlite3.Connection,
    settings: Settings,
    track_ids: list[str],
    now: datetime,
) -> dict:
    marked: list[str] = []
    not_found: list[str] = []
    current: list[str] = []
    _cancel_pending_for_tracks(conn, {str(raw or "") for raw in track_ids})
    for raw in track_ids:
        track_id = str(raw or "")
        row = conn.execute("SELECT id FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if row is None:
            not_found.append(track_id)
            continue
        was_current = is_track_current(conn, track_id)
        conn.execute(
            "UPDATE tracks SET pending_delete = 1, updated_at = ? WHERE id = ?",
            (to_iso(now), track_id),
        )
        if conn.execute(
            "DELETE FROM queue_positions WHERE track_id = ? "
            "AND state IN ('pending', 'prepared')",
            (track_id,),
        ).rowcount:
            from . import queue_engine

            queue_engine.bump_queue_revision(conn)
        marked.append(track_id)
        if was_current:
            current.append(track_id)
    db.log_event(
        conn,
        "info",
        "media",
        "Медиатека: удаление выбранных треков",
        {"marked": marked, "current": current},
        at=to_iso(now),
    )
    conn.commit()
    return {
        "marked": marked,
        "deleted": [],
        "purged": [],
        "pending": [],
        "not_found": not_found,
        "current": current,
    }


def delete_pending_after_change(conn: sqlite3.Connection, settings: Settings) -> list[str]:
    """Purge tracks whose removal was deferred until the engine moved on."""

    rows = conn.execute("SELECT id FROM tracks WHERE pending_delete = 1").fetchall()
    if not rows:
        return []
    if queue_sync_unresolved(conn):
        # The engine may still hold the old queue; keep every file until a
        # successful sync confirms that the positions are gone.
        return []
    removed = []
    for row in rows:
        track_id = str(row["id"])
        if is_track_current(conn, track_id):
            continue
        if track_has_future_positions(conn, track_id):
            continue
        if purge_track(conn, settings, track_id):
            removed.append(track_id)
    return removed


def _validated_track_updates(fields: dict) -> dict:
    allowed = {"title", "artist", "album", "genre", "year"}
    updates: dict = {}
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "year":
            if value in (None, ""):
                updates["year"] = None
            else:
                try:
                    year = int(value)
                except (TypeError, ValueError):
                    raise ApiError(
                        422, "invalid_field", "Год должен быть числом.", {"year": "Некорректный год."}
                    )
                if not (YEAR_MIN <= year <= YEAR_MAX):
                    raise ApiError(
                        422,
                        "invalid_field",
                        f"Год должен быть между {YEAR_MIN} и {YEAR_MAX}.",
                        {"year": "Некорректный год."},
                    )
                updates["year"] = year
        elif key == "title":
            title = str(value or "").strip()
            if not title:
                raise ApiError(
                    422, "invalid_field", "Название не может быть пустым.", {"title": "Укажите название."}
                )
            updates["title"] = title[:300]
        else:
            text = str(value or "").strip()
            updates[key] = text[:300] if text else None
    return updates


def update_metadata(
    conn: sqlite3.Connection,
    track_id: str,
    fields: dict,
    now: datetime,
) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if row is None:
        raise ApiError(404, "track_not_found", "Трек не найден.")
    updates = _validated_track_updates(fields)
    if not updates:
        return row
    updates["updated_at"] = to_iso(now)
    assignments = ", ".join(f"{key} = ?" for key in updates)
    conn.execute(
        f"UPDATE tracks SET {assignments} WHERE id = ?",
        (*updates.values(), track_id),
    )
    conn.commit()
    return conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()


def update_track_atomic(
    conn: sqlite3.Connection,
    settings: Settings,
    track_id: str,
    fields: dict,
    cover_stream: BinaryIO | None,
    cover_action: str,
    now: datetime,
) -> sqlite3.Row:
    """Apply metadata and cover in one validated, all-or-nothing operation."""

    if cover_action not in ("keep", "replace", "revert"):
        raise ApiError(422, "invalid_cover_action", "cover_action должен быть keep, replace или revert.")
    if cover_action == "replace" and cover_stream is None:
        raise ApiError(422, "cover_required", "Для замены обложки передайте файл.")
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if row is None:
        raise ApiError(404, "track_not_found", "Трек не найден.")
    updates = _validated_track_updates(fields)

    old_custom = row["custom_cover_path"]
    temp_path: Path | None = None
    new_relative: str | None = None
    new_final: Path | None = None
    if cover_action == "replace":
        temp_path, new_relative = prepare_custom_cover(settings, track_id, cover_stream)
        new_final = settings.cover_dir / new_relative
        try:
            os.replace(temp_path, new_final)
        except BaseException:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        temp_path = None
        updates["custom_cover_path"] = new_relative
    elif cover_action == "revert":
        updates["custom_cover_path"] = None

    updates["updated_at"] = to_iso(now)
    assignments = ", ".join(f"{key} = ?" for key in updates)
    started = not conn.in_transaction
    try:
        if started:
            conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"UPDATE tracks SET {assignments} WHERE id = ?",
            (*updates.values(), track_id),
        )
        if started:
            conn.commit()
    except BaseException:
        if started:
            conn.rollback()
        if new_final is not None and old_custom != new_relative:
            try:
                new_final.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
    if cover_action == "replace" and old_custom and old_custom != new_relative:
        remove_custom_cover(settings, old_custom)
    elif cover_action == "revert" and old_custom:
        remove_custom_cover(settings, old_custom)
    return conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
