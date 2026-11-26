"""Two-phase media import: streaming ingest, duplicate detection, decisions.

The incoming file is always written to ``music/.pending`` first. A unique file is
promoted to ``music/<track_id><ext>`` immediately; a file that matches an
existing track waits there until the owner saves or skips it. Pending files are
never visible to the library, playlists, queue or public endpoints.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Iterator

from . import db, media
from .config import Settings
from .models import track_payload
from .util import ApiError, is_relative_to, parse_iso, to_iso

BATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
KIND_ORDER = {"content": 0, "tags": 1, "possible": 2}
MAX_OTHER_MATCHES = 10
LOGGER = logging.getLogger("vdradio.uploads")


def normalize_text(value) -> str:
    """Comparison-only normalisation: NFKC, collapsed spaces, case-folded."""

    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text.casefold()


@contextmanager
def _write_txn(conn: sqlite3.Connection) -> Iterator[None]:
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        if started:
            conn.commit()
    except BaseException:
        if started:
            conn.rollback()
        raise


def _json_loads(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _batch_row(conn: sqlite3.Connection, batch_id: str):
    return conn.execute("SELECT * FROM upload_batches WHERE id = ?", (batch_id,)).fetchone()


def _ensure_batch(conn: sqlite3.Connection, batch_id: str | None, now: datetime) -> str:
    if batch_id:
        if not BATCH_ID_RE.match(batch_id):
            raise ApiError(422, "invalid_batch_id", "Некорректный идентификатор загрузки.")
        if _batch_row(conn, batch_id) is None:
            stamp = to_iso(now)
            conn.execute(
                "INSERT INTO upload_batches(id, created_at, updated_at, status, total, counters) "
                "VALUES(?, ?, ?, 'pending', 0, '{}')",
                (batch_id, stamp, stamp),
            )
        return batch_id
    batch_id = uuid.uuid4().hex
    stamp = to_iso(now)
    conn.execute(
        "INSERT INTO upload_batches(id, created_at, updated_at, status, total, counters) "
        "VALUES(?, ?, ?, 'pending', 0, '{}')",
        (batch_id, stamp, stamp),
    )
    return batch_id


def _refresh_batch(
    conn: sqlite3.Connection, batch_id: str, now: datetime, status: str | None = None
) -> None:
    rows = conn.execute(
        "SELECT state FROM pending_uploads WHERE batch_id = ?", (batch_id,)
    ).fetchall()
    counts = {"added": 0, "replaced": 0, "pending": 0, "skipped": 0, "errors": 0}
    for row in rows:
        state = str(row["state"] or "")
        if state in ("added", "saved"):
            counts["added"] += 1
        elif state == "replaced":
            counts["replaced"] += 1
        elif state == "pending":
            counts["pending"] += 1
        elif state == "skipped":
            counts["skipped"] += 1
        else:
            counts["errors"] += 1
    batch = _batch_row(conn, batch_id)
    resolved_status = status
    if resolved_status is None:
        if counts["pending"]:
            resolved_status = "pending"
        elif batch is not None and batch["status"] == "cancelled":
            resolved_status = "cancelled"
        else:
            resolved_status = "done"
    conn.execute(
        "UPDATE upload_batches SET total = ?, counters = ?, status = ?, updated_at = ? "
        "WHERE id = ?",
        (
            len(rows),
            json.dumps(counts, ensure_ascii=False),
            resolved_status,
            to_iso(now),
            batch_id,
        ),
    )


def _record_error(
    conn: sqlite3.Connection,
    batch_id: str | None,
    filename: str | None,
    ext: str | None,
    reason: str,
    message: str,
    size: int,
    now: datetime,
) -> None:
    if not batch_id:
        return
    item_id = uuid.uuid4().hex
    stamp = to_iso(now)
    conn.execute(
        "INSERT INTO pending_uploads(id, batch_id, filename, temp_path, ext, sha256, size, "
        "metadata, cover_temp, reason, match_track_id, matches, state, decision, created_at, "
        "updated_at) VALUES(?, ?, ?, NULL, ?, NULL, ?, ?, NULL, ?, NULL, '[]', 'error', NULL, ?, ?)",
        (
            item_id,
            batch_id,
            filename,
            ext,
            size,
            json.dumps({"error": message, "filename": filename}, ensure_ascii=False),
            reason,
            stamp,
            stamp,
        ),
    )
    _refresh_batch(conn, batch_id, now)


def _metadata_payload(
    base_name: str, ext: str, size: int, meta: dict
) -> dict:
    tags = meta["tags"]
    title = tags.get("title") or Path(base_name).stem.strip() or "Без названия"
    return {
        "filename": base_name,
        "title": title,
        "artist": tags.get("artist"),
        "album": tags.get("album"),
        "genre": tags.get("genre"),
        "year": tags.get("year"),
        "duration": meta.get("duration"),
        "bitrate": meta.get("bitrate"),
        "size": size,
        "ext": ext,
        "source_title": tags.get("title"),
        "source_artist": tags.get("artist"),
        "source_album": tags.get("album"),
        "source_genre": tags.get("genre"),
        "title_from_filename": not bool(tags.get("title")),
    }


def _match_payload(conn: sqlite3.Connection, settings: Settings, matches: list) -> list[dict]:
    items = []
    for match in matches:
        track_id = match.get("track_id")
        row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if row is None:
            continue
        items.append(
            {"track_id": track_id, "kind": match.get("kind"), "track": track_payload(row, settings)}
        )
    return items


def _match_snapshot(conn: sqlite3.Connection, matches: list) -> list[dict]:
    """Freeze each candidate's hash and path at detection time.

    A later replace decision compares the live track against this snapshot and
    refuses with ``stale_match`` when the target changed in between.
    """

    snapshot = []
    for item in matches:
        track_id = str(item.get("track_id") or "")
        if not track_id:
            continue
        row = conn.execute(
            "SELECT id, sha256, path FROM tracks WHERE id = ?", (track_id,)
        ).fetchone()
        entry = {"track_id": track_id, "kind": item.get("kind")}
        if row is not None:
            entry["sha256"] = row["sha256"]
            entry["path"] = row["path"]
        snapshot.append(entry)
    return snapshot


def _replace_candidates(conn: sqlite3.Connection, row) -> list[dict]:
    """Return the stored match candidates that still exist in the library."""

    seen: set[str] = set()
    items = []
    for match in _json_loads(row["matches"], []):
        track_id = str(match.get("track_id") or "")
        if not track_id or track_id in seen:
            continue
        seen.add(track_id)
        track = conn.execute(
            "SELECT * FROM tracks WHERE id = ? AND pending_delete = 0", (track_id,)
        ).fetchone()
        if track is None:
            continue
        items.append({"match": match, "row": track})
    return items


def _candidate_summary(track, settings: Settings) -> dict:
    payload = track_payload(track, settings)
    return {
        "id": payload["id"],
        "title": payload["title"],
        "artist": payload["artist"],
        "album": payload["album"],
        "duration": payload["duration"],
        "bitrate": payload["bitrate"],
        "size": payload["size"],
        "cover_url": payload["cover_url"],
        "is_duplicate": payload["is_duplicate"],
    }


def pending_payload(conn: sqlite3.Connection, settings: Settings, row) -> dict:
    matches = _json_loads(row["matches"], [])
    metadata = _json_loads(row["metadata"], {})
    if row["state"] == "pending" and row["cover_temp"]:
        metadata["cover_url"] = f"/api/admin/uploads/{row['id']}/cover"
    candidates = _replace_candidates(conn, row)
    summaries = [_candidate_summary(item["row"], settings) for item in candidates]
    incoming_sha = str(row["sha256"] or "")
    identical = bool(candidates) and all(
        str(item["row"]["sha256"] or "") == incoming_sha for item in candidates
    )
    can_replace = row["state"] == "pending" and bool(candidates) and not identical
    return {
        "id": row["id"],
        "filename": row["filename"],
        "size": row["size"],
        "state": row["state"],
        "reason": row["reason"],
        "metadata": metadata,
        "matches": _match_payload(conn, settings, matches),
        "match_track_id": row["match_track_id"],
        "decision": row["decision"],
        "decision_target": row["decision_target"],
        "error": metadata.get("error"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "can_replace": can_replace,
        "identical": identical,
        "replace_target": summaries[0] if len(summaries) == 1 else None,
        "replace_candidates": summaries,
    }


def batch_payload(conn: sqlite3.Connection, settings: Settings, batch_id: str) -> dict | None:
    batch = _batch_row(conn, batch_id)
    if batch is None:
        return None
    rows = conn.execute(
        "SELECT * FROM pending_uploads WHERE batch_id = ? ORDER BY created_at ASC, rowid ASC",
        (batch_id,),
    ).fetchall()
    counts = {"added": 0, "replaced": 0, "pending": 0, "skipped": 0, "errors": 0}
    items = []
    for row in rows:
        state = str(row["state"] or "")
        if state in ("added", "saved"):
            counts["added"] += 1
        elif state == "replaced":
            counts["replaced"] += 1
        elif state == "pending":
            counts["pending"] += 1
        elif state == "skipped":
            counts["skipped"] += 1
        else:
            counts["errors"] += 1
        items.append(pending_payload(conn, settings, row))
    if counts["pending"]:
        status = "pending"
    elif batch["status"] == "cancelled":
        status = "cancelled"
    else:
        status = "done"
    return {
        "id": batch_id,
        "status": status,
        "total": len(rows),
        "counts": counts,
        "items": items,
    }


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def find_duplicate_matches(
    conn: sqlite3.Connection, tags: dict, sha256: str
) -> tuple[str | None, str | None, list[dict]]:
    """Return ``(kind, best_track_id, ordered_matches)``.

    ``content`` matches always win. Tag matching requires title and artist tags
    on both sides; an incoming title derived from the file name never matches.
    """

    content = conn.execute(
        "SELECT id, created_at, rowid AS row_order FROM tracks "
        "WHERE pending_delete = 0 AND sha256 = ? "
        "ORDER BY created_at DESC, rowid DESC",
        (sha256,),
    ).fetchall()
    if content:
        matches = [{"track_id": row["id"], "kind": "content"} for row in content]
        return "content", str(content[0]["id"]), matches[: MAX_OTHER_MATCHES + 1]

    title = normalize_text(tags.get("title"))
    artist = normalize_text(tags.get("artist"))
    if not title or not artist:
        # No title/artist tags: never match on album, artist or filename.
        return None, None, []
    album = normalize_text(tags.get("album"))

    candidates = conn.execute(
        "SELECT id, title, artist, album, source_title, source_artist, source_album, "
        "created_at, rowid AS row_order FROM tracks "
        "WHERE pending_delete = 0 AND source_title IS NOT NULL AND source_title != '' "
        "AND source_artist IS NOT NULL AND source_artist != ''"
    ).fetchall()
    found: list[dict] = []
    for row in candidates:
        pairs = (
            (row["title"], row["artist"], row["album"]),
            (row["source_title"], row["source_artist"], row["source_album"]),
        )
        kind = None
        for pair_title, pair_artist, pair_album in pairs:
            if not pair_title or not pair_artist:
                continue
            if normalize_text(pair_title) != title or normalize_text(pair_artist) != artist:
                continue
            pair_album_norm = normalize_text(pair_album)
            if album and pair_album_norm:
                if pair_album_norm == album:
                    kind = "tags"
                    break
                kind = kind or "possible"
            elif album or pair_album_norm:
                # Album is known on one side only.
                kind = kind or "possible"
            else:
                # Album is unknown on both sides: still the same tag set.
                kind = "tags"
                break
        if kind:
            found.append(
                {
                    "track_id": str(row["id"]),
                    "kind": kind,
                    "created_at": row["created_at"] or "",
                    "row_order": int(row["row_order"] or 0),
                }
            )
    if not found:
        return None, None, []
    found.sort(key=lambda item: (item["created_at"], item["row_order"]), reverse=True)
    found.sort(key=lambda item: KIND_ORDER[item["kind"]])
    best_kind = found[0]["kind"]
    same_kind = [item for item in found if item["kind"] == best_kind]
    return best_kind, same_kind[0]["track_id"], same_kind[: MAX_OTHER_MATCHES + 1]


# ---------------------------------------------------------------------------
# Track creation
# ---------------------------------------------------------------------------


def _stage_track_file(
    settings: Settings,
    source_path: Path,
    ext: str,
    cover_bytes: bytes | None,
) -> dict:
    """Copy the file (and cover) outside any DB transaction."""

    track_id = ""
    target = None
    for _attempt in range(5):
        candidate = uuid.uuid4().hex
        target = settings.music_dir / f"{candidate}{ext}"
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as destination, open(source_path, "rb") as source:
                shutil.copyfileobj(source, destination, media.CHUNK_SIZE)
                destination.flush()
                os.fsync(destination.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        track_id = candidate
        break
    if not track_id or target is None:
        raise ApiError(
            500,
            "storage_conflict",
            "Не удалось выделить уникальный путь для файла. Повторите загрузку.",
        )
    cover_name = None
    try:
        if cover_bytes:
            cover_name = media.write_cover(settings, track_id, cover_bytes)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {
        "track_id": track_id,
        "target": target,
        "relative": str(target.relative_to(settings.music_dir)),
        "cover_name": cover_name,
    }


def _discard_staged_file(settings: Settings, staged: dict | None) -> None:
    if not staged:
        return
    try:
        staged["target"].unlink(missing_ok=True)
    except OSError:
        pass
    cover_name = staged.get("cover_name")
    if cover_name:
        try:
            media.cover_path_for(settings, staged["track_id"]).unlink(missing_ok=True)
        except OSError:
            pass


def _insert_track(
    conn: sqlite3.Connection,
    staged: dict,
    base_name: str,
    ext: str,
    sha256: str,
    size: int,
    meta: dict,
    now: datetime,
    duplicate_kind: str | None = None,
    duplicate_of: str | None = None,
) -> sqlite3.Row:
    """Insert a previously staged file and its cover reference."""

    track_id = staged["track_id"]
    cover_name = staged.get("cover_name")
    tags = meta["tags"]
    title = tags.get("title") or Path(base_name).stem.strip() or "Без названия"
    stamp = to_iso(now)
    conn.execute(
        "INSERT INTO tracks(id, sha256, path, ext, title, artist, album, genre, year, "
        "duration, bitrate, size, cover_path, custom_cover_path, source_title, "
        "source_artist, source_album, source_genre, source_filename, duplicate_of, "
        "duplicate_kind, duplicate_confirmed_at, play_count, pending_delete, created_at, "
        "updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, "
        "?, ?, 0, 0, ?, ?)",
        (
            track_id,
            sha256,
            staged["relative"],
            ext,
            title,
            tags.get("artist"),
            tags.get("album"),
            tags.get("genre"),
            tags.get("year"),
            meta.get("duration"),
            meta.get("bitrate"),
            size,
            cover_name,
            tags.get("title"),
            tags.get("artist"),
            tags.get("album"),
            tags.get("genre"),
            base_name,
            duplicate_of,
            duplicate_kind,
            to_iso(now) if duplicate_of else None,
            stamp,
            stamp,
        ),
    )
    return conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def _remove_pending_files(settings: Settings, row) -> None:
    for key in ("temp_path", "cover_temp"):
        value = row[key] if key in row.keys() else None
        if not value:
            continue
        candidate = Path(str(value))
        if is_relative_to(candidate, settings.pending_dir):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass


def receive_upload(
    conn: sqlite3.Connection,
    settings: Settings,
    stream: BinaryIO,
    filename: str | None,
    now: datetime,
    batch_id: str | None = None,
) -> dict:
    settings.ensure_directories()
    settings.pending_dir.mkdir(parents=True, exist_ok=True)
    resolved_batch = _ensure_batch(conn, batch_id, now)
    base_name = None
    ext = None
    temp_path: Path | None = None
    cover_temp: Path | None = None
    staged: dict | None = None
    stable_path: Path | None = None
    track = None
    size = 0
    try:
        base_name, ext = media._normalise_filename(filename)
        max_upload_mb = db.get_int_setting(conn, "max_upload_mb", 300)
        max_bytes = max_upload_mb * 1024 * 1024
        usage = media.disk_state(settings)
        if settings.min_free_bytes and usage["free_bytes"] < settings.min_free_bytes:
            raise ApiError(
                507,
                "not_enough_space",
                "Недостаточно свободного места на диске для загрузки файла.",
            )

        digest = hashlib.sha256()
        fd, temp_name = tempfile.mkstemp(
            prefix="incoming-", suffix=ext, dir=str(settings.pending_dir)
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "wb") as handle:
            while True:
                chunk = stream.read(media.CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ApiError(
                        413,
                        "file_too_large",
                        f"Файл превышает допустимый размер {max_upload_mb} МБ.",
                    )
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if size == 0:
            raise ApiError(422, "empty_file", "Файл пустой.")

        media._check_upload_limits(conn, settings, size)
        sha256 = digest.hexdigest()
        meta = media.probe_audio(temp_path, ext)
        cover_bytes = media.extract_cover_bytes(meta["audio"], ext)
        if cover_bytes:
            cover_temp = Path(temp_name + ".cover")
            cover_temp.write_bytes(cover_bytes)

        metadata = _metadata_payload(base_name, ext, size, meta)
        stamp = to_iso(now)
        conn.commit()

        # File copy, hashing and cover encoding stay outside any transaction.
        staged = _stage_track_file(settings, temp_path, ext, cover_bytes)
        pending_id = uuid.uuid4().hex
        stable_path = settings.pending_dir / f"{pending_id}{ext}"
        os.replace(temp_path, stable_path)
        temp_path = stable_path
        if cover_temp is not None:
            stable_cover = settings.pending_dir / f"{pending_id}.cover"
            os.replace(cover_temp, stable_cover)
            cover_temp = stable_cover

        track = None
        pending_row = None
        final_kind = None
        discard_staged = False
        for attempt, delay in enumerate((0.0, 0.05, 0.15, 0.4)):
            if delay:
                time.sleep(delay)
            try:
                with _write_txn(conn):
                    # Final availability check and insert are serialized by
                    # BEGIN IMMEDIATE, so parallel identical uploads cannot both
                    # create a stored track. No file I/O happens here.
                    final_kind, best_id, matches = find_duplicate_matches(
                        conn, meta["tags"], sha256
                    )
                    if final_kind is None:
                        track = _insert_track(
                            conn,
                            staged,
                            base_name,
                            ext,
                            sha256,
                            size,
                            meta,
                            now,
                        )
                        conn.execute(
                            "INSERT INTO pending_uploads(id, batch_id, filename, temp_path, ext, "
                            "sha256, size, metadata, cover_temp, reason, match_track_id, matches, "
                            "state, decision, created_at, updated_at) VALUES(?, ?, ?, NULL, ?, ?, "
                            "?, ?, NULL, NULL, NULL, '[]', 'added', 'save', ?, ?)",
                            (
                                track["id"],
                                resolved_batch,
                                base_name,
                                ext,
                                sha256,
                                size,
                                json.dumps(metadata, ensure_ascii=False),
                                stamp,
                                stamp,
                            ),
                        )
                        _refresh_batch(conn, resolved_batch, now)
                        db.log_event(
                            conn,
                            "info",
                            "media",
                            "???? ????????",
                            {"track_id": track["id"]},
                            at=stamp,
                        )
                        conn.commit()
                    else:
                        discard_staged = True
                        match_rows = _match_snapshot(conn, matches)
                        best_row = conn.execute(
                            "SELECT sha256 FROM tracks WHERE id = ?", (best_id,)
                        ).fetchone()
                        conn.execute(
                            "INSERT INTO pending_uploads(id, batch_id, filename, temp_path, ext, "
                            "sha256, size, metadata, cover_temp, reason, match_track_id, matches, "
                            "state, decision, created_at, updated_at, match_sha, decision_target) "
                            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?, ?, ?)",
                            (
                                pending_id,
                                resolved_batch,
                                base_name,
                                str(stable_path),
                                ext,
                                sha256,
                                size,
                                json.dumps(metadata, ensure_ascii=False),
                                str(cover_temp) if cover_temp else None,
                                final_kind,
                                best_id,
                                json.dumps(match_rows, ensure_ascii=False),
                                stamp,
                                stamp,
                                str(best_row["sha256"]) if best_row is not None else None,
                                best_id,
                            ),
                        )
                        _refresh_batch(conn, resolved_batch, now)
                        conn.commit()
                break
            except sqlite3.OperationalError as exc:
                track = None
                discard_staged = False
                if (
                    ("locked" in str(exc).lower() or "busy" in str(exc).lower())
                    and attempt < 3
                ):
                    continue
                raise
        if discard_staged:
            _discard_staged_file(settings, staged)
            staged = None
        if track is not None:
            for leftover in (stable_path, cover_temp):
                if leftover is not None:
                    try:
                        leftover.unlink(missing_ok=True)
                    except OSError:
                        pass
            temp_path = None
            cover_temp = None
            item = conn.execute(
                "SELECT * FROM pending_uploads WHERE id = ?", (track["id"],)
            ).fetchone()
            return {
                "status": "added",
                "track": track,
                "upload": pending_payload(conn, settings, item),
                "batch_id": resolved_batch,
            }
        pending_row = conn.execute(
            "SELECT * FROM pending_uploads WHERE id = ?", (pending_id,)
        ).fetchone()
        return {
            "status": "pending",
            "pending": pending_payload(conn, settings, pending_row),
            "batch_id": resolved_batch,
        }
    except ApiError as exc:
        if track is None:
            _discard_staged_file(settings, staged)
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        if cover_temp is not None:
            try:
                cover_temp.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            _record_error(
                conn,
                resolved_batch,
                base_name or (filename or "") or "Файл",
                ext,
                exc.code,
                exc.message,
                size,
                now,
            )
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
        raise
    except OSError as exc:
        if track is None:
            _discard_staged_file(settings, staged)
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        if cover_temp is not None:
            try:
                cover_temp.unlink(missing_ok=True)
            except OSError:
                pass
        message = "Недостаточно свободного места на диске для загрузки файла."
        try:
            _record_error(
                conn,
                resolved_batch,
                base_name or (filename or "") or "Файл",
                ext,
                "not_enough_space",
                message,
                size,
                now,
            )
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
        raise ApiError(507, "not_enough_space", message) from exc
    except BaseException:
        if track is None:
            _discard_staged_file(settings, staged)
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        if cover_temp is not None:
            try:
                cover_temp.unlink(missing_ok=True)
            except OSError:
                pass
        raise


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def _pending_row(conn: sqlite3.Connection, pending_id: str):
    return conn.execute(
        "SELECT * FROM pending_uploads WHERE id = ?", (pending_id,)
    ).fetchone()


def _terminal_result(
    conn: sqlite3.Connection, settings: Settings, row, action: str
) -> dict:
    decision = row["decision"]
    if row["state"] == "replaced" and action == "replace":
        stored = _json_loads(row["decision_result"], {}) or {}
        track_id = stored.get("track_id") or row["decision_target"]
        track = None
        if track_id:
            track_row = conn.execute(
                "SELECT * FROM tracks WHERE id = ?", (track_id,)
            ).fetchone()
            if track_row is not None:
                track = track_payload(track_row, settings)
        return {
            "status": "replaced",
            "replaced": True,
            "identical": False,
            "track_id": track_id,
            "track": track,
            "pending": pending_payload(conn, settings, row),
        }
    if decision != action:
        raise ApiError(
            409,
            "already_decided",
            "Решение по этому файлу уже принято. Обновите список загрузок.",
        )
    payload = pending_payload(conn, settings, row)
    if action == "save":
        metadata = _json_loads(row["metadata"], {})
        track_id = metadata.get("saved_track_id") or (
            row["id"] if row["state"] == "added" else None
        )
        track = None
        if track_id:
            track_row = conn.execute(
                "SELECT * FROM tracks WHERE id = ?", (track_id,)
            ).fetchone()
            if track_row is not None:
                track = track_payload(track_row, settings)
        return {"status": "saved", "pending": payload, "track": track}
    return {"status": "skipped", "pending": payload}


def decide(
    conn: sqlite3.Connection,
    settings: Settings,
    pending_id: str,
    action: str,
    now: datetime,
    track_id: str | None = None,
) -> dict:
    if action == "replace":
        return _decide_replace(conn, settings, pending_id, now, track_id)
    for attempt, delay in enumerate((0.0, 0.05, 0.15, 0.4)):
        if delay:
            time.sleep(delay)
        try:
            return _decide_impl(conn, settings, pending_id, action, now)
        except sqlite3.OperationalError as exc:
            if (
                ("locked" in str(exc).lower() or "busy" in str(exc).lower())
                and attempt < 3
            ):
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                continue
            raise
    raise ApiError(503, "upload_busy", "Не удалось применить решение. Повторите попытку.")


def _decide_impl(
    conn: sqlite3.Connection,
    settings: Settings,
    pending_id: str,
    action: str,
    now: datetime,
) -> dict:
    if action not in ("save", "skip"):
        raise ApiError(422, "invalid_action", "??????? ?????? ???? save ??? skip.")

    result: dict | None = None
    error: ApiError | None = None
    stage_info: tuple | None = None
    row = None
    with _write_txn(conn):
        row = _pending_row(conn, pending_id)
        if row is None:
            raise ApiError(404, "upload_not_found", "???????? ?? ???????.")
        if row["state"] != "pending":
            result = _terminal_result(conn, settings, row, action)
        elif action == "skip":
            conn.execute(
                "UPDATE pending_uploads SET state = 'skipped', decision = 'skip', updated_at = ? "
                "WHERE id = ?",
                (to_iso(now), pending_id),
            )
            _refresh_batch(conn, row["batch_id"], now)
            conn.commit()
            _remove_pending_files(settings, row)
            row = _pending_row(conn, pending_id)
            result = {"status": "skipped", "pending": pending_payload(conn, settings, row)}
        else:
            temp_path = Path(str(row["temp_path"] or ""))
            if (
                not row["temp_path"]
                or not is_relative_to(temp_path, settings.pending_dir)
                or not temp_path.exists()
            ):
                metadata = _json_loads(row["metadata"], {})
                metadata["error"] = (
                    "???? ??????? ???????, ?? ??? ??????. ????????? ??? ?????."
                )
                conn.execute(
                    "UPDATE pending_uploads SET state = 'error', reason = 'pending_file_missing', "
                    "metadata = ?, decision = NULL, updated_at = ? WHERE id = ?",
                    (json.dumps(metadata, ensure_ascii=False), to_iso(now), pending_id),
                )
                _refresh_batch(conn, row["batch_id"], now)
                conn.commit()
                error = ApiError(
                    409,
                    "pending_file_missing",
                    "???? ??????? ???????, ?? ??? ??????. ????????? ??? ?????.",
                )
            else:
                metadata = _json_loads(row["metadata"], {})
                tags = {
                    "title": metadata.get("source_title"),
                    "artist": metadata.get("source_artist"),
                    "album": metadata.get("source_album"),
                    "genre": metadata.get("source_genre"),
                    "year": metadata.get("year"),
                }
                cover_value = row["cover_temp"]
                size = int(row["size"] or 0)
                meta = {
                    "tags": tags,
                    "duration": metadata.get("duration"),
                    "bitrate": metadata.get("bitrate"),
                }
                stage_info = (
                    temp_path,
                    str(row["ext"] or ""),
                    cover_value,
                    metadata,
                    tags,
                    meta,
                    size,
                    str(row["sha256"] or ""),
                    str(row["filename"] or ""),
                )

    if error is not None:
        raise error
    if result is not None:
        return result
    assert stage_info is not None

    (
        temp_path,
        ext,
        cover_value,
        metadata,
        tags,
        meta,
        size,
        sha256,
        filename,
    ) = stage_info
    media._check_upload_limits(conn, settings, size)
    cover_bytes = None
    if cover_value:
        cover_path = Path(str(cover_value))
        if is_relative_to(cover_path, settings.pending_dir) and cover_path.exists():
            try:
                cover_bytes = cover_path.read_bytes()
            except OSError:
                cover_bytes = None
    staged = _stage_track_file(settings, temp_path, ext, cover_bytes)
    try:
        with _write_txn(conn):
            row = _pending_row(conn, pending_id)
            if row is None:
                raise ApiError(404, "upload_not_found", "???????? ?? ???????.")
            if row["state"] != "pending":
                result = _terminal_result(conn, settings, row, action)
            else:
                track = _insert_track(
                    conn,
                    staged,
                    filename,
                    ext,
                    sha256,
                    size,
                    meta,
                    now,
                    duplicate_kind=row["reason"],
                    duplicate_of=row["match_track_id"],
                )
                try:
                    metadata["saved_track_id"] = track["id"]
                    conn.execute(
                        "UPDATE pending_uploads SET state = 'saved', decision = 'save', "
                        "metadata = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(metadata, ensure_ascii=False), to_iso(now), pending_id),
                    )
                    _refresh_batch(conn, row["batch_id"], now)
                    db.log_event(
                        conn,
                        "info",
                        "media",
                        "????????? ????? ?????",
                        {"track_id": track["id"], "duplicate_of": row["match_track_id"]},
                        at=to_iso(now),
                    )
                    conn.commit()
                except BaseException:
                    _discard_staged_file(settings, staged)
                    raise
        if result is not None:
            _discard_staged_file(settings, staged)
            return result
        _remove_pending_files(settings, row)
        updated_row = _pending_row(conn, pending_id)
        return {
            "status": "saved",
            "track": track_payload(track, settings),
            "pending": pending_payload(conn, settings, updated_row),
        }
    except BaseException:
        _discard_staged_file(settings, staged)
        raise


# ---------------------------------------------------------------------------
# Replace an existing track (MEDIA-03)
# ---------------------------------------------------------------------------

DISPLAY_SOURCE = {
    "title": "source_title",
    "artist": "source_artist",
    "album": "source_album",
    "genre": "source_genre",
}


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _same_value(first, second) -> bool:
    if _blank(first) and _blank(second):
        return True
    return str(first).strip() == str(second).strip()


def _display_is_default(track, field: str) -> bool:
    """True when the display value is still the original extracted/derived one.

    A user edit (display differs from the old source value) is never replaced.
    A title derived from the file name counts as default too.
    """

    display = track[field]
    source = track[DISPLAY_SOURCE[field]]
    if _same_value(display, source):
        return True
    if field == "title" and _blank(source):
        stem = Path(str(track["source_filename"] or "")).stem.strip()
        if stem and _same_value(display, stem):
            return True
        if _same_value(display, "Без названия"):
            return True
    return False


def _replacement_metadata(track, metadata: dict) -> dict:
    """Display fields updated only where the owner had not overridden them."""

    tags = {
        "title": metadata.get("source_title"),
        "artist": metadata.get("source_artist"),
        "album": metadata.get("source_album"),
        "genre": metadata.get("source_genre"),
        "year": metadata.get("year"),
    }
    updates: dict = {}
    for field in ("title", "artist", "album", "genre"):
        if not _display_is_default(track, field):
            continue
        value = tags.get(field)
        if field == "title":
            if not _blank(value):
                updates["title"] = str(value).strip()[:300]
            continue
        if not _blank(value):
            updates[field] = str(value).strip()[:300]
    # There is no historical ``source_year`` column, so a non-empty display
    # year is always treated as a possible user edit: only a blank year is
    # filled from the new file.
    if _blank(track["year"]) and tags.get("year") is not None:
        try:
            updates["year"] = int(tags["year"])
        except (TypeError, ValueError):
            pass
    return updates


def _stale_match(
    conn: sqlite3.Connection, settings: Settings, row, track_id: str | None = None
) -> ApiError:
    candidates = [
        _candidate_summary(item["row"], settings) for item in _replace_candidates(conn, row)
    ]
    return ApiError(
        409,
        "stale_match",
        "Совпадение изменилось. Обновите список и подтвердите замену.",
        extra={"track_id": track_id, "candidates": candidates},
    )


def _resolve_replace_target(
    conn: sqlite3.Connection,
    settings: Settings,
    row,
    track_id: str | None,
) -> dict:
    candidates = _replace_candidates(conn, row)
    if not candidates:
        raise _stale_match(conn, settings, row, track_id)
    if track_id:
        chosen = next(
            (item for item in candidates if str(item["row"]["id"]) == str(track_id)), None
        )
        if chosen is None:
            raise _stale_match(conn, settings, row, track_id)
        return chosen
    if len(candidates) > 1:
        raise ApiError(
            422,
            "ambiguous_target",
            "Найдено несколько совпадений. Выберите трек для замены.",
            extra={
                "track_id": None,
                "candidates": [
                    _candidate_summary(item["row"], settings) for item in candidates
                ],
            },
        )
    return candidates[0]


def _target_matches_snapshot(target, entry: dict | None, row) -> bool:
    """The live target must still equal the values frozen at detection."""

    if target is None or int(target["pending_delete"] or 0):
        return False
    expected_sha = str((entry or {}).get("sha256") or "")
    if not expected_sha and entry is not None:
        if str(entry.get("track_id")) == str(row["match_track_id"]):
            expected_sha = str(row["match_sha"] or "")
    if not expected_sha:
        # Legacy pending row without a frozen hash: freshness cannot be proven.
        return False
    if str(target["sha256"] or "") != expected_sha:
        return False
    expected_path = str((entry or {}).get("path") or "")
    if expected_path and str(target["path"] or "") != expected_path:
        return False
    return True


def _check_replace_space(settings: Settings, incoming: int) -> None:
    usage = media.disk_state(settings)
    required = int(settings.min_free_bytes or 0) + int(settings.export_reserve_bytes or 0)
    if usage["free_bytes"] - incoming < required:
        raise ApiError(
            507,
            "not_enough_space",
            "Недостаточно свободного места на диске для замены файла.",
        )


def _write_extracted_cover(
    settings: Settings, track_id: str, data: bytes
) -> str | None:
    """Encode the new embedded cover under a fresh unique file name.

    A new name (instead of ``<track_id>.jpg``) means the old extracted cover is
    never overwritten before the database switch commits.
    """

    from PIL import Image

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.thumbnail((media.MAX_COVER_SIDE, media.MAX_COVER_SIDE))
        settings.cover_dir.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(track_id))[:64] or "track"
        destination = settings.cover_dir / f"{safe_id}-{uuid.uuid4().hex[:8]}.jpg"
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=85, optimize=True)
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
        with os.fdopen(fd, "wb") as handle:
            handle.write(buffer.getvalue())
            handle.flush()
            os.fsync(handle.fileno())
        return destination.name
    except Exception:  # noqa: BLE001 - a broken cover must not fail the replace
        LOGGER.warning("failed to encode replacement cover for %s", track_id, exc_info=True)
        return None


def _stage_replacement_file(
    settings: Settings,
    source_path: Path,
    ext: str,
    cover_bytes: bytes | None,
    track_id: str,
) -> dict:
    """Copy the staged upload to a brand new independent path (no DB txn)."""

    settings.music_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(track_id))[:64] or "track"
    target = None
    for _attempt in range(5):
        candidate = settings.music_dir / f"{safe_id}.{uuid.uuid4().hex}{ext}"
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as destination, open(source_path, "rb") as source:
                shutil.copyfileobj(source, destination, media.CHUNK_SIZE)
                destination.flush()
                os.fsync(destination.fileno())
        except BaseException:
            candidate.unlink(missing_ok=True)
            raise
        target = candidate
        break
    if target is None:
        raise ApiError(
            500,
            "storage_conflict",
            "Не удалось выделить путь для замены файла. Повторите попытку.",
        )
    cover_name = _write_extracted_cover(settings, track_id, cover_bytes) if cover_bytes else None
    return {
        "target": target,
        "relative": str(target.relative_to(settings.music_dir)),
        "cover_name": cover_name,
    }


def _discard_replacement(settings: Settings, staged: dict | None) -> None:
    if not staged:
        return
    try:
        staged["target"].unlink(missing_ok=True)
    except OSError:
        pass
    cover_name = staged.get("cover_name")
    if cover_name:
        _remove_extracted_cover(settings, cover_name)


def _remove_extracted_cover(settings: Settings, name: str | None) -> None:
    if not name:
        return
    candidate = settings.cover_dir / str(name)
    if not is_relative_to(candidate, settings.cover_dir):
        return
    if is_relative_to(candidate, settings.cover_dir / "custom"):
        # Custom covers are owned by the editor endpoints, never by replace.
        return
    try:
        candidate.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("failed to delete extracted cover %s", candidate, exc_info=True)


def _switch_track_file(
    conn: sqlite3.Connection,
    settings: Settings,
    pending_id: str,
    target_id: str,
    staged: dict,
    metadata: dict,
    sha256: str,
    size: int,
    now: datetime,
) -> tuple[dict, bool]:
    """One short ``BEGIN IMMEDIATE`` that swaps the row to the new file."""

    used_staged = False
    result: dict | None = None
    with _write_txn(conn):
        row = _pending_row(conn, pending_id)
        if row is None:
            raise ApiError(404, "upload_not_found", "Загрузка не найдена.")
        if row["state"] != "pending":
            result = _terminal_result(conn, settings, row, "replace")
        else:
            target = conn.execute(
                "SELECT * FROM tracks WHERE id = ?", (target_id,)
            ).fetchone()
            entry = next(
                (
                    item
                    for item in _json_loads(row["matches"], [])
                    if str(item.get("track_id")) == str(target_id)
                ),
                None,
            )
            if not _target_matches_snapshot(target, entry, row):
                raise _stale_match(conn, settings, row, target_id)
            stamp = to_iso(now)
            updates = _replacement_metadata(target, metadata)
            updates.update(
                {
                    "path": staged["relative"],
                    "sha256": sha256,
                    "size": size,
                    "duration": metadata.get("duration"),
                    "bitrate": metadata.get("bitrate"),
                    "ext": str(row["ext"] or ""),
                    "cover_path": staged.get("cover_name"),
                    "source_title": metadata.get("source_title"),
                    "source_artist": metadata.get("source_artist"),
                    "source_album": metadata.get("source_album"),
                    "source_genre": metadata.get("source_genre"),
                    "source_filename": row["filename"],
                    "replaced_at": stamp,
                    "updated_at": stamp,
                }
            )
            assignments = ", ".join(f"{key} = ?" for key in updates)
            conn.execute(
                f"UPDATE tracks SET {assignments} WHERE id = ?",
                (*updates.values(), target_id),
            )
            old_path = str(target["path"] or "")
            if old_path and old_path != staged["relative"]:
                conn.execute(
                    "INSERT INTO retained_files(track_id, path, reason, created_at) "
                    "VALUES(?, ?, 'replace', ?)",
                    (target_id, old_path, stamp),
                )
            stored = {
                "status": "replaced",
                "replaced": True,
                "identical": False,
                "track_id": target_id,
            }
            conn.execute(
                "UPDATE pending_uploads SET state = 'replaced', decision = 'replace', "
                "decision_target = ?, decision_result = ?, updated_at = ? "
                "WHERE id = ? AND state = 'pending'",
                (target_id, json.dumps(stored, ensure_ascii=False), stamp, pending_id),
            )
            _refresh_batch(conn, row["batch_id"], now)
            db.log_event(
                conn,
                "info",
                "media",
                "Трек заменён новым аудиофайлом",
                {"track_id": target_id, "old_path": old_path},
                at=stamp,
            )
            conn.commit()
            used_staged = True
            updated = conn.execute(
                "SELECT * FROM tracks WHERE id = ?", (target_id,)
            ).fetchone()
            result = dict(stored)
            result.update(
                {
                    "track": track_payload(updated, settings),
                    "pending": pending_payload(
                        conn, settings, _pending_row(conn, pending_id)
                    ),
                    "_retired_cover": target["cover_path"],
                }
            )
    assert result is not None
    return result, used_staged


def _decide_replace(
    conn: sqlite3.Connection,
    settings: Settings,
    pending_id: str,
    now: datetime,
    track_id: str | None,
) -> dict:
    row = _pending_row(conn, pending_id)
    if row is None:
        raise ApiError(404, "upload_not_found", "Загрузка не найдена.")
    if row["state"] != "pending":
        return _terminal_result(conn, settings, row, "replace")

    temp_path = Path(str(row["temp_path"] or ""))
    if (
        not row["temp_path"]
        or not is_relative_to(temp_path, settings.pending_dir)
        or not temp_path.exists()
    ):
        message = "Временный файл загрузки не найден. Повторите загрузку."
        metadata = _json_loads(row["metadata"], {})
        metadata["error"] = message
        with _write_txn(conn):
            conn.execute(
                "UPDATE pending_uploads SET state = 'error', reason = 'pending_file_missing', "
                "metadata = ?, decision = NULL, updated_at = ? WHERE id = ? AND state = 'pending'",
                (json.dumps(metadata, ensure_ascii=False), to_iso(now), pending_id),
            )
            _refresh_batch(conn, row["batch_id"], now)
        raise ApiError(409, "pending_file_missing", message)

    chosen = _resolve_replace_target(conn, settings, row, track_id)
    target = chosen["row"]
    target_id = str(target["id"])
    incoming_sha = str(row["sha256"] or "")

    if incoming_sha and str(target["sha256"] or "") == incoming_sha:
        target_path = Path(str(target["path"] or ""))
        if not target_path.is_absolute():
            target_path = settings.music_dir / target_path
        if is_relative_to(target_path, settings.music_dir) and target_path.exists():
            return {
                "status": "identical",
                "replaced": False,
                "identical": True,
                "track_id": target_id,
                "track": track_payload(target, settings),
                "pending": pending_payload(conn, settings, row),
            }

    if not _target_matches_snapshot(target, chosen["match"], row):
        raise _stale_match(conn, settings, row, target_id)

    size = int(row["size"] or 0)
    _check_replace_space(settings, size)
    metadata = _json_loads(row["metadata"], {})
    cover_bytes = None
    if row["cover_temp"]:
        cover_path = Path(str(row["cover_temp"]))
        if is_relative_to(cover_path, settings.pending_dir) and cover_path.exists():
            try:
                cover_bytes = cover_path.read_bytes()
            except OSError:
                cover_bytes = None

    staged = None
    try:
        staged = _stage_replacement_file(
            settings, temp_path, str(row["ext"] or ""), cover_bytes, target_id
        )
    except OSError as exc:
        raise ApiError(
            507,
            "not_enough_space",
            "Не удалось подготовить файл замены. Проверьте свободное место и повторите.",
        ) from exc
    try:
        result = None
        used_staged = False
        for attempt, delay in enumerate((0.0, 0.05, 0.15, 0.4)):
            if delay:
                time.sleep(delay)
            try:
                result, used_staged = _switch_track_file(
                    conn,
                    settings,
                    pending_id,
                    target_id,
                    staged,
                    metadata,
                    incoming_sha,
                    size,
                    now,
                )
                break
            except sqlite3.OperationalError as exc:
                if (
                    ("locked" in str(exc).lower() or "busy" in str(exc).lower())
                    and attempt < 3
                ):
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                    continue
                raise
        else:
            raise ApiError(503, "upload_busy", "Не удалось применить решение. Повторите попытку.")
        assert result is not None
    except BaseException:
        _discard_replacement(settings, staged)
        raise

    if not used_staged:
        # A concurrent decision won the race; nothing was switched.
        _discard_replacement(settings, staged)
        return result

    _remove_pending_files(settings, row)
    retired_cover = result.pop("_retired_cover", None)
    if retired_cover and retired_cover != staged.get("cover_name"):
        _remove_extracted_cover(settings, retired_cover)
    return result


def _batch_rows(conn: sqlite3.Connection, batch_id: str, scope: str, query: str) -> list:
    rows = conn.execute(
        "SELECT * FROM pending_uploads WHERE batch_id = ? AND state = 'pending' "
        "ORDER BY created_at ASC, rowid ASC",
        (batch_id,),
    ).fetchall()
    if scope == "search":
        needle = normalize_text(query)
        if not needle:
            return []
        filtered = []
        for row in rows:
            metadata = _json_loads(row["metadata"], {})
            haystack = " ".join(
                str(part or "")
                for part in (
                    row["filename"],
                    metadata.get("title"),
                    metadata.get("artist"),
                    metadata.get("album"),
                )
            )
            if needle in normalize_text(haystack):
                filtered.append(row)
        return filtered
    return rows


def _batch_replace(
    conn: sqlite3.Connection,
    settings: Settings,
    batch_id: str,
    scope: str,
    query: str,
    now: datetime,
) -> dict:
    rows = _batch_rows(conn, batch_id, scope, query)
    resolved: list[tuple] = []
    for row in rows:
        try:
            chosen = _resolve_replace_target(conn, settings, row, None)
            resolved.append((row, str(chosen["row"]["id"]), None))
        except ApiError as exc:
            status = "stale" if exc.code == "stale_match" else "failed"
            resolved.append((row, exc.extra.get("track_id"), (status, exc)))
    replaced = identical = skipped = failed = conflicts = 0
    items: list[dict] = []
    chosen_targets: set[str] = set()
    for row, target_id, error in resolved:
        if error is not None:
            status, exc = error
            failed += 1
            items.append(
                {
                    "id": row["id"],
                    "status": status,
                    "track_id": target_id,
                    "error": exc.message,
                }
            )
            continue
        if target_id and target_id in chosen_targets:
            # Another new file in this request already owns the target: only the
            # first claimant may replace, never last-wins.
            conflicts += 1
            items.append(
                {
                    "id": row["id"],
                    "status": "conflict",
                    "track_id": target_id,
                    "error": "Несколько файлов претендуют на один трек. Выберите одну версию.",
                }
            )
            continue
        if target_id:
            chosen_targets.add(target_id)
        try:
            result = decide(conn, settings, row["id"], "replace", now)
        except ApiError as exc:
            if exc.code == "already_decided":
                skipped += 1
                items.append(
                    {"id": row["id"], "status": "skipped", "track_id": None, "error": None}
                )
            elif exc.code == "stale_match":
                failed += 1
                items.append(
                    {
                        "id": row["id"],
                        "status": "stale",
                        "track_id": exc.extra.get("track_id"),
                        "error": exc.message,
                    }
                )
            else:
                failed += 1
                items.append(
                    {
                        "id": row["id"],
                        "status": "failed",
                        "track_id": None,
                        "error": exc.message,
                    }
                )
            continue
        status = str(result.get("status") or "replaced")
        if status == "replaced":
            replaced += 1
        elif status == "identical":
            identical += 1
        else:
            skipped += 1
        items.append(
            {
                "id": row["id"],
                "status": status,
                "track_id": result.get("track_id"),
                "error": None,
            }
        )
    _refresh_batch(conn, batch_id, now)
    conn.commit()
    return {
        "batch": batch_payload(conn, settings, batch_id),
        "processed": replaced + identical,
        "replaced": replaced,
        "identical": identical,
        "skipped": skipped,
        "failed": failed,
        "conflicts": conflicts,
        "items": items,
    }


def batch_decide(
    conn: sqlite3.Connection,
    settings: Settings,
    batch_id: str,
    action: str,
    scope: str,
    query: str,
    now: datetime,
) -> dict:
    if _batch_row(conn, batch_id) is None:
        raise ApiError(404, "batch_not_found", "Загрузка не найдена.")
    if action not in ("save", "skip", "replace"):
        raise ApiError(422, "invalid_action", "Решение должно быть save, skip или replace.")
    if scope not in ("all", "search"):
        raise ApiError(422, "invalid_scope", "Область решения должна быть all или search.")
    if action == "replace":
        return _batch_replace(conn, settings, batch_id, scope, query, now)
    rows = _batch_rows(conn, batch_id, scope, query)
    processed = 0
    for row in rows:
        try:
            decide(conn, settings, row["id"], action, now)
        except ApiError as exc:
            if exc.code == "already_decided":
                # Another request won the race; never override it.
                continue
            raise
        processed += 1
    _refresh_batch(conn, batch_id, now)
    conn.commit()
    return {"batch": batch_payload(conn, settings, batch_id), "processed": processed}


def batch_cancel(
    conn: sqlite3.Connection, settings: Settings, batch_id: str, now: datetime
) -> dict:
    if _batch_row(conn, batch_id) is None:
        raise ApiError(404, "batch_not_found", "Загрузка не найдена.")
    rows = _batch_rows(conn, batch_id, "all", "")
    for row in rows:
        try:
            decide(conn, settings, row["id"], "skip", now)
        except ApiError as exc:
            if exc.code == "already_decided":
                continue
            raise
    _refresh_batch(conn, batch_id, now, status="cancelled")
    conn.commit()
    return {"batch": batch_payload(conn, settings, batch_id)}


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup_pending(
    conn: sqlite3.Connection, settings: Settings, now: datetime
) -> int:
    """Expire inactive pending files (24 h by default) and orphan temp files."""

    ttl = timedelta(hours=max(0.0, float(settings.upload_ttl_hours or 24.0)))
    cutoff = now - ttl
    rows = conn.execute(
        "SELECT * FROM pending_uploads WHERE state = 'pending'"
    ).fetchall()
    removed = 0
    expired_rows = []
    for row in rows:
        updated = parse_iso(row["updated_at"]) or parse_iso(row["created_at"])
        if updated is not None and updated > cutoff:
            continue
        if row["batch_id"]:
            recent = conn.execute(
                "SELECT 1 FROM pending_uploads WHERE batch_id = ? AND updated_at > ? LIMIT 1",
                (row["batch_id"], to_iso(cutoff)),
            ).fetchone()
            if recent is not None:
                # An active batch is never expired.
                continue
        metadata = _json_loads(row["metadata"], {})
        metadata["error"] = "Срок ожидания решения истёк."
        cursor = conn.execute(
            "UPDATE pending_uploads SET state = 'error', reason = 'expired', metadata = ?, "
            "updated_at = ? WHERE id = ? AND state = 'pending'",
            (json.dumps(metadata, ensure_ascii=False), to_iso(now), row["id"]),
        )
        if cursor.rowcount != 1:
            # A decision won the race: never overwrite it.
            continue
        _refresh_batch(conn, row["batch_id"], now)
        expired_rows.append(row)
        removed += 1
    conn.commit()
    for row in expired_rows:
        _remove_pending_files(settings, row)

    referenced = set()
    for row in conn.execute(
        "SELECT temp_path, cover_temp FROM pending_uploads WHERE state = 'pending'"
    ).fetchall():
        for value in (row["temp_path"], row["cover_temp"]):
            if value:
                referenced.add(str(Path(str(value))))
    try:
        if not settings.pending_dir.is_dir():
            raise FileNotFoundError
        for entry in settings.pending_dir.iterdir():
            if not entry.is_file():
                continue
            if str(entry) in referenced:
                continue
            try:
                modified = datetime.fromtimestamp(entry.stat().st_mtime, tz=now.tzinfo)
            except OSError:
                continue
            if modified > cutoff:
                continue
            try:
                entry.unlink()
            except OSError:
                continue
    except FileNotFoundError:
        pass
    conn.commit()
    return removed


def startup_cleanup(conn: sqlite3.Connection, settings: Settings) -> None:
    """Remove temp files left behind by a crash at startup."""

    settings.pending_dir.mkdir(parents=True, exist_ok=True)
    rows = conn.execute("SELECT * FROM pending_uploads").fetchall()
    referenced = set()
    for row in rows:
        if row["state"] == "pending":
            for value in (row["temp_path"], row["cover_temp"]):
                if value:
                    referenced.add(str(Path(str(value))))
        else:
            _remove_pending_files(settings, row)
    try:
        if not settings.pending_dir.is_dir():
            raise FileNotFoundError
        for entry in settings.pending_dir.iterdir():
            if entry.is_file() and str(entry) not in referenced:
                try:
                    entry.unlink()
                except OSError:
                    continue
    except FileNotFoundError:
        pass
    conn.commit()
