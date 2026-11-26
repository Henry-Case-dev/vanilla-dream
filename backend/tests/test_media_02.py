"""VD-RADIO-MEDIA-02: duplicates, two-phase upload, editor, covers, export."""

from __future__ import annotations

import io
import sqlite3
import threading
import zipfile
from pathlib import Path

import mutagen

from app import db, exports, uploads
from app.config import SYSTEM_PLAYLIST_ID
from tests.helpers import (
    jpeg_bytes,
    make_playlist,
    write_mp3,
)


def _post_file(env, filename: str, data: bytes, batch_id: str | None = None):
    files = {"file": (filename, data, "audio/mpeg")}
    data_fields = {"batch_id": batch_id} if batch_id else None
    return env.client.post(
        "/api/admin/tracks/upload", files=files, data=data_fields
    )


def _upload_file(env, path: Path, batch_id: str | None = None):
    return _post_file(env, path.name, path.read_bytes(), batch_id)


def _added(env, path: Path, batch_id: str | None = None) -> dict:
    response = _upload_file(env, path, batch_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "added"
    return body["track"]


def _pending(env, path: Path, batch_id: str | None = None) -> dict:
    response = _upload_file(env, path, batch_id)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending"
    return body["pending"]


def _decide(env, pending_id: str, action: str):
    return env.client.post(
        f"/api/admin/uploads/{pending_id}/decide", json={"action": action}
    )


def _music_files(env) -> list[Path]:
    return [path for path in env.settings.music_dir.iterdir() if path.is_file()]


def _row(env, track_id: str):
    conn = db.open_db(env.settings)
    try:
        return conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    finally:
        conn.close()


def _audio_payload(data: bytes) -> bytes:
    """Strip an ID3v2 tag so the audio frames can be compared."""

    if data[:3] != b"ID3" or len(data) < 10:
        return data
    size = (
        ((data[6] & 0x7F) << 21)
        | ((data[7] & 0x7F) << 14)
        | ((data[8] & 0x7F) << 7)
        | (data[9] & 0x7F)
    )
    footer = 10 if data[5] & 0x10 else 0
    return data[10 + size + footer :]


def test_media_env_defaults_and_overrides(monkeypatch):
    from app.config import Settings

    for name in (
        "VD_PENDING_DIR",
        "VD_EXPORT_DIR",
        "VD_UPLOAD_TTL_HOURS",
        "VD_EXPORT_TTL_HOURS",
        "VD_EXPORT_RESERVE_BYTES",
        "VD_MUSIC_DIR",
        "VD_DB_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings.from_env()
    assert defaults.pending_dir == defaults.music_dir / ".pending"
    assert defaults.export_dir == defaults.db_path.parent / "exports"
    assert defaults.upload_ttl_hours == 24.0
    assert defaults.export_ttl_hours == 6.0
    assert defaults.export_reserve_bytes == 536870912

    monkeypatch.setenv("VD_PENDING_DIR", "/custom/pending")
    monkeypatch.setenv("VD_EXPORT_DIR", "/custom/exports")
    monkeypatch.setenv("VD_UPLOAD_TTL_HOURS", "3.5")
    monkeypatch.setenv("VD_EXPORT_TTL_HOURS", "1.25")
    monkeypatch.setenv("VD_EXPORT_RESERVE_BYTES", "1024")
    custom = Settings.from_env()
    assert custom.pending_dir == Path("/custom/pending")
    assert custom.export_dir == Path("/custom/exports")
    assert custom.upload_ttl_hours == 3.5
    assert custom.export_ttl_hours == 1.25
    assert custom.export_reserve_bytes == 1024


# ---------------------------------------------------------------------------
# Duplicates and independent storage
# ---------------------------------------------------------------------------


def test_unique_upload_stores_independent_path_and_source_filename(env, admin):
    path = write_mp3(
        env.settings.music_dir.parent / "Оригинал — Песня.mp3",
        title="Песня",
        artist="Исполнитель",
        album="Альбом",
    )
    track = _added(env, path)
    assert track["source_filename"] == "Оригинал — Песня.mp3"
    assert track["is_duplicate"] is False
    assert track["duplicate_of"] is None
    assert track["cover_kind"] == "none"
    assert track["can_revert_cover"] is False
    row = _row(env, track["id"])
    assert row["path"] == f"{track['id']}.mp3"
    assert (env.settings.music_dir / row["path"]).read_bytes() == path.read_bytes()
    assert row["source_title"] == "Песня"
    assert row["source_artist"] == "Исполнитель"


def test_content_duplicate_saves_independent_copy_and_is_idempotent(env, admin):
    first_path = write_mp3(
        env.settings.music_dir.parent / "first.mp3", title="Дубль", artist="Автор"
    )
    first = _added(env, first_path)
    pending = _pending(
        env,
        write_mp3(
            env.settings.music_dir.parent / "renamed-copy.mp3",
            title="Дубль",
            artist="Автор",
        ),
    )
    # The uploaded bytes are identical, so the original filename does not matter.
    assert pending["reason"] == "content"
    assert pending["match_track_id"] == first["id"]
    assert pending["matches"][0]["track"]["id"] == first["id"]

    saved = _decide(env, pending["id"], "save")
    assert saved.status_code == 200, saved.text
    body = saved.json()
    second = body["track"]
    assert second["id"] != first["id"]
    assert second["is_duplicate"] is True
    assert second["duplicate_kind"] == "content"
    assert second["duplicate_of"] == first["id"]
    assert second["source_filename"] == "renamed-copy.mp3"
    assert second["title"] == "Дубль"

    first_row = _row(env, first["id"])
    second_row = _row(env, second["id"])
    assert first_row["path"] != second_row["path"]
    assert first_row["sha256"] == second_row["sha256"]
    assert (env.settings.music_dir / first_row["path"]).exists()
    assert (env.settings.music_dir / second_row["path"]).exists()
    assert len(_music_files(env)) == 2

    repeat = _decide(env, pending["id"], "save")
    assert repeat.status_code == 200
    assert repeat.json()["track"]["id"] == second["id"]
    assert len(_music_files(env)) == 2

    conflict = _decide(env, pending["id"], "skip")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "already_decided"


def test_tag_and_possible_duplicate_detection(env, admin):
    first = _added(
        env,
        write_mp3(
            env.settings.music_dir.parent / "tags-a.mp3",
            title="Ночь",
            artist="Луна",
            album="Сиреневый",
            seconds=1.0,
        ),
    )
    same_tags = write_mp3(
        env.settings.music_dir.parent / "tags-b.mp3",
        title="Ночь",
        artist="Луна",
        album="Сиреневый",
        seconds=2.5,
    )
    pending = _pending(env, same_tags)
    assert pending["reason"] == "tags"
    assert pending["match_track_id"] == first["id"]
    saved = _decide(env, pending["id"], "save").json()
    assert saved["track"]["duplicate_kind"] == "tags"
    assert saved["track"]["duplicate_of"] == first["id"]
    # Duration is never a matching criterion.
    assert saved["track"]["duration"] != first["duration"]

    album_differs = write_mp3(
        env.settings.music_dir.parent / "tags-c.mp3",
        title="Ночь",
        artist="Луна",
        album="Другой",
        seconds=3.0,
    )
    possible = _pending(env, album_differs)
    assert possible["reason"] == "possible"
    # Both copies are candidates; the newest one is the best match.
    assert possible["match_track_id"] == saved["track"]["id"]


def test_case_and_unicode_normalization_for_comparison_only(env, admin):
    first = _added(
        env,
        write_mp3(
            env.settings.music_dir.parent / "unicode-a.mp3",
            title="Café  Song",
            artist="Ärtist",
            album="Alb",
        ),
    )
    # Full-width characters, different case and doubled spaces all normalise.
    second = write_mp3(
        env.settings.music_dir.parent / "unicode-b.mp3",
        title="ＣＡＦÉ SONG",
        artist="ärtist",
        album="alb",
    )
    pending = _pending(env, second)
    assert pending["reason"] == "tags"
    assert pending["match_track_id"] == first["id"]
    saved = _decide(env, pending["id"], "save").json()["track"]
    # Displayed values are never rewritten by normalisation.
    assert saved["title"] == "ＣＡＦÉ SONG"
    assert saved["artist"] == "ärtist"


def test_missing_tags_or_filename_only_title_never_matches(env, admin):
    _added(env, write_mp3(env.settings.music_dir.parent / "plain.mp3"))
    # Same filename-derived title, different audio, no tags at all.
    no_tags = write_mp3(env.settings.music_dir.parent / "plain-copy.mp3", seconds=2.0)
    assert _added(env, no_tags)["is_duplicate"] is False

    tagged = _added(
        env,
        write_mp3(
            env.settings.music_dir.parent / "tagged.mp3",
            title="Только название",
            artist="Артист",
        ),
    )
    # Incoming file has no tags: the filename stem is not a title for matching.
    same_stem = write_mp3(
        env.settings.music_dir.parent / "Только название.mp3", seconds=2.4
    )
    fresh = _added(env, same_stem)
    assert fresh["is_duplicate"] is False
    assert fresh["title"] == "Только название"
    assert _row(env, fresh["id"])["source_title"] is None
    assert fresh["id"] != tagged["id"]


def test_compare_against_displayed_and_original_tags(env, admin):
    original = _added(
        env,
        write_mp3(
            env.settings.music_dir.parent / "source-tags.mp3",
            title="Оригинал",
            artist="Первый",
            album="Альбом",
        ),
    )
    edited = env.client.patch(
        f"/api/admin/tracks/{original['id']}",
        json={"title": "Изменённое", "artist": "Второй"},
    )
    assert edited.status_code == 200, edited.text

    again = write_mp3(
        env.settings.music_dir.parent / "same-source-tags.mp3",
        title="Оригинал",
        artist="Первый",
        album="Альбом",
        seconds=2.0,
    )
    pending = _pending(env, again)
    assert pending["reason"] == "tags"
    assert pending["match_track_id"] == original["id"]


def test_pending_cover_is_served_for_comparison(env, admin):
    _added(
        env,
        write_mp3(
            env.settings.music_dir.parent / "pending-cover-base.mp3",
            title="С обложкой",
            artist="Автор",
            cover=True,
        ),
    )
    pending = _pending(
        env,
        write_mp3(
            env.settings.music_dir.parent / "pending-cover-new.mp3",
            title="С обложкой",
            artist="Автор",
            seconds=2.0,
            cover=True,
        ),
    )
    url = pending["metadata"]["cover_url"]
    assert url == f"/api/admin/uploads/{pending['id']}/cover"
    response = env.client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert len(response.content) > 100


# ---------------------------------------------------------------------------
# Batches and decisions
# ---------------------------------------------------------------------------


def test_batch_mix_in_batch_duplicates_and_error_counter(env, admin):
    _added(
        env,
        write_mp3(
            env.settings.music_dir.parent / "existing.mp3", title="Уже есть", artist="Автор"
        ),
    )
    batch_id = "batch-mix-1"
    unique = write_mp3(
        env.settings.music_dir.parent / "batch-unique.mp3", title="Новый", artist="Автор"
    )
    added = _upload_file(env, unique, batch_id)
    assert added.status_code == 201

    duplicate = write_mp3(
        env.settings.music_dir.parent / "batch-dup.mp3", title="Уже есть", artist="Автор"
    )
    pending = _upload_file(env, duplicate, batch_id)
    assert pending.status_code == 202

    # The same new file inside the same batch now matches the just-added track.
    in_batch = _upload_file(env, unique, batch_id)
    assert in_batch.status_code == 202
    assert in_batch.json()["pending"]["reason"] == "content"

    bad = _post_file(env, "broken.mp3", b"not audio", batch_id)
    assert bad.status_code == 422

    batch = env.client.get(f"/api/admin/uploads/{batch_id}").json()["batch"]
    assert batch["status"] == "pending"
    assert batch["total"] == 4
    assert batch["counts"] == {
        "added": 1,
        "replaced": 0,
        "pending": 2,
        "skipped": 0,
        "errors": 1,
    }
    assert {item["state"] for item in batch["items"]} == {"added", "pending", "error"}
    assert not any(
        path.name.startswith("incoming-") for path in env.settings.pending_dir.iterdir()
    )


def test_batch_decisions_only_touch_undecided_items(env, admin):
    batch_id = "batch-decisions-1"
    first = _added(
        env,
        write_mp3(env.settings.music_dir.parent / "base.mp3", title="База", artist="Автор"),
    )
    rows = []
    for index in range(3):
        pending = _pending(
            env,
            write_mp3(
                env.settings.music_dir.parent / f"decision-{index}.mp3",
                title="База",
                artist="Автор",
                seconds=1.5 + index,
            ),
            batch_id,
        )
        rows.append(pending)
    assert all(row["reason"] == "tags" for row in rows)

    individual = _decide(env, rows[0]["id"], "skip")
    assert individual.status_code == 200
    assert individual.json()["status"] == "skipped"

    bulk = env.client.post(
        f"/api/admin/uploads/batch/{batch_id}/decide", json={"action": "save"}
    )
    assert bulk.status_code == 200, bulk.text
    body = bulk.json()
    assert body["processed"] == 2
    assert body["batch"]["status"] == "done"
    states = {item["id"]: item["state"] for item in body["batch"]["items"]}
    assert states[rows[0]["id"]] == "skipped"
    assert states[rows[1]["id"]] == "saved"
    assert states[rows[2]["id"]] == "saved"

    # Individual decisions are preserved by a repeated bulk action.
    again = env.client.post(
        f"/api/admin/uploads/batch/{batch_id}/decide", json={"action": "save"}
    ).json()
    assert again["processed"] == 0
    listing = env.client.get("/api/admin/tracks").json()
    assert listing["total"] == 3  # base + two saved copies


def test_batch_search_scope_and_playlist_auto_cover(env, admin):
    track = _added(
        env,
        write_mp3(
            env.settings.music_dir.parent / "auto-cover.mp3",
            title="Обложка плейлиста",
            artist="Автор",
            cover=True,
        ),
    )
    playlist = make_playlist(env.client, "Обложки", [track["id"]])
    extracted = env.settings.cover_dir / _row(env, track["id"])["cover_path"]
    original_bytes = extracted.read_bytes()
    env.client.post(
        f"/api/admin/tracks/{track['id']}/cover",
        files={"file": ("custom.png", jpeg_bytes((100, 100), (20, 30, 40)), "image/png")},
    )
    custom_bytes = (
        env.settings.cover_dir / _row(env, track["id"])["custom_cover_path"]
    ).read_bytes()
    public_cover = env.client.get(f"/api/admin/tracks/{track['id']}/cover")
    assert public_cover.content == custom_bytes != original_bytes
    playlist_cover = env.client.get(f"/api/public/playlist-cover/{playlist['id']}")
    assert playlist_cover.status_code == 200
    assert playlist_cover.content == custom_bytes

    batch_id = "search-batch"
    _added(
        env,
        write_mp3(
            env.settings.music_dir.parent / "search-base.mp3", title="Поиск", artist="Автор"
        ),
    )
    first = _pending(
        env,
        write_mp3(
            env.settings.music_dir.parent / "search-one.mp3",
            title="Поиск",
            artist="Автор",
            seconds=2.0,
        ),
        batch_id,
    )
    second = _pending(
        env,
        write_mp3(
            env.settings.music_dir.parent / "search-two.mp3",
            title="Поиск",
            artist="Автор",
            seconds=3.0,
        ),
        batch_id,
    )
    result = env.client.post(
        f"/api/admin/uploads/batch/{batch_id}/decide",
        json={"action": "skip", "scope": "search", "query": "search-one"},
    )
    assert result.status_code == 200, result.text
    assert result.json()["processed"] == 1
    states = {
        item["id"]: item["state"] for item in result.json()["batch"]["items"]
    }
    assert states[first["id"]] == "skipped"
    assert states[second["id"]] == "pending"


def test_parallel_batches_save_independent_records(env, admin):
    first = _added(
        env,
        write_mp3(env.settings.music_dir.parent / "shared.mp3", title="Общий", artist="Автор"),
    )
    pending_a = _pending(
        env,
        write_mp3(
            env.settings.music_dir.parent / "dup-a.mp3", title="Общий", artist="Автор"
        ),
        "parallel-a",
    )
    pending_b = _pending(
        env,
        write_mp3(
            env.settings.music_dir.parent / "dup-b.mp3", title="Общий", artist="Автор"
        ),
        "parallel-b",
    )
    first_row = _row(env, first["id"])
    first_bytes = (env.settings.music_dir / first_row["path"]).read_bytes()
    saved_a = _decide(env, pending_a["id"], "save").json()["track"]
    saved_b = _decide(env, pending_b["id"], "save").json()["track"]
    assert len({first["id"], saved_a["id"], saved_b["id"]}) == 3
    assert saved_a["duplicate_of"] == first["id"]
    assert saved_b["duplicate_of"] == first["id"]
    assert _row(env, saved_a["id"])["path"] != _row(env, saved_b["id"])["path"]
    assert (env.settings.music_dir / first_row["path"]).read_bytes() == first_bytes
    assert len(_music_files(env)) == 3


def test_concurrent_save_decisions_are_serialized(env, admin):
    first = _added(
        env,
        write_mp3(env.settings.music_dir.parent / "concurrent.mp3", title="Поток", artist="Автор"),
    )
    first_path = env.settings.music_dir / _row(env, first["id"])["path"]
    first_bytes = first_path.read_bytes()
    pending_a = _pending(
        env,
        write_mp3(
            env.settings.music_dir.parent / "concurrent-a.mp3",
            title="Поток",
            artist="Автор",
            seconds=2.0,
        ),
        "concurrent-batch-a",
    )
    pending_b = _pending(
        env,
        write_mp3(
            env.settings.music_dir.parent / "concurrent-b.mp3",
            title="Поток",
            artist="Автор",
            seconds=3.0,
        ),
        "concurrent-batch-b",
    )
    results: dict[str, dict] = {}
    errors: list[BaseException] = []

    def save(pending_id: str) -> None:
        conn = db.open_db(env.settings)
        try:
            results[pending_id] = uploads.decide(
                conn, env.settings, pending_id, "save", env.clock.now()
            )
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion
            errors.append(exc)
        finally:
            conn.close()

    threads = [
        threading.Thread(target=save, args=(pending["id"],))
        for pending in (pending_a, pending_b)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    saved_ids = {results[pending_a["id"]]["track"]["id"], results[pending_b["id"]]["track"]["id"]}
    assert len(saved_ids) == 2
    assert first["id"] not in saved_ids
    assert first_path.read_bytes() == first_bytes
    assert len(_music_files(env)) == 3


def test_batch_cancel_skips_all_undecided_and_removes_temp(env, admin):
    batch_id = "batch-cancel-1"
    _added(
        env,
        write_mp3(env.settings.music_dir.parent / "cancel-base.mp3", title="Отмена", artist="Автор"),
    )
    rows = [
        _pending(
            env,
            write_mp3(
                env.settings.music_dir.parent / f"cancel-{index}.mp3",
                title="Отмена",
                artist="Автор",
                seconds=1.0 + index,
            ),
            batch_id,
        )
        for index in range(2)
    ]
    before = sorted(path.name for path in env.settings.pending_dir.iterdir())
    assert len(before) >= 2
    cancelled = env.client.post(f"/api/admin/uploads/batch/{batch_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    batch = cancelled.json()["batch"]
    assert batch["status"] == "cancelled"
    assert batch["counts"]["pending"] == 0
    assert batch["counts"]["skipped"] == 2
    assert [item["state"] for item in batch["items"]] == ["skipped", "skipped"]
    assert not list(env.settings.pending_dir.glob("*"))
    assert env.client.get("/api/admin/tracks").json()["total"] == 1
    assert _decide(env, rows[0]["id"], "skip").status_code == 200
    for row in rows:
        assert _decide(env, row["id"], "save").status_code == 409


def test_pending_files_are_invisible_and_expire(env, admin):
    base = _added(
        env,
        write_mp3(env.settings.music_dir.parent / "visible-base.mp3", title="Видимый", artist="Автор"),
    )
    pending = _pending(
        env,
        write_mp3(
            env.settings.music_dir.parent / "hidden.mp3",
            title="Видимый",
            artist="Автор",
            seconds=2.0,
        ),
        "invisible-batch",
    )
    assert env.client.get("/api/admin/tracks").json()["total"] == 1
    playlist = env.client.get(f"/api/admin/playlists/{SYSTEM_PLAYLIST_ID}").json()["playlist"]
    assert playlist["track_count"] == 1
    queue = env.client.get("/api/admin/queue").json()
    assert len(queue["items"]) <= 1
    # Only the visible library track can be queued; the pending file has no row.
    assert all(item["track"]["id"] == base["id"] for item in queue["items"])
    assert all(item["track"]["id"] != pending["id"] for item in queue["items"])
    now_playing = env.client.get("/api/public/now-playing").json()
    assert now_playing["track"] is None
    temp_files = list(env.settings.pending_dir.glob("*"))
    assert temp_files

    env.clock.advance(25 * 3600)
    env.app.state.scheduler.tick()
    batch = env.client.get("/api/admin/uploads/invisible-batch").json()["batch"]
    assert batch["counts"]["errors"] == 1
    assert batch["items"][0]["state"] == "error"
    assert batch["items"][0]["reason"] == "expired"
    assert not list(env.settings.pending_dir.glob("*"))
    assert _decide(env, pending["id"], "save").status_code == 409


def test_active_batch_is_not_expired_by_ttl(env, admin):
    batch_id = "active-batch"
    _added(
        env,
        write_mp3(env.settings.music_dir.parent / "active-base.mp3", title="Актив", artist="Автор"),
    )
    old = _pending(
        env,
        write_mp3(
            env.settings.music_dir.parent / "active-old.mp3", title="Актив", artist="Автор"
        ),
        batch_id,
    )
    # The old item alone is past the TTL, but the batch has newer activity.
    conn = db.open_db(env.settings)
    try:
        conn.execute(
            "UPDATE pending_uploads SET created_at = ?, updated_at = ? WHERE id = ?",
            ("2026-11-01T00:00:00.000Z", "2026-11-01T00:00:00.000Z", old["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    env.clock.advance(3600)
    _pending(
        env,
        write_mp3(
            env.settings.music_dir.parent / "active-new.mp3",
            title="Актив",
            artist="Автор",
            seconds=2.0,
        ),
        batch_id,
    )
    env.app.state.scheduler.tick()
    batch = env.client.get(f"/api/admin/uploads/{batch_id}").json()["batch"]
    assert batch["counts"]["pending"] == 2


def test_startup_cleanup_removes_orphan_temp_files(env):
    env.settings.pending_dir.mkdir(parents=True, exist_ok=True)
    orphan = env.settings.pending_dir / "orphan-incoming.mp3"
    orphan.write_bytes(b"stale")
    conn = db.open_db(env.settings)
    try:
        uploads.startup_cleanup(conn, env.settings)
    finally:
        conn.close()
    assert not orphan.exists()


# ---------------------------------------------------------------------------
# Editor, covers, independence
# ---------------------------------------------------------------------------


def test_editor_keeps_audio_and_does_not_touch_other_rows(env, admin):
    path = write_mp3(
        env.settings.music_dir.parent / "edit-a.mp3",
        title="Старое",
        artist="Автор",
        album="Альбом",
    )
    first = _added(env, path)
    second = _added(
        env,
        write_mp3(env.settings.music_dir.parent / "edit-b.mp3", title="Другое", artist="Автор"),
    )
    original_bytes = (env.settings.music_dir / _row(env, first["id"])["path"]).read_bytes()
    original_sha = _row(env, first["id"])["sha256"]

    updated = env.client.patch(
        f"/api/admin/tracks/{first['id']}",
        json={"title": "Новое", "artist": "", "album": ""},
    )
    assert updated.status_code == 200, updated.text
    payload = updated.json()["track"]
    assert payload["title"] == "Новое"
    assert payload["artist"] is None and payload["album"] is None

    empty_title = env.client.patch(
        f"/api/admin/tracks/{first['id']}", json={"title": "   "}
    )
    assert empty_title.status_code == 422

    row = _row(env, first["id"])
    assert row["sha256"] == original_sha
    assert (env.settings.music_dir / row["path"]).read_bytes() == original_bytes
    assert _row(env, second["id"])["title"] == "Другое"


def test_custom_cover_upload_revert_and_invalid_keeps_state(env, admin):
    track = _added(
        env,
        write_mp3(
            env.settings.music_dir.parent / "cover-track.mp3",
            title="Обложка",
            artist="Автор",
            cover=True,
        ),
    )
    assert track["cover_kind"] == "file"
    assert track["can_revert_cover"] is False

    uploaded = env.client.post(
        f"/api/admin/tracks/{track['id']}/cover",
        files={"file": ("cover.png", jpeg_bytes((1800, 1200)), "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    covered = uploaded.json()["track"]
    assert covered["cover_kind"] == "custom"
    assert covered["can_revert_cover"] is True
    row = _row(env, track["id"])
    custom = env.settings.cover_dir / row["custom_cover_path"]
    assert custom.exists()
    from PIL import Image

    with Image.open(custom) as image:
        assert image.size == (600, 600)
        assert image.format == "JPEG"

    tagged = env.client.get(
        f"/api/admin/tracks/{track['id']}/download", params={"mode": "tags"}
    )
    tagged_audio = mutagen.File(io.BytesIO(tagged.content))
    assert tagged_audio.tags.getall("APIC")[0].data == custom.read_bytes()

    invalid = env.client.post(
        f"/api/admin/tracks/{track['id']}/cover",
        files={"file": ("bad.png", b"not an image", "image/png")},
    )
    assert invalid.status_code == 422
    assert _row(env, track["id"])["custom_cover_path"] == row["custom_cover_path"]

    reverted = env.client.delete(f"/api/admin/tracks/{track['id']}/cover")
    assert reverted.status_code == 200
    assert reverted.json()["track"]["cover_kind"] == "file"
    assert not custom.exists()

    no_custom = env.client.delete(f"/api/admin/tracks/{track['id']}/cover")
    assert no_custom.status_code == 200
    assert no_custom.json()["track"]["cover_kind"] == "file"

    plain = _added(
        env,
        write_mp3(env.settings.music_dir.parent / "plain-cover.mp3", title="Без обложки"),
    )
    removed = env.client.delete(f"/api/admin/tracks/{plain['id']}/cover")
    assert removed.status_code == 200
    assert removed.json()["track"]["cover_kind"] == "none"


def test_duplicate_flag_survives_editor_and_independent_delete(env, admin):
    first = _added(
        env,
        write_mp3(env.settings.music_dir.parent / "ind-base.mp3", title="Копия", artist="Автор"),
    )
    pending = _pending(
        env,
        write_mp3(
            env.settings.music_dir.parent / "ind-copy.mp3",
            title="Копия",
            artist="Автор",
            seconds=2.0,
        ),
    )
    second = _decide(env, pending["id"], "save").json()["track"]
    edited = env.client.patch(
        f"/api/admin/tracks/{second['id']}", json={"title": "Копия (правка)"}
    ).json()["track"]
    assert edited["is_duplicate"] is True
    assert edited["duplicate_of"] == first["id"]
    assert edited["duplicate_kind"] == "tags"
    assert _row(env, first["id"])["title"] == "Копия"

    env.client.post(
        f"/api/admin/tracks/{second['id']}/cover",
        files={"file": ("cover.png", jpeg_bytes(), "image/png")},
    )
    assert env.client.get(f"/api/admin/tracks/{first['id']}/cover").status_code == 404
    assert env.client.get(f"/api/admin/tracks/{second['id']}/cover").status_code == 200

    first_path = env.settings.music_dir / _row(env, first["id"])["path"]
    second_path = env.settings.music_dir / _row(env, second["id"])["path"]
    custom = env.settings.cover_dir / _row(env, second["id"])["custom_cover_path"]
    assert env.client.delete(f"/api/admin/tracks/{first['id']}").status_code == 200
    assert not first_path.exists()
    assert second_path.exists()
    assert custom.exists()
    assert _row(env, second["id"]) is not None

    assert env.client.delete(f"/api/admin/tracks/{second['id']}").status_code == 200
    assert not second_path.exists()
    assert not custom.exists()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_single_download_original_and_tags_readback(env, admin):
    path = write_mp3(
        env.settings.music_dir.parent / "download.mp3",
        title="Скачиваемая",
        artist="Ёлка",
        album="Альбом",
        cover=True,
    )
    track = _added(env, path)
    env.client.patch(
        f"/api/admin/tracks/{track['id']}",
        json={"title": "Новое имя", "artist": "Ёлка", "album": "Новый альбом"},
    )
    source = env.settings.music_dir / _row(env, track["id"])["path"]

    original = env.client.get(
        f"/api/admin/tracks/{track['id']}/download", params={"mode": "original"}
    )
    assert original.status_code == 200
    assert original.content == source.read_bytes()
    disposition = original.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''" in disposition

    tagged = env.client.get(
        f"/api/admin/tracks/{track['id']}/download", params={"mode": "tags"}
    )
    assert tagged.status_code == 200
    audio = mutagen.File(io.BytesIO(tagged.content), easy=False)
    assert audio.tags["TIT2"].text[0] == "Новое имя"
    assert audio.tags["TPE1"].text[0] == "Ёлка"
    assert audio.tags["TALB"].text[0] == "Новый альбом"
    frames = audio.tags.getall("APIC")
    assert frames and frames[0].data
    assert _audio_payload(tagged.content) == _audio_payload(source.read_bytes())
    # The temporary tagged copy is removed after the response.
    tmp_dir = env.settings.export_dir / "tmp"
    assert not list(tmp_dir.glob("tagged-*"))
    # Reading the file changed no audio file or hash.
    assert (env.settings.music_dir / _row(env, track["id"])["path"]).read_bytes() == source.read_bytes()


def test_new_media_endpoints_require_admin(env):
    assert env.client.get("/api/admin/exports/none").status_code == 401
    assert env.client.get("/api/admin/uploads/none").status_code == 401
    assert env.client.get("/api/admin/tracks/none/download").status_code == 401
    assert env.client.post("/api/admin/exports", json={}).status_code == 401


def test_export_zip_playlist_order_m3u8_and_safe_names(env, admin):
    target = write_mp3(
        env.settings.music_dir.parent / "zip-a.mp3",
        title="../../Выход",
        artist="Артист",
        album="Альбом",
        seconds=1.0,
    )
    first = _added(env, target)
    second = _added(
        env,
        write_mp3(
            env.settings.music_dir.parent / "zip-b.mp3",
            title="Одинаковая",
            artist="Ёлка",
            seconds=2.0,
        ),
    )
    third = _added(
        env,
        write_mp3(
            env.settings.music_dir.parent / "zip-c.mp3",
            title="Одинаковая",
            artist="Другой",
            seconds=3.0,
        ),
    )
    env.client.patch(
        f"/api/admin/tracks/{third['id']}",
        json={"title": "Одинаковая", "artist": "Ёлка"},
    )
    playlist = make_playlist(env.client, "Порядок", [third["id"], first["id"], second["id"]])

    created = env.client.post(
        "/api/admin/exports",
        json={"scope": "playlist", "playlist_id": playlist["id"], "format": "zip"},
    )
    assert created.status_code == 202, created.text
    job = created.json()["job"]
    assert job["state"] == "queued"

    status = env.client.get(f"/api/admin/exports/{job['id']}").json()["job"]
    assert status["state"] == "ready", status
    assert status["processed"] == status["total"] == 3
    assert status["progress"] == 1.0
    assert status["archive"]["size"] > 0
    from app.util import parse_iso

    expires = parse_iso(status["expires_at"])
    assert 5.9 * 3600 <= (expires - env.clock.now()).total_seconds() <= 6.1 * 3600

    archive = env.client.get(f"/api/admin/exports/{job['id']}/archive")
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zip_file:
        assert all(
            info.compress_type == zipfile.ZIP_STORED for info in zip_file.infolist()
        )
        names = zip_file.namelist()
        assert names[-1] == "playlist.m3u8"
        entries = names[:-1]
        assert len(entries) == 3
        assert entries[0].startswith("001 - ")
        assert entries[1].startswith("002 - ")
        assert entries[2].startswith("003 - ")
        assert all("/" not in name and "\\" not in name for name in names)
        assert all(not part.startswith("..") for part in entries)
        assert any("Ёлка" in name for name in entries)
        assert entries[1].endswith(" (2).mp3") or entries[2].endswith(" (2).mp3")
        manifest = zip_file.read("playlist.m3u8").decode("utf-8")
        lines = [line for line in manifest.splitlines() if line and not line.startswith("#")]
        assert lines == entries


def test_export_zip_entries_are_windows_safe(env, admin):
    track = _added(
        env,
        write_mp3(
            env.settings.music_dir.parent / "reserved.mp3",
            title="CON",
            artist="AUX",
        ),
    )
    created = env.client.post(
        "/api/admin/exports",
        json={"scope": "tracks", "ids": [track["id"]], "format": "zip"},
    )
    job_id = created.json()["job"]["id"]
    archive = env.client.get(f"/api/admin/exports/{job_id}/archive")
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zip_file:
        entry = zip_file.namelist()[0]
    stem = Path(entry).stem.upper()
    assert stem not in exports.WINDOWS_RESERVED
    assert ":" not in entry and "?" not in entry


def test_export_zip_tags_mode_and_library_scope(env, admin):
    first = _added(
        env,
        write_mp3(
            env.settings.music_dir.parent / "lib-a.mp3",
            title="Библиотека",
            artist="Автор",
            album="Альбом",
        ),
    )
    _added(
        env,
        write_mp3(env.settings.music_dir.parent / "lib-b.mp3", title="Вторая", artist="Автор"),
    )
    env.client.patch(
        f"/api/admin/tracks/{first['id']}",
        json={"title": "Библиотека (правка)", "artist": "Другой"},
    )
    created = env.client.post(
        "/api/admin/exports",
        json={"scope": "library", "format": "zip", "metadata": "tags"},
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["job"]["id"]
    status = env.client.get(f"/api/admin/exports/{job_id}").json()["job"]
    assert status["state"] == "ready", status
    archive = env.client.get(f"/api/admin/exports/{job_id}/archive")
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zip_file:
        entry = next(name for name in zip_file.namelist() if "Библиотека" in name)
        data = zip_file.read(entry)
        audio = mutagen.File(io.BytesIO(data))
        assert audio.tags["TIT2"].text[0] == "Библиотека (правка)"
        assert audio.tags["TPE1"].text[0] == "Другой"
        source = env.settings.music_dir / _row(env, first["id"])["path"]
        assert _audio_payload(data) == _audio_payload(source.read_bytes())


def test_export_selection_and_missing_file_error(env, admin):
    first = _added(
        env,
        write_mp3(env.settings.music_dir.parent / "sel-a.mp3", title="Выбор A", artist="Автор"),
    )
    second = _added(
        env,
        write_mp3(env.settings.music_dir.parent / "sel-b.mp3", title="Выбор B", artist="Автор"),
    )
    files = env.client.post(
        "/api/admin/exports",
        json={
            "scope": "tracks",
            "ids": [second["id"], first["id"]],
            "format": "files",
            "metadata": "original",
        },
    )
    assert files.status_code == 200, files.text
    body = files.json()
    assert [item["track_id"] for item in body["files"]] == [second["id"], first["id"]]
    download = env.client.get(body["files"][0]["url"])
    assert download.status_code == 200

    selection_zip = env.client.post(
        "/api/admin/exports",
        json={"scope": "tracks", "ids": [first["id"], second["id"]], "format": "zip"},
    )
    assert selection_zip.status_code == 202
    selection_job = selection_zip.json()["job"]["id"]
    assert env.client.get(f"/api/admin/exports/{selection_job}").json()["job"]["state"] == "ready"
    selection_archive = env.client.get(f"/api/admin/exports/{selection_job}/archive")
    with zipfile.ZipFile(io.BytesIO(selection_archive.content)) as zip_file:
        names = zip_file.namelist()
        assert len(names) == 3
        assert names[-1] == "playlist.m3u8"
        assert names[0].startswith("001 - ")
        assert names[1].startswith("002 - ")

    # Remove one source file before the background job reads it.
    missing_path = env.settings.music_dir / _row(env, second["id"])["path"]
    missing_path.unlink()
    archive = env.client.post(
        "/api/admin/exports",
        json={"scope": "tracks", "ids": [first["id"], second["id"]], "format": "zip"},
    )
    assert archive.status_code == 202
    job_id = archive.json()["job"]["id"]
    status = env.client.get(f"/api/admin/exports/{job_id}").json()["job"]
    assert status["state"] == "error"
    assert "Файл трека не найден" in status["error"]
    assert not (env.settings.export_dir / job_id).exists()


def test_export_disk_guard_cancel_and_expiry(env, admin, monkeypatch):
    import dataclasses

    track = _added(
        env,
        write_mp3(env.settings.music_dir.parent / "guard.mp3", title="Место", artist="Автор"),
    )
    original_settings = env.app.state.settings
    env.app.state.settings = dataclasses.replace(
        original_settings, export_reserve_bytes=10**18
    )
    try:
        guarded = env.client.post(
            "/api/admin/exports",
            json={"scope": "tracks", "ids": [track["id"]], "format": "zip"},
        )
    finally:
        env.app.state.settings = original_settings
    assert guarded.status_code == 507
    assert "свободного места" in guarded.json()["detail"]["message"]

    monkeypatch.setattr(exports, "process_job", lambda *args, **kwargs: None)
    queued = env.client.post(
        "/api/admin/exports",
        json={"scope": "tracks", "ids": [track["id"]], "format": "zip"},
    )
    assert queued.status_code == 202
    job_id = queued.json()["job"]["id"]
    assert env.client.get(f"/api/admin/exports/{job_id}").json()["job"]["state"] == "queued"
    cancelled = env.client.post(f"/api/admin/exports/{job_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["job"]["state"] == "cancelled"
    assert env.client.get(f"/api/admin/exports/{job_id}/archive").status_code == 409
    monkeypatch.undo()

    cancel_ready = env.client.post(
        "/api/admin/exports",
        json={"scope": "tracks", "ids": [track["id"]], "format": "zip"},
    )
    cancel_ready_id = cancel_ready.json()["job"]["id"]
    assert (env.settings.export_dir / cancel_ready_id).exists()
    cancelled_ready = env.client.post(f"/api/admin/exports/{cancel_ready_id}/cancel")
    assert cancelled_ready.json()["job"]["state"] == "cancelled"
    assert not (env.settings.export_dir / cancel_ready_id).exists()

    ready = env.client.post(
        "/api/admin/exports",
        json={"scope": "tracks", "ids": [track["id"]], "format": "zip"},
    )
    ready_id = ready.json()["job"]["id"]
    assert env.client.get(f"/api/admin/exports/{ready_id}").json()["job"]["state"] == "ready"
    conn = db.open_db(env.settings)
    try:
        conn.execute(
            "UPDATE export_jobs SET expires_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00.000Z", ready_id),
        )
        conn.commit()
        removed = exports.cleanup_exports(conn, env.settings, env.clock.now())
    finally:
        conn.close()
    assert removed == 1
    assert env.client.get(f"/api/admin/exports/{ready_id}").status_code == 404
    assert not (env.settings.export_dir / ready_id).exists()


def test_export_startup_cleanup_marks_stale_jobs(env, admin):
    track = _added(
        env,
        write_mp3(env.settings.music_dir.parent / "startup-export.mp3", title="Старт", artist="Автор"),
    )
    created = env.client.post(
        "/api/admin/exports",
        json={"scope": "tracks", "ids": [track["id"]], "format": "zip"},
    )
    job_id = created.json()["job"]["id"]
    assert env.client.get(f"/api/admin/exports/{job_id}").json()["job"]["state"] == "ready"
    # Simulate a crash that removed the archive before the service restarted.
    archive = next((env.settings.export_dir / job_id).glob("*.zip"))
    archive.unlink()
    conn = db.open_db(env.settings)
    try:
        exports.startup_cleanup(conn, env.settings, env.clock.now())
    finally:
        conn.close()
    status = env.client.get(f"/api/admin/exports/{job_id}").json()["job"]
    assert status["state"] == "error"
    assert "не найден" in status["error"]


def test_export_does_not_touch_station_or_queue(env, admin):
    track = _added(
        env,
        write_mp3(env.settings.music_dir.parent / "station-safe.mp3", title="Эфир", artist="Автор"),
    )
    queue_before = env.client.get("/api/admin/queue").json()
    status_before = env.client.get("/api/admin/station/status").json()
    created = env.client.post(
        "/api/admin/exports",
        json={"scope": "tracks", "ids": [track["id"]], "format": "zip"},
    )
    assert created.status_code == 202
    assert env.client.get("/api/admin/queue").json() == queue_before
    status_after = env.client.get("/api/admin/station/status").json()
    assert status_after["state"] == status_before["state"] == "off"
    assert status_after["desired"] == "stopped"


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_v2_preserves_rows_relations_and_drops_unique(settings):
    conn = sqlite3.connect(str(settings.db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE tracks (
                id TEXT PRIMARY KEY,
                sha256 TEXT UNIQUE NOT NULL,
                path TEXT NOT NULL,
                ext TEXT,
                title TEXT NOT NULL,
                artist TEXT,
                album TEXT,
                genre TEXT,
                year INTEGER,
                duration REAL,
                bitrate INTEGER,
                size INTEGER,
                cover_path TEXT,
                source_title TEXT,
                source_artist TEXT,
                source_album TEXT,
                source_genre TEXT,
                play_count INTEGER DEFAULT 0,
                pending_delete INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE playlists (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
                cover_path TEXT, shuffle INTEGER DEFAULT 0,
                repeat_mode TEXT DEFAULT 'none', system INTEGER NOT NULL DEFAULT 0,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE playlist_tracks (
                playlist_id TEXT, track_id TEXT, position INTEGER,
                PRIMARY KEY (playlist_id, track_id)
            );
            CREATE TABLE history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, track_id TEXT, position_id TEXT,
                started_at TEXT, ended_at TEXT, source_kind TEXT, source_ref TEXT,
                title TEXT, artist TEXT, album TEXT
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        conn.execute("PRAGMA user_version = 2")
        conn.execute(
            "INSERT INTO tracks(id, sha256, path, ext, title, artist, album, cover_path, "
            "source_title, source_artist, source_album, created_at, updated_at) "
            "VALUES('old-1', 'samehash', 'legacy.mp3', '.mp3', 'Legacy', 'Artist', "
            "'Album', 'old-1.jpg', 'Legacy', 'Artist', 'Album', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO playlists(id, name, description, cover_path, shuffle, repeat_mode, "
            "system, created_at, updated_at) VALUES('user-1', 'User', 'desc', NULL, 0, "
            "'none', 0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO playlist_tracks(playlist_id, track_id, position) "
            "VALUES('user-1', 'old-1', 0)"
        )
        conn.execute(
            "INSERT INTO history(track_id, started_at, title, artist) "
            "VALUES('old-1', '2026-01-01T00:00:00Z', 'Legacy', 'Artist')"
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('station_name', 'Old name')"
        )
        conn.commit()
    finally:
        conn.close()

    db.init_db(settings)
    conn = db.open_db(settings)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        track = conn.execute("SELECT * FROM tracks WHERE id = 'old-1'").fetchone()
        relation = conn.execute(
            "SELECT * FROM playlist_tracks WHERE track_id = 'old-1'"
        ).fetchone()
        history = conn.execute("SELECT * FROM history WHERE track_id = 'old-1'").fetchone()
        setting = db.get_setting(conn, "station_name")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)")}
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        unique = conn.execute("PRAGMA index_list(tracks)").fetchall()
        # The legacy UNIQUE(sha256) must be gone: a second row may share the hash.
        conn.execute(
            "INSERT INTO tracks(id, sha256, path, title, created_at, updated_at) "
            "VALUES('new-2', 'samehash', 'new-2.mp3', 'Second', "
            "'2026-01-02T00:00:00Z', '2026-01-02T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()

    assert version == db.SCHEMA_VERSION == 5
    assert track["path"] == "legacy.mp3"
    assert track["title"] == "Legacy"
    assert track["cover_path"] == "old-1.jpg"
    assert track["source_title"] == "Legacy"
    assert track["source_filename"] is None
    assert track["custom_cover_path"] is None
    assert relation["playlist_id"] == "user-1"
    assert history["title"] == "Legacy"
    assert setting == "Old name"
    assert {"source_filename", "custom_cover_path", "duplicate_of", "duplicate_kind",
            "duplicate_confirmed_at"} <= columns
    assert {"upload_batches", "pending_uploads", "export_jobs"} <= tables
    assert "idx_tracks_sha256" in indexes
    assert all(not (index["unique"] and index["origin"] == "u") for index in unique)
