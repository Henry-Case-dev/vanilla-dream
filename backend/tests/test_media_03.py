"""VD-RADIO-MEDIA-03: replace an existing track on a duplicate decision."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app import db, media, queue_engine, uploads
from tests.helpers import jpeg_bytes, make_playlist, play_next, write_mp3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _post(env, path: Path, batch_id: str | None = None):
    data = {"batch_id": batch_id} if batch_id else None
    return env.client.post(
        "/api/admin/tracks/upload",
        files={"file": (path.name, path.read_bytes(), "audio/mpeg")},
        data=data,
    )


def _added(env, path: Path, batch_id: str | None = None) -> dict:
    response = _post(env, path, batch_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "added"
    return body["track"]


def _pending(env, path: Path, batch_id: str | None = None) -> dict:
    response = _post(env, path, batch_id)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending"
    pending = body["pending"]
    pending["_batch_id"] = body["batch_id"]
    return pending


def _pending_state(env, pending: dict) -> dict:
    payload = env.client.get(f"/api/admin/uploads/{pending['_batch_id']}").json()["batch"]
    return next(item for item in payload["items"] if item["id"] == pending["id"])


def _decide(env, pending_id: str, action: str = "replace", track_id: str | None = None):
    body: dict = {"action": action}
    if track_id:
        body["track_id"] = track_id
    return env.client.post(f"/api/admin/uploads/{pending_id}/decide", json=body)


def _add(env, name: str, **kwargs) -> dict:
    return _added(
        env, write_mp3(env.settings.music_dir.parent / f"{name}.mp3", **kwargs)
    )


def _replacement(env, name: str, **kwargs) -> Path:
    return write_mp3(env.settings.music_dir.parent / f"{name}.mp3", **kwargs)


def _conn(env):
    return db.open_db(env.settings)


def _row(env, track_id: str):
    conn = _conn(env)
    try:
        return conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    finally:
        conn.close()


def _scalar(env, sql: str, params=()):
    conn = _conn(env)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def _setting(env, key: str):
    conn = _conn(env)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row is not None else None
    finally:
        conn.close()


def _retained(env, track_id: str | None = None) -> list:
    conn = _conn(env)
    try:
        if track_id:
            return conn.execute(
                "SELECT * FROM retained_files WHERE track_id = ? ORDER BY id",
                (track_id,),
            ).fetchall()
        return conn.execute("SELECT * FROM retained_files ORDER BY id").fetchall()
    finally:
        conn.close()


def _track_path(env, row) -> Path:
    path = Path(str(row["path"]))
    return path if path.is_absolute() else env.settings.music_dir / path


def _music_files(env) -> set[Path]:
    return {path for path in env.settings.music_dir.iterdir() if path.is_file()}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _use_playlist(env, playlist_id: str) -> None:
    response = env.client.put(
        "/api/admin/station/mode", json={"mode": "playlist", "playlist_id": playlist_id}
    )
    assert response.status_code == 200, response.text
    env.app.state.scheduler.tick()


def _start(env) -> None:
    response = env.client.post("/api/admin/station/start")
    assert response.status_code == 200, response.text
    env.app.state.scheduler.tick()


# ---------------------------------------------------------------------------
# Identity, relations and fields
# ---------------------------------------------------------------------------


def test_replace_preserves_identity_relations_and_updates_fields(env, admin):
    original = _add(
        env,
        "orig",
        title="Песня",
        artist="Исполнитель",
        album="Альбом",
        genre="Pop",
        year="2000",
        cover=True,
        seconds=1.0,
    )
    other = _add(env, "other", title="Другая", artist="Кто-то")
    playlist = make_playlist(env.client, "Мой", [original["id"], other["id"]])
    conn = _conn(env)
    try:
        conn.execute(
            "INSERT INTO history(track_id, started_at, title) "
            "VALUES(?, '2026-11-20T10:00:00Z', ?)",
            (original["id"], original["title"]),
        )
        conn.commit()
    finally:
        conn.close()

    old = _row(env, original["id"])
    old_path = _track_path(env, old)
    before_files = _music_files(env)
    replacement = _replacement(
        env,
        "orig-new",
        title="Песня",
        artist="Исполнитель",
        album="Новый альбом",
        genre="Rock",
        year="2010",
        cover=True,
        seconds=3.0,
    )
    pending = _pending(env, replacement)
    assert pending["can_replace"] is True
    assert pending["identical"] is False
    assert pending["replace_target"]["id"] == original["id"]
    assert [item["id"] for item in pending["replace_candidates"]] == [original["id"]]
    assert pending["replace_candidates"][0]["is_duplicate"] is False

    response = _decide(env, pending["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "replaced"
    assert body["replaced"] is True
    assert body["identical"] is False
    assert body["track_id"] == original["id"]
    assert body["engine_synced"] is True
    assert body["track"]["id"] == original["id"]
    assert body["pending"]["state"] == "replaced"
    assert body["pending"]["can_replace"] is False
    assert body["pending"]["decision_target"] == original["id"]
    assert "applied_revision" in body

    row = _row(env, original["id"])
    # Identity and every relation stay; no second library row.
    assert row["id"] == original["id"]
    assert row["created_at"] == old["created_at"]
    assert _scalar(env, "SELECT COUNT(*) FROM tracks") == 2
    assert (
        _scalar(
            env,
            "SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
            (playlist["id"], original["id"]),
        )
        == 1
    )
    conn = _conn(env)
    try:
        order = [
            item["track_id"]
            for item in conn.execute(
                "SELECT track_id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position",
                (playlist["id"],),
            ).fetchall()
        ]
    finally:
        conn.close()
    assert order == [original["id"], other["id"]]
    library = env.client.get("/api/admin/tracks", params={"q": "Песня"}).json()
    assert any(item["id"] == original["id"] for item in library["items"])
    assert _scalar(env, "SELECT COUNT(*) FROM history WHERE track_id = ?", (original["id"],)) == 1

    # New file fields.
    assert row["sha256"] == _sha(replacement.read_bytes())
    assert row["path"] != old["path"]
    assert row["path"].startswith(f"{original['id']}.")
    assert row["path"].endswith(".mp3")
    assert row["size"] == len(replacement.read_bytes())
    assert row["duration"] > 2.5
    assert row["duration"] == pytest.approx(body["track"]["duration"])
    assert row["bitrate"] and row["bitrate"] > 0
    assert row["ext"] == ".mp3"
    assert row["source_title"] == "Песня"
    assert row["source_artist"] == "Исполнитель"
    assert row["source_album"] == "Новый альбом"
    assert row["source_genre"] == "Rock"
    assert row["album"] == "Новый альбом"
    assert row["genre"] == "Rock"
    # Display year was not blank: it is never erased (no source_year column).
    assert row["year"] == 2000
    assert row["cover_path"] and row["cover_path"] != old["cover_path"]
    assert (env.settings.cover_dir / row["cover_path"]).exists()
    assert row["replaced_at"] is not None
    assert row["updated_at"] == row["replaced_at"]

    new_path = _track_path(env, row)
    assert new_path.exists()
    assert new_path.read_bytes() == replacement.read_bytes()
    # Old file is never overwritten and stays until released.
    assert old_path.exists()
    assert old_path.read_bytes() != new_path.read_bytes()
    retained = _retained(env, original["id"])
    assert len(retained) == 1
    assert retained[0]["path"] == old["path"]
    assert retained[0]["reason"] == "replace"
    assert len(_music_files(env)) == len(before_files) + 1


def test_replace_keeps_user_edits_and_custom_cover(env, admin):
    original = _add(
        env,
        "editable",
        title="Песня",
        artist="Исполнитель",
        album="Альбом",
        cover=True,
    )
    patched = env.client.patch(
        f"/api/admin/tracks/{original['id']}",
        json={"title": "Моё название", "album": "Мой альбом"},
    )
    assert patched.status_code == 200, patched.text
    custom = env.client.post(
        f"/api/admin/tracks/{original['id']}/cover",
        files={"file": ("cover.jpg", jpeg_bytes((40, 40), (10, 20, 30)), "image/jpeg")},
    )
    assert custom.status_code == 200, custom.text
    assert custom.json()["track"]["cover_kind"] == "custom"
    old = _row(env, original["id"])

    # Same title/artist tags so the comparison still detects the duplicate.
    replacement = _replacement(
        env,
        "editable-new",
        title="Песня",
        artist="Исполнитель",
        album="Новый альбом",
        genre="Rock",
        cover=True,
        seconds=2.0,
    )
    pending = _pending(env, replacement)
    body = _decide(env, pending["id"]).json()
    assert body["status"] == "replaced"
    row = _row(env, original["id"])

    assert row["title"] == "Моё название"
    assert row["album"] == "Мой альбом"
    assert row["artist"] == "Исполнитель"
    assert row["genre"] == "Rock"
    assert row["source_title"] == "Песня"
    assert row["source_album"] == "Новый альбом"
    assert row["custom_cover_path"] == old["custom_cover_path"]
    assert row["cover_path"] != old["cover_path"]
    assert (env.settings.cover_dir / row["cover_path"]).exists()
    assert not (env.settings.cover_dir / old["cover_path"]).exists()

    payload = body["track"]
    assert payload["cover_kind"] == "custom"
    assert payload.get("original_cover_url")
    # "Revert" returns the new file's extracted cover.
    reverted = env.client.delete(f"/api/admin/tracks/{original['id']}/cover")
    assert reverted.status_code == 200, reverted.text
    assert reverted.json()["track"]["cover_kind"] == "file"
    assert reverted.json()["track"]["cover_url"]


def test_identical_duplicate_is_noop_and_save_still_works(env, admin):
    source = env.settings.music_dir.parent / "same.mp3"
    write_mp3(source, title="Одинаковый", artist="Автор", cover=True)
    original = _added(env, source)
    files_before = _music_files(env)

    pending = _pending(env, source)
    assert pending["identical"] is True
    assert pending["can_replace"] is False

    response = _decide(env, pending["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "identical"
    assert body["replaced"] is False
    assert body["identical"] is True
    assert body["track_id"] == original["id"]
    assert _music_files(env) == files_before
    assert _retained(env) == []
    assert _scalar(env, "SELECT COUNT(*) FROM tracks") == 1
    assert _row(env, original["id"])["replaced_at"] is None

    # The pending item is untouched, so "save a copy" stays available.
    assert _pending_state(env, pending)["state"] == "pending"
    saved = _decide(env, pending["id"], "save")
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "saved"
    assert _scalar(env, "SELECT COUNT(*) FROM tracks") == 2


def test_replace_targets_only_the_selected_duplicate(env, admin):
    first = _add(env, "dup-a", title="Дубль", artist="Автор", album="Альбом", seconds=1.0)
    first_row = _row(env, first["id"])

    second_file = _replacement(
        env, "dup-b", title="Дубль", artist="Автор", album="Альбом", seconds=1.3
    )
    pending_copy = _pending(env, second_file)
    saved = _decide(env, pending_copy["id"], "save").json()
    second = saved["track"]
    assert second["duplicate_of"] == first["id"]
    second_row = _row(env, second["id"])

    new_file = _replacement(
        env, "dup-c", title="Дубль", artist="Автор", album="Альбом", seconds=2.0
    )
    pending = _pending(env, new_file)
    assert pending["replace_target"] is None
    assert {item["id"] for item in pending["replace_candidates"]} == {
        first["id"],
        second["id"],
    }
    ambiguous = _decide(env, pending["id"])
    assert ambiguous.status_code == 422
    assert ambiguous.json()["detail"]["code"] == "ambiguous_target"

    chosen = _decide(env, pending["id"], track_id=second["id"])
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["status"] == "replaced"
    assert chosen.json()["track_id"] == second["id"]

    untouched = _row(env, first["id"])
    assert untouched["sha256"] == first_row["sha256"]
    assert untouched["path"] == first_row["path"]
    assert untouched["updated_at"] == first_row["updated_at"]
    assert untouched["replaced_at"] is None
    changed = _row(env, second["id"])
    assert changed["sha256"] != second_row["sha256"]
    assert changed["path"] != second_row["path"]


def test_replace_keeps_historical_duplicate_marker(env, admin):
    original = _add(env, "marker-a", title="Метка", artist="Автор", seconds=1.0)
    copy_pending = _pending(
        env, _replacement(env, "marker-b", title="Метка", artist="Автор", seconds=1.4)
    )
    copy = _decide(env, copy_pending["id"], "save").json()["track"]
    before = _row(env, copy["id"])
    assert before["duplicate_of"] == original["id"]
    assert before["duplicate_kind"]
    assert before["duplicate_confirmed_at"]

    pending = _pending(
        env, _replacement(env, "marker-c", title="Метка", artist="Автор", seconds=2.0)
    )
    assert pending["replace_target"] is None
    chosen = _decide(env, pending["id"], track_id=copy["id"]).json()
    assert chosen["status"] == "replaced"
    after = _row(env, copy["id"])
    assert after["duplicate_of"] == before["duplicate_of"]
    assert after["duplicate_kind"] == before["duplicate_kind"]
    assert after["duplicate_confirmed_at"] == before["duplicate_confirmed_at"]
    assert after["created_at"] == before["created_at"]
    assert after["replaced_at"] is not None


# ---------------------------------------------------------------------------
# Playing, prepared and engine queue
# ---------------------------------------------------------------------------


def test_current_track_keeps_old_duration_while_playing(env, admin):
    current = _add(env, "live-a", title="Эфир", artist="Автор", seconds=1.0)
    following = _add(env, "live-b", title="Следующий", artist="Автор", seconds=1.0)
    playlist = make_playlist(
        env.client, "Эфир", [current["id"], following["id"]]
    )
    _use_playlist(env, playlist["id"])
    _start(env)
    started = play_next(env)
    assert started is not None and started["track"]["id"] == current["id"]

    conn = _conn(env)
    try:
        before = queue_engine.playback_payload(conn, env.settings, env.clock.now())
    finally:
        conn.close()
    assert before is not None and before["track"]["id"] == current["id"]
    old_duration = before["duration"]
    assert old_duration > 0

    replacement = _replacement(env, "live-new", title="Эфир", artist="Автор", seconds=4.0)
    pending = _pending(env, replacement)
    body = _decide(env, pending["id"]).json()
    assert body["status"] == "replaced"
    assert _row(env, current["id"])["duration"] > 3.5

    conn = _conn(env)
    try:
        after = queue_engine.playback_payload(conn, env.settings, env.clock.now())
    finally:
        conn.close()
    assert after["track"]["id"] == current["id"]
    assert after["duration"] == old_duration
    assert after["elapsed"] == before["elapsed"]
    assert after["position_id"] == before["position_id"]


def test_prepared_position_resolves_new_file_and_keeps_position_id(env, admin):
    first = _add(env, "prep-a", title="Первый", artist="Автор", seconds=1.0)
    second = _add(env, "prep-b", title="Второй", artist="Автор", seconds=1.0)
    playlist = make_playlist(env.client, "Подготовка", [first["id"], second["id"]])
    _use_playlist(env, playlist["id"])
    _start(env)
    play_next(env)

    queue = env.client.get("/api/admin/queue").json()
    target_item = next(
        item for item in queue["items"] if item["track"]["id"] == second["id"]
    )
    position_id = target_item["id"]
    assert target_item["prepared"] is True
    link = env.settings.queue_link_dir / f"{position_id}.mp3"
    assert link.is_symlink()
    old_link_target = link.resolve()
    old_row = _row(env, second["id"])

    replacement = _replacement(env, "prep-new", title="Второй", artist="Автор", seconds=3.0)
    pending = _pending(env, replacement)
    body = _decide(env, pending["id"]).json()
    assert body["status"] == "replaced"
    assert body["engine_synced"] is True

    new_row = _row(env, second["id"])
    new_path = _track_path(env, new_row).resolve()
    assert link.resolve() == new_path
    assert link.resolve() != old_link_target
    assert new_path.read_bytes() == replacement.read_bytes()

    engine_lines = [
        line
        for line in env.settings.engine_queue_file.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert any(line.split("\t", 1)[0] == position_id for line in engine_lines)
    queue_after = env.client.get("/api/admin/queue").json()
    assert any(
        item["id"] == position_id and item["track"]["id"] == second["id"]
        for item in queue_after["items"]
    )
    assert _row(env, second["id"])["play_count"] == old_row["play_count"]


def test_repeat_one_target_and_cycle_survive_replace(env, admin):
    first = _add(env, "rep-a", title="Повтор", artist="Автор", seconds=1.0)
    second = _add(env, "rep-b", title="Другой", artist="Автор", seconds=1.0)
    response = env.client.post(
        "/api/admin/playlists",
        json={"name": "Повтор", "repeat_mode": "one"},
    )
    assert response.status_code == 201, response.text
    playlist = response.json()["playlist"]
    added = env.client.post(
        f"/api/admin/playlists/{playlist['id']}/tracks",
        json={"track_ids": [first["id"], second["id"]]},
    )
    assert added.status_code == 200, added.text
    _use_playlist(env, playlist["id"])
    _start(env)
    play_next(env)

    def cycles() -> dict:
        conn = _conn(env)
        try:
            return {
                row["key"]: row["value"]
                for row in conn.execute(
                    "SELECT key, value FROM settings WHERE key LIKE '_cycle:%'"
                ).fetchall()
            }
        finally:
            conn.close()

    before = cycles()
    repeat_before = {
        key: json.loads(value)
        for key, value in before.items()
        if json.loads(value).get("repeat_one")
    }
    assert repeat_before
    assert any(
        state.get("repeat_target_id") == first["id"]
        for state in repeat_before.values()
    )

    replacement = _replacement(env, "rep-new", title="Повтор", artist="Автор", seconds=3.0)
    pending = _pending(env, replacement)
    body = _decide(env, pending["id"]).json()
    assert body["status"] == "replaced"

    after = cycles()
    for key, value in repeat_before.items():
        assert json.loads(after[key]) == value


# ---------------------------------------------------------------------------
# Export / download lifecycle
# ---------------------------------------------------------------------------


def test_export_snapshot_holds_old_path_until_job_ends(env, admin):
    track = _add(env, "exp", title="Экспорт", artist="Автор", seconds=1.0)
    old_row = _row(env, track["id"])
    old_path = _track_path(env, old_row)
    response = env.client.post(
        "/api/admin/exports",
        json={
            "scope": "tracks",
            "ids": [track["id"]],
            "format": "zip",
            "metadata": "original",
        },
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job"]["id"]
    conn = _conn(env)
    try:
        job = conn.execute("SELECT * FROM export_jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    snapshot = json.loads(job["snapshot"])
    assert Path(snapshot[0]["path"]) == old_path
    assert job["state"] in ("queued", "preparing", "ready", "downloading")

    pending = _pending(
        env, _replacement(env, "exp-new", title="Экспорт", artist="Автор", seconds=2.0)
    )
    assert _decide(env, pending["id"]).json()["status"] == "replaced"
    assert _retained(env, track["id"])

    env.app.state.scheduler.tick()
    assert old_path.exists()
    assert _retained(env, track["id"])

    cancelled = env.client.post(f"/api/admin/exports/{job_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    env.app.state.scheduler.tick()
    assert not old_path.exists()
    assert _retained(env, track["id"]) == []


def test_downloads_use_new_file_and_failed_unlink_is_retried(env, admin, monkeypatch):
    track = _add(env, "dl", title="Скачать", artist="Автор", seconds=1.0)
    old_row = _row(env, track["id"])
    old_path = _track_path(env, old_row)
    old_bytes = old_path.read_bytes()
    before = env.client.get(f"/api/admin/tracks/{track['id']}/download?mode=original")
    assert before.status_code == 200
    assert before.content == old_bytes

    replacement = _replacement(env, "dl-new", title="Скачать", artist="Автор", seconds=2.0)
    pending = _pending(env, replacement)
    assert _decide(env, pending["id"]).json()["status"] == "replaced"

    after = env.client.get(f"/api/admin/tracks/{track['id']}/download?mode=original")
    assert after.status_code == 200
    assert after.content == replacement.read_bytes()

    # A consumer that still holds the old file must not lose it: the failed
    # unlink keeps the row, the next pass retries and only then deletes it.
    handle = open(old_path, "rb")
    try:
        handle.seek(0)
        assert handle.read() == old_bytes
    finally:
        handle.close()

    original_unlink = Path.unlink

    def failing_unlink(self, missing_ok=False):
        if self == old_path:
            raise PermissionError("busy")
        return original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    conn = _conn(env)
    try:
        removed = media.cleanup_retained_files(conn, env.settings)
    finally:
        conn.close()
    assert removed == 0
    assert old_path.exists()
    assert _retained(env, track["id"])

    monkeypatch.undo()
    conn = _conn(env)
    try:
        removed = media.cleanup_retained_files(conn, env.settings)
    finally:
        conn.close()
    assert removed == 1
    assert not old_path.exists()
    assert _retained(env, track["id"]) == []


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_failed_copy_keeps_old_track_and_pending_retryable(env, admin, monkeypatch):
    track = _add(env, "failcopy", title="Копия", artist="Автор", seconds=1.0)
    old_row = _row(env, track["id"])
    old_path = _track_path(env, old_row)
    files_before = _music_files(env)

    pending = _pending(
        env, _replacement(env, "failcopy-new", title="Копия", artist="Автор", seconds=2.0)
    )

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(uploads, "_stage_replacement_file", boom)
    response = _decide(env, pending["id"])
    assert response.status_code == 507
    assert response.json()["detail"]["code"] == "not_enough_space"

    row = _row(env, track["id"])
    assert row["sha256"] == old_row["sha256"]
    assert row["path"] == old_row["path"]
    assert row["updated_at"] == old_row["updated_at"]
    assert old_path.exists()
    assert _music_files(env) == files_before
    assert _retained(env, track["id"]) == []
    assert _pending_state(env, pending)["state"] == "pending"

    monkeypatch.undo()
    retry = _decide(env, pending["id"])
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "replaced"
    assert _row(env, track["id"])["sha256"] != old_row["sha256"]


def test_failed_engine_sync_keeps_old_file_until_confirmed(env, admin):
    first = _add(env, "sync-a", title="Первый", artist="Автор", seconds=1.0)
    second = _add(env, "sync-b", title="Второй", artist="Автор", seconds=1.0)
    playlist = make_playlist(env.client, "Синх", [first["id"], second["id"]])
    _use_playlist(env, playlist["id"])
    _start(env)
    play_next(env)

    old_row = _row(env, second["id"])
    old_path = _track_path(env, old_row)
    replacement = _replacement(env, "sync-new", title="Второй", artist="Автор", seconds=3.0)
    pending = _pending(env, replacement)
    env.engine.sync_ok = False
    body = _decide(env, pending["id"]).json()
    assert body["status"] == "replaced"
    assert body["engine_synced"] is False
    assert body["application"] == "pending"
    assert _setting(env, "_engine_sync_error")
    assert old_path.exists()
    assert _retained(env, second["id"])

    env.app.state.scheduler.tick()
    assert old_path.exists()
    assert _retained(env, second["id"])

    env.engine.sync_ok = True
    env.app.state.scheduler.tick()
    env.app.state.scheduler.tick()
    assert _setting(env, "_engine_sync_error") is None
    assert not old_path.exists()
    assert _retained(env, second["id"]) == []


def test_disk_guard_blocks_replace(env, admin, monkeypatch):
    track = _add(env, "space", title="Место", artist="Автор", seconds=1.0)
    old_row = _row(env, track["id"])
    files_before = _music_files(env)
    pending = _pending(
        env, _replacement(env, "space-new", title="Место", artist="Автор", seconds=2.0)
    )
    monkeypatch.setattr(
        media,
        "disk_state",
        lambda settings: {"free_bytes": 0, "total_bytes": 0, "used_bytes": 0},
    )
    response = _decide(env, pending["id"])
    assert response.status_code == 507
    assert response.json()["detail"]["code"] == "not_enough_space"
    assert _row(env, track["id"])["sha256"] == old_row["sha256"]
    assert _music_files(env) == files_before
    assert _retained(env, track["id"]) == []


def test_stale_match_after_target_changed(env, admin):
    track = _add(env, "stale", title="Цель", artist="Автор", seconds=1.0)
    old_row = _row(env, track["id"])
    pending = _pending(
        env, _replacement(env, "stale-new", title="Цель", artist="Автор", seconds=2.0)
    )
    files_before = _music_files(env)

    conn = _conn(env)
    try:
        conn.execute(
            "UPDATE tracks SET sha256 = 'changed-hash', path = 'changed.mp3' WHERE id = ?",
            (track["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    response = _decide(env, pending["id"])
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "stale_match"
    assert detail["candidates"][0]["id"] == track["id"]
    assert _row(env, track["id"])["sha256"] == "changed-hash"
    assert _music_files(env) == files_before
    assert _pending_state(env, pending)["state"] == "pending"
    assert _retained(env, track["id"]) == []
    assert old_row["created_at"] == _row(env, track["id"])["created_at"]


def test_deleted_target_creates_no_new_track(env, admin):
    track = _add(env, "gone", title="Удаляемый", artist="Автор", seconds=1.0)
    pending = _pending(
        env, _replacement(env, "gone-new", title="Удаляемый", artist="Автор", seconds=2.0)
    )
    deleted = env.client.request(
        "DELETE", f"/api/admin/tracks/{track['id']}", json={"mode": "after"}
    )
    assert deleted.status_code == 200, deleted.text
    tracks_before = _scalar(env, "SELECT COUNT(*) FROM tracks")

    response = _decide(env, pending["id"])
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_match"
    assert _scalar(env, "SELECT COUNT(*) FROM tracks") == tracks_before
    assert _retained(env, track["id"]) == []


def test_repeated_replace_is_idempotent(env, admin):
    track = _add(env, "idem", title="Идемпотент", artist="Автор", seconds=1.0)
    pending = _pending(
        env, _replacement(env, "idem-new", title="Идемпотент", artist="Автор", seconds=2.0)
    )
    first = _decide(env, pending["id"]).json()
    assert first["status"] == "replaced"
    row_after = _row(env, track["id"])
    files_after = _music_files(env)
    retained_after = _retained(env, track["id"])

    second = _decide(env, pending["id"])
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["status"] == "replaced"
    assert body["replaced"] is True
    assert body["identical"] is False
    assert body["track_id"] == track["id"]
    assert _music_files(env) == files_after
    assert _retained(env, track["id"]) == retained_after
    assert _row(env, track["id"])["sha256"] == row_after["sha256"]
    assert _row(env, track["id"])["path"] == row_after["path"]


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


def test_batch_replace_mixed_outcomes_and_conflict(env, admin):
    target = _add(env, "batch-target", title="Цель", artist="Автор", seconds=1.0)
    other = _add(env, "batch-other", title="Другая", artist="Автор", seconds=1.0)
    same = _add(env, "batch-same", title="Идентичная", artist="Автор", seconds=1.0)
    stale = _add(env, "batch-stale", title="Устаревшая", artist="Автор", seconds=1.0)

    batch_id = "media03-batch"
    pending_ok = _pending(
        env,
        _replacement(env, "batch-ok", title="Цель", artist="Автор", seconds=2.0),
        batch_id,
    )
    pending_conflict = _pending(
        env,
        _replacement(env, "batch-conflict", title="Цель", artist="Автор", seconds=2.5),
        batch_id,
    )
    same_path = _track_path(env, _row(env, same["id"]))
    pending_identical = _pending(env, same_path, batch_id)
    pending_stale = _pending(
        env,
        _replacement(env, "batch-stale-new", title="Устаревшая", artist="Автор", seconds=2.0),
        batch_id,
    )
    assert pending_stale["replace_target"]["id"] == stale["id"]

    # A decision taken before the batch must never be overwritten by it.
    decided_pending = _pending(
        env,
        _replacement(env, "batch-saved-copy", title="Цель", artist="Автор", seconds=1.4),
        batch_id,
    )
    assert _decide(env, decided_pending["id"], "save").status_code == 200

    conn = _conn(env)
    try:
        conn.execute(
            "UPDATE tracks SET sha256 = 'mutated', path = 'mutated.mp3' WHERE id = ?",
            (stale["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    response = env.client.post(
        f"/api/admin/uploads/batch/{batch_id}/decide", json={"action": "replace"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["replaced"] == 1
    assert body["identical"] == 1
    assert body["conflicts"] == 1
    assert body["failed"] == 1
    statuses = {item["id"]: item["status"] for item in body["items"]}
    assert statuses[pending_ok["id"]] == "replaced"
    assert statuses[pending_conflict["id"]] == "conflict"
    assert statuses[pending_identical["id"]] == "identical"
    assert statuses[pending_stale["id"]] == "stale"
    assert decided_pending["id"] not in statuses
    assert body["engine_synced"] is True
    assert "state_revision" in body and "applied_revision" in body
    assert "application" in body

    target_row = _row(env, target["id"])
    assert target_row["sha256"] == _sha(
        (env.settings.music_dir.parent / "batch-ok.mp3").read_bytes()
    )
    assert _row(env, other["id"])["replaced_at"] is None
    saved_state = env.client.get(f"/api/admin/uploads/{batch_id}").json()
    saved_item = next(
        item for item in saved_state["batch"]["items"] if item["id"] == decided_pending["id"]
    )
    assert saved_item["state"] == "saved"
    assert saved_item["decision"] == "save"
    assert saved_state["batch"]["counts"]["replaced"] == 1

    # A repeated batch must not create extra copies/replaces.
    files_after = _music_files(env)
    retained_after = _retained(env)
    repeat = env.client.post(
        f"/api/admin/uploads/batch/{batch_id}/decide", json={"action": "replace"}
    )
    assert repeat.status_code == 200, repeat.text
    assert _music_files(env) == files_after
    assert _retained(env) == retained_after


# ---------------------------------------------------------------------------
# Cleanup, purge and transaction layout
# ---------------------------------------------------------------------------


def test_engine_sync_error_blocks_retained_cleanup(env, admin):
    track = _add(env, "clean", title="Очистка", artist="Автор", seconds=1.0)
    old_path = _track_path(env, _row(env, track["id"]))
    pending = _pending(
        env, _replacement(env, "clean-new", title="Очистка", artist="Автор", seconds=2.0)
    )
    assert _decide(env, pending["id"]).json()["status"] == "replaced"
    conn = _conn(env)
    try:
        db.set_setting(conn, "_engine_sync_error", "2026-11-23T09:00:00Z")
        conn.commit()
        assert media.cleanup_retained_files(conn, env.settings) == 0
        assert old_path.exists()
        db.delete_setting(conn, "_engine_sync_error")
        conn.commit()
        assert media.cleanup_retained_files(conn, env.settings) == 1
    finally:
        conn.close()
    assert not old_path.exists()
    assert _retained(env, track["id"]) == []


def test_purge_track_removes_retained_files(env, admin):
    track = _add(env, "purge", title="Удалить", artist="Автор", seconds=1.0)
    old_path = _track_path(env, _row(env, track["id"]))
    pending = _pending(
        env, _replacement(env, "purge-new", title="Удалить", artist="Автор", seconds=2.0)
    )
    assert _decide(env, pending["id"]).json()["status"] == "replaced"
    new_path = _track_path(env, _row(env, track["id"]))
    assert old_path.exists() and new_path.exists()

    conn = _conn(env)
    try:
        assert media.purge_track(conn, env.settings, track["id"]) is True
        conn.commit()
    finally:
        conn.close()
    assert not old_path.exists()
    assert not new_path.exists()
    assert _retained(env, track["id"]) == []
    assert _scalar(env, "SELECT COUNT(*) FROM tracks WHERE id = ?", (track["id"],)) == 0


def test_file_copy_happens_outside_any_transaction(env, admin, monkeypatch):
    track = _add(env, "txn", title="Транзакция", artist="Автор", seconds=1.0)
    pending = _pending(
        env, _replacement(env, "txn-new", title="Транзакция", artist="Автор", seconds=2.0)
    )
    conn = _conn(env)
    observed: list[bool] = []
    real = uploads._stage_replacement_file

    def spy(settings, source, ext, cover, track_id):
        observed.append(conn.in_transaction)
        return real(settings, source, ext, cover, track_id)

    monkeypatch.setattr(uploads, "_stage_replacement_file", spy)
    try:
        result = uploads.decide(
            conn, env.settings, pending["id"], "replace", env.clock.now()
        )
    finally:
        conn.close()
    assert result["status"] == "replaced"
    assert observed == [False]
    assert _row(env, track["id"])["replaced_at"] is not None


# ---------------------------------------------------------------------------
# Migration v4 -> v5
# ---------------------------------------------------------------------------


def test_migration_v4_to_v5_preserves_data(settings):
    conn = sqlite3.connect(str(settings.db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE tracks (
                id TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
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
                custom_cover_path TEXT,
                source_title TEXT,
                source_artist TEXT,
                source_album TEXT,
                source_genre TEXT,
                source_filename TEXT,
                duplicate_of TEXT,
                duplicate_kind TEXT,
                duplicate_confirmed_at TEXT,
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
            CREATE TABLE pending_uploads (
                id TEXT PRIMARY KEY, batch_id TEXT, filename TEXT, temp_path TEXT,
                ext TEXT, sha256 TEXT, size INTEGER, metadata TEXT, cover_temp TEXT,
                reason TEXT, match_track_id TEXT, matches TEXT,
                state TEXT NOT NULL DEFAULT 'pending', decision TEXT,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE playback_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                position_id TEXT, track_id TEXT, started_at TEXT, source_kind TEXT,
                source_ref TEXT, updated_at TEXT
            );
            CREATE TABLE export_jobs (
                id TEXT PRIMARY KEY, scope TEXT, scope_ref TEXT, track_ids TEXT,
                metadata_mode TEXT, format TEXT, state TEXT NOT NULL DEFAULT 'queued',
                processed INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
                archive_path TEXT, archive_name TEXT, size INTEGER, error TEXT,
                snapshot TEXT, created_at TEXT, updated_at TEXT, finished_at TEXT,
                expires_at TEXT
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        conn.execute("PRAGMA user_version = 4")
        conn.execute(
            "INSERT INTO tracks(id, sha256, path, ext, title, artist, album, genre, year, "
            "duration, bitrate, size, cover_path, custom_cover_path, source_title, "
            "source_artist, source_album, source_genre, source_filename, created_at, updated_at) "
            "VALUES('legacy-1', 'hash-legacy', 'legacy-1.mp3', '.mp3', 'Песня', 'Артист', "
            "'Альбом', 'Pop', 1999, 12.5, 128, 100, 'legacy-1.jpg', 'custom/legacy.jpg', "
            "'Песня', 'Артист', 'Альбом', 'Pop', 'legacy.mp3', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO playlists(id, name, description, shuffle, repeat_mode, system, "
            "created_at, updated_at) VALUES('user-1', 'Пользовательский', 'desc', 0, "
            "'none', 0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO playlist_tracks(playlist_id, track_id, position) "
            "VALUES('user-1', 'legacy-1', 0)"
        )
        conn.execute(
            "INSERT INTO history(track_id, started_at, title) "
            "VALUES('legacy-1', '2026-01-02T00:00:00Z', 'Песня')"
        )
        conn.execute(
            "INSERT INTO pending_uploads(id, filename, sha256, size, metadata, reason, "
            "match_track_id, matches, state, created_at, updated_at) VALUES('pending-1', "
            "'legacy-copy.mp3', 'hash-copy', 50, '{}', 'tags', 'legacy-1', "
            "'[{\"track_id\": \"legacy-1\", \"kind\": \"tags\"}]', 'pending', "
            "'2026-01-03T00:00:00Z', '2026-01-03T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO playback_state(singleton, track_id, started_at) "
            "VALUES(1, 'legacy-1', '2026-01-02T00:00:00Z')"
        )
        conn.execute("INSERT INTO settings(key, value) VALUES('station_name', 'Старое')")
        conn.commit()
    finally:
        conn.close()

    db.init_db(settings)
    conn = db.open_db(settings)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        track_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)")}
        pending_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(pending_uploads)")
        }
        playback_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(playback_state)")
        }
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        track = conn.execute("SELECT * FROM tracks WHERE id = 'legacy-1'").fetchone()
        pending = conn.execute(
            "SELECT * FROM pending_uploads WHERE id = 'pending-1'"
        ).fetchone()
        playback = conn.execute(
            "SELECT * FROM playback_state WHERE singleton = 1"
        ).fetchone()
        relation = conn.execute(
            "SELECT * FROM playlist_tracks WHERE track_id = 'legacy-1'"
        ).fetchone()
        history = conn.execute(
            "SELECT * FROM history WHERE track_id = 'legacy-1'"
        ).fetchone()
        setting = db.get_setting(conn, "station_name")
    finally:
        conn.close()

    assert version == db.SCHEMA_VERSION == 5
    assert {"replaced_at"} <= track_columns
    assert {"match_sha", "decision_target", "decision_result"} <= pending_columns
    assert "duration" in playback_columns
    assert "retained_files" in tables
    assert track["title"] == "Песня"
    assert track["path"] == "legacy-1.mp3"
    assert track["custom_cover_path"] == "custom/legacy.jpg"
    assert track["replaced_at"] is None
    assert pending["match_sha"] == "hash-legacy"
    assert pending["state"] == "pending"
    assert playback["duration"] is None
    assert relation["playlist_id"] == "user-1"
    assert history["title"] == "Песня"
    assert setting == "Старое"


def test_retained_table_is_additive_on_fresh_install(env, admin):
    assert _scalar(env, "SELECT COUNT(*) FROM retained_files") == 0
    assert _scalar(env, "PRAGMA user_version") == db.SCHEMA_VERSION
