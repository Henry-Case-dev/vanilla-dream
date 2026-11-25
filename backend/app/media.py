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


def save_custom_cover(settings: Settings, track_id: str, stream: BinaryIO) -> str:
    """Validate, crop and store a custom square 600x600 JPEG cover."""

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
    digest = hashlib.sha256(data).hexdigest()[:8]
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(track_id))[:64] or "track"
    custom_dir = settings.cover_dir / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)
    destination = custom_dir / f"{safe_id}-{digest}.jpg"
    fd, temp_name = tempfile.mkstemp(prefix=".cover-", suffix=".jpg", dir=str(custom_dir))
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
    return f"custom/{destination.name}"


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


def purge_track(conn: sqlite3.Connection, settings: Settings, track_id: str) -> None:
    """Delete files and every DB reference for a track (caller owns the transaction)."""

    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if row is None:
        return
    path = Path(str(row["path"] or ""))
    if not path.is_absolute():
        path = settings.music_dir / path
    if is_relative_to(path, settings.music_dir):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("failed to delete audio file %s", path, exc_info=True)
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
    positions = conn.execute(
        "SELECT id FROM queue_positions WHERE track_id = ?", (track_id,)
    ).fetchall()
    for position in positions:
        remove_queue_link(settings, str(position["id"]))
    conn.execute("DELETE FROM playlist_tracks WHERE track_id = ?", (track_id,))
    conn.execute("DELETE FROM queue_positions WHERE track_id = ?", (track_id,))
    conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))


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


def delete_track(
    conn: sqlite3.Connection, settings: Settings, track_id: str, now: datetime
) -> dict:
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if row is None:
        raise ApiError(404, "track_not_found", "Трек не найден.")
    playlists_count = playlists_with_track(conn, track_id)
    if is_track_current(conn, track_id):
        conn.execute(
            "UPDATE tracks SET pending_delete = 1, updated_at = ? WHERE id = ?",
            (to_iso(now), track_id),
        )
        db.log_event(
            conn, "info", "media", "Трек играет, удаление отложено", {"track_id": track_id}
        )
        conn.commit()
        return {"deleted": False, "pending": True, "playlists_count": playlists_count}
    purge_track(conn, settings, track_id)
    db.log_event(conn, "info", "media", "Трек удалён", {"track_id": track_id}, at=to_iso(now))
    conn.commit()
    return {"deleted": True, "pending": False, "playlists_count": playlists_count}


def delete_pending_after_change(conn: sqlite3.Connection, settings: Settings) -> list[str]:
    """Purge tracks whose removal was deferred until the engine moved on."""

    rows = conn.execute("SELECT id FROM tracks WHERE pending_delete = 1").fetchall()
    if not rows:
        return []
    removed = []
    for row in rows:
        track_id = str(row["id"])
        if is_track_current(conn, track_id):
            continue
        purge_track(conn, settings, track_id)
        removed.append(track_id)
    return removed


def update_metadata(
    conn: sqlite3.Connection,
    track_id: str,
    fields: dict,
    now: datetime,
) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if row is None:
        raise ApiError(404, "track_not_found", "Трек не найден.")
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
