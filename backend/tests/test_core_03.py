"""VD-RADIO-CORE-03: modes, sync v2, queue editing, lifecycle, editor, replenish."""

from __future__ import annotations

import io
import json
import threading
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from app import db, exports, media, queue_engine, uploads
from app.config import SYSTEM_PLAYLIST_ID
from app.util import parse_iso
from tests.helpers import make_playlist, play_next, upload_and_get, write_mp3


def _upload(env, name: str, title: str | None = None) -> dict:
    return upload_and_get(
        env.client,
        write_mp3(env.settings.music_dir.parent / f"{name}.mp3", title=title or name),
    )


def _state(env) -> dict:
    response = env.client.get("/api/admin/station/state")
    assert response.status_code == 200, response.text
    return response.json()


def _mode(env, mode: str, playlist_id: str | None = None) -> dict:
    body = {"mode": mode}
    if playlist_id:
        body["playlist_id"] = playlist_id
    response = env.client.put("/api/admin/station/mode", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _play(env, playlist_id: str, mode: str = "now", restart: bool = False) -> dict:
    response = env.client.post(
        f"/api/admin/playlists/{playlist_id}/play",
        json={"mode": mode, "restart": restart},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _conn_row(env, sql: str, params=()):
    conn = db.open_db(env.settings)
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _settings_update(env, **values):
    response = env.client.put("/api/admin/settings", json=values)
    assert response.status_code == 200, response.text
    return response.json()


def test_state_endpoint_and_permanent_playlist_selection(env, admin):
    first = _upload(env, "state-a", "A")
    second = _upload(env, "state-b", "B")
    playlist = make_playlist(env.client, "Program A", [first["id"], second["id"]])

    initial = _state(env)
    assert initial["mode"] == "playlist"
    assert initial["program_playlist_id"] == SYSTEM_PLAYLIST_ID
    assert initial["desired"] == "stopped"

    played = _play(env, playlist["id"], "now")
    assert played["started"] is True
    state = _state(env)
    assert state["mode"] == "playlist"
    assert state["selected_playlist_id"] == playlist["id"]
    assert state["program_playlist_id"] == playlist["id"]
    assert state["effective_source"]["playlist_id"] == playlist["id"]
    assert state["pending_source"] is None
    assert state["override"] is None
    assert state["state_revision"] >= initial["state_revision"]
    assert _conn_row(env, "SELECT COUNT(*) AS c FROM overrides WHERE active = 1")["c"] == 0

    playlist_payload = env.client.get(f"/api/admin/playlists/{playlist['id']}").json()["playlist"]
    assert playlist_payload["repeat_governed_by"] == "playlist"


def _engine_queue_ids(env) -> list[str]:
    lines = [
        line
        for line in env.settings.engine_queue_file.read_text(encoding="utf-8").splitlines()
        if line
    ]
    return [line.split("\t", 1)[0] for line in lines]


def _start_item(env, item) -> dict:
    link = env.settings.queue_link_dir / f"{item['id']}.mp3"
    response = env.client.post(
        "/api/internal/engine/track-start",
        data={"f": str(link), "ts": f"{env.clock.now().timestamp():.3f}"},
        headers={"X-Internal-Token": env.internal_token},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_next_keeps_program_until_track_end_then_applies(env, admin):
    track_a = _upload(env, "next-a", "Next A")
    track_b = _upload(env, "next-b", "Next B")
    playlist_a = make_playlist(env.client, "A", [track_a["id"]], repeat_mode="all")
    playlist_b = make_playlist(env.client, "B", [track_b["id"]], repeat_mode="all")
    _play(env, playlist_a["id"], "now")
    play_next(env)
    manual_one = _upload(env, "next-manual-1", "Manual 1")
    manual_two = _upload(env, "next-manual-2", "Manual 2")
    env.client.post(f"/api/admin/tracks/{manual_one['id']}/queue")
    env.client.post(f"/api/admin/tracks/{manual_two['id']}/queue")
    manual_ids = [
        item["id"]
        for item in env.client.get("/api/admin/queue").json()["items"]
        if item["manual"]
    ]
    assert len(manual_ids) == 2

    pending = _play(env, playlist_b["id"], "next")
    assert pending["status"] == "switching"
    state = _state(env)
    assert state["program_playlist_id"] == playlist_a["id"]
    assert state["pending_source"]["playlist_id"] == playlist_b["id"]
    items = env.client.get("/api/admin/queue").json()["items"]
    head = items[0]
    assert head["track"]["id"] == track_b["id"]
    assert state["pending_source"]["head_position_id"] == head["id"]
    assert [item["id"] for item in items if item["manual"]] == manual_ids
    # The engine already received the new head before the current track ends.
    engine_ids = _engine_queue_ids(env)
    assert engine_ids[0] == head["id"]
    assert engine_ids[1:] == manual_ids[: len(engine_ids) - 1]

    # The duration alone must not switch or rebuild the prepared queue.
    env.clock.advance(float(track_a["duration"] or 1.0) + 2.0)
    env.app.state.scheduler.tick()
    state = _state(env)
    assert state["program_playlist_id"] == playlist_a["id"]
    assert state["pending_source"]["head_position_id"] == head["id"]
    assert _engine_queue_ids(env)[0] == head["id"]

    _start_item(env, head)
    env.app.state.scheduler.tick()
    state = _state(env)
    assert state["program_playlist_id"] == playlist_b["id"]
    assert state["selected_playlist_id"] == playlist_b["id"]
    assert state["pending_source"] is None
    queued = env.client.get("/api/admin/queue").json()["items"]
    assert queued
    automatic = [item for item in queued if not item["manual"]]
    assert automatic and all(item["track"]["id"] == track_b["id"] for item in automatic)
    assert [item["id"] for item in queued if item["manual"]] == manual_ids


def test_rapid_playlist_next_requests_last_wins(env, admin):
    a = _upload(env, "rapid-a", "Rapid A")
    b = _upload(env, "rapid-b", "Rapid B")
    c = _upload(env, "rapid-c", "Rapid C")
    playlist_a = make_playlist(env.client, "Rapid A", [a["id"]], repeat_mode="all")
    playlist_b = make_playlist(env.client, "Rapid B", [b["id"]], repeat_mode="all")
    playlist_c = make_playlist(env.client, "Rapid C", [c["id"]], repeat_mode="all")
    _play(env, playlist_a["id"], "now")
    play_next(env)
    revision = _state(env)["state_revision"]

    _play(env, playlist_b["id"], "next")
    _play(env, playlist_c["id"], "next")
    state = _state(env)
    assert state["pending_source"]["playlist_id"] == playlist_c["id"]
    assert state["state_revision"] > revision
    items = env.client.get("/api/admin/queue").json()["items"]
    head = items[0]
    assert head["track"]["id"] == c["id"]
    assert state["pending_source"]["head_position_id"] == head["id"]
    assert _engine_queue_ids(env)[0] == head["id"]

    _start_item(env, head)
    env.app.state.scheduler.tick()
    assert _state(env)["program_playlist_id"] == playlist_c["id"]


def test_selecting_active_source_cancels_pending_switch(env, admin):
    track_a = _upload(env, "cancel-a", "Cancel A")
    track_b = _upload(env, "cancel-b", "Cancel B")
    playlist_a = make_playlist(env.client, "Cancel A", [track_a["id"]], repeat_mode="all")
    playlist_b = make_playlist(env.client, "Cancel B", [track_b["id"]], repeat_mode="all")
    _play(env, playlist_a["id"], "now")
    play_next(env)
    _play(env, playlist_b["id"], "next")
    assert _state(env)["pending_source"]["playlist_id"] == playlist_b["id"]
    assert env.client.get("/api/admin/queue").json()["items"][0]["track"]["id"] == track_b["id"]
    skips = env.engine.skip_calls

    again = _play(env, playlist_a["id"], "now")
    assert again["status"] == "applied"
    assert again["started"] is False
    assert env.engine.skip_calls == skips
    state = _state(env)
    assert state["pending_source"] is None
    assert state["program_playlist_id"] == playlist_a["id"]
    assert state["state_revision"] >= again["state_revision"]
    items = env.client.get("/api/admin/queue").json()["items"]
    assert items
    assert all(item["track"]["id"] == track_a["id"] for item in items)
    first_id = _engine_queue_ids(env)[0]
    assert first_id in {item["id"] for item in items}


def test_sync_v2_partial_and_missing_lines(env, admin):
    for index in range(3):
        _upload(env, f"syncv2-{index}", f"Sync v2 {index}")
    _settings_update(env, default_playlist_id=SYSTEM_PLAYLIST_ID)
    env.client.post("/api/admin/station/start")
    env.app.state.scheduler.tick()
    lines = [
        line
        for line in env.settings.engine_queue_file.read_text(encoding="utf-8").splitlines()
        if line
    ]
    ids = [line.split("\t", 1)[0] for line in lines]
    assert len(ids) >= 2
    env.engine.sync_report_result = {
        "ok": True,
        "reply": "\n".join(["OK", f"ok {ids[0]}", f"fail {ids[1]}", "END"]),
    }
    conn = db.open_db(env.settings)
    try:
        assert queue_engine.sync_engine(conn, env.settings, env.engine, force=True) is True
        states = {
            row["id"]: row["state"]
            for row in conn.execute(
                "SELECT id, state FROM queue_positions WHERE state IN ('pending','prepared')"
            ).fetchall()
        }
        assert states[ids[0]] == "prepared"
        assert states[ids[1]] == "pending"
        warnings = conn.execute(
            "SELECT COUNT(*) FROM events WHERE message LIKE '%не подготовил часть%'"
        ).fetchone()[0]
        assert warnings >= 1

        env.engine.sync_report_result = {
            "ok": True,
            "reply": "\n".join(["OK", f"ok {ids[0]}", "END"]),
        }
        assert queue_engine.sync_engine(conn, env.settings, env.engine, force=True) is False
        prepared = conn.execute(
            "SELECT COUNT(*) FROM queue_positions WHERE state = 'prepared'"
        ).fetchone()[0]
        assert prepared == 0
        assert db.get_setting(conn, "_engine_sync_error")
    finally:
        conn.close()


def test_queue_edits_prepared_move_delete_shuffle_clear(env, admin):
    tracks = [_upload(env, f"editq-{i}", f"Edit {i}") for i in range(6)]
    playlist = make_playlist(env.client, "Edit queue", [t["id"] for t in tracks])
    _play(env, playlist["id"], "now")
    manual = _upload(env, "editq-manual", "Manual")
    env.client.post(f"/api/admin/tracks/{manual['id']}/queue")
    items = env.client.get("/api/admin/queue").json()["items"]
    prepared = [item for item in items if item["prepared"]]
    assert prepared
    moved = env.client.patch(
        f"/api/admin/queue/{prepared[0]['id']}", json={"move_to": 3}
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["items"][3]["id"] == prepared[0]["id"]
    nexted = env.client.post(f"/api/admin/queue/{prepared[0]['id']}/move-next")
    assert nexted.status_code == 200
    assert nexted.json()["items"][0]["id"] == prepared[0]["id"]
    deleted = env.client.delete(f"/api/admin/queue/{prepared[0]['id']}")
    assert deleted.status_code == 200
    assert prepared[0]["id"] not in [item["id"] for item in deleted.json()["items"]]
    cleared = env.client.post("/api/admin/queue/clear-manual")
    assert cleared.status_code == 200
    assert not any(item["manual"] for item in cleared.json()["items"])

    current = env.client.get("/api/admin/queue").json()["current"]
    if current:
        started = env.client.patch(
            f"/api/admin/queue/{current['id']}", json={"move_to": 0}
        )
        assert started.status_code == 409
        assert started.json()["detail"]["code"] == "position_started"


def test_track_start_statuses_and_no_started_at_rewrite(env, admin):
    track = _upload(env, "events", "Events")
    playlist = make_playlist(env.client, "Events", [track["id"]], repeat_mode="all")
    _play(env, playlist["id"], "now")
    position = env.client.get("/api/admin/queue").json()["items"][0]
    url = "/api/internal/engine/track-start"
    headers = {"X-Internal-Token": env.internal_token}
    first = env.client.post(url, data={"vd": position["id"], "ts": "1700000000.5"}, headers=headers)
    duplicate = env.client.post(url, data={"vd": position["id"], "ts": "1700000000.5"}, headers=headers)
    stale = env.client.post(url, data={"vd": position["id"], "ts": "1699999999.0"}, headers=headers)
    unknown = env.client.post(url, data={"vd": "missing", "ts": "1700000001.0"}, headers=headers)
    assert first.json()["status"] == "ok"
    assert duplicate.json()["status"] == "duplicate"
    assert stale.json()["status"] == "stale"
    assert unknown.json()["status"] == "unknown"
    row = _conn_row(env, "SELECT started_at FROM history WHERE position_id = ?", (position["id"],))
    assert row["started_at"].startswith("2023-11-14T22:13:20")


def test_compute_state_does_not_mask_engine_with_empty_library(env):
    from app.station import compute_state

    now = env.clock.now()
    base = dict(
        desired="playing",
        unit_active=True,
        unit_state="failed",
        mount_present=False,
        has_current=False,
        active_since=now,
        now=now,
        recent_failures=0,
        has_library=False,
    )
    assert compute_state(**base) == "error"
    assert compute_state(**{**base, "unit_state": "active", "unit_active": False}) == "error"
    assert compute_state(
        **{**base, "unit_state": "active", "mount_present": False}
    ) == "starting"


def test_delete_modes_and_purge_failure_retry(env, admin, monkeypatch):
    first = _upload(env, "del-after", "Del after")
    second = _upload(env, "del-now", "Del now")
    playlist = make_playlist(env.client, "Delete", [first["id"], second["id"]], repeat_mode="all")
    _play(env, playlist["id"], "now")
    started = play_next(env)
    current_id = started["track"]["id"]
    current_path = env.settings.music_dir / _conn_row(
        env, "SELECT path FROM tracks WHERE id = ?", (current_id,)
    )["path"]

    after = env.client.request(
        "DELETE", f"/api/admin/tracks/{current_id}", json={"mode": "after"}
    )
    assert after.status_code == 200
    assert after.json()["pending"] is True
    assert current_path.exists()

    other = _upload(env, "del-other", "Other")
    original_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self == (env.settings.music_dir / _conn_row(
            env, "SELECT path FROM tracks WHERE id = ?", (other["id"],)
        )["path"]):
            raise PermissionError("busy")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    blocked = env.client.request(
        "DELETE", f"/api/admin/tracks/{other['id']}", json={"mode": "after"}
    )
    assert blocked.status_code == 200
    assert blocked.json()["pending"] is True
    assert _conn_row(env, "SELECT pending_delete FROM tracks WHERE id = ?", (other["id"],))["pending_delete"] == 1
    monkeypatch.setattr(Path, "unlink", original_unlink)

    conn = db.open_db(env.settings)
    try:
        media.delete_pending_after_change(conn, env.settings)
        conn.commit()
    finally:
        conn.close()
    assert _conn_row(env, "SELECT id FROM tracks WHERE id = ?", (other["id"],)) is None


def test_delete_prepared_waits_for_sync_confirmation(env, admin):
    tracks = [_upload(env, f"prep-del-{i}", f"Prepared {i}") for i in range(4)]
    playlist = make_playlist(
        env.client, "Prepared delete", [t["id"] for t in tracks], repeat_mode="all"
    )
    _settings_update(env, queue_depth=3)
    _play(env, playlist["id"], "now")
    current = play_next(env)
    conn = db.open_db(env.settings)
    try:
        row = conn.execute(
            "SELECT * FROM queue_positions WHERE state = 'prepared' "
            "AND track_id != ? ORDER BY seq LIMIT 1",
            (current["track"]["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    target_id = str(row["track_id"])
    target_path = env.settings.music_dir / _conn_row(
        env, "SELECT path FROM tracks WHERE id = ?", (target_id,)
    )["path"]

    env.engine.sync_ok = False
    response = env.client.request(
        "DELETE", f"/api/admin/tracks/{target_id}", json={"mode": "after"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] is False
    assert body["pending"] is True
    assert target_path.exists()
    assert (
        _conn_row(
            env, "SELECT pending_delete FROM tracks WHERE id = ?", (target_id,)
        )["pending_delete"]
        == 1
    )
    assert (
        _conn_row(
            env,
            "SELECT COUNT(*) AS c FROM queue_positions WHERE track_id = ?",
            (target_id,),
        )["c"]
        == 0
    )

    # A failed sync keeps the file even after the scheduler retries.
    env.app.state.scheduler.tick()
    assert target_path.exists()

    env.engine.sync_ok = True
    env.app.state.scheduler.tick()
    assert not target_path.exists()
    assert _conn_row(env, "SELECT id FROM tracks WHERE id = ?", (target_id,)) is None


def test_bulk_delete_keeps_file_when_unlink_fails(env, admin, monkeypatch):
    tracks = [_upload(env, f"bulk-fail-{i}", f"Bulk fail {i}") for i in range(3)]
    paths = {
        track["id"]: env.settings.music_dir
        / _conn_row(env, "SELECT path FROM tracks WHERE id = ?", (track["id"],))["path"]
        for track in tracks
    }
    original_unlink = Path.unlink
    failed_id = tracks[1]["id"]

    def failing_unlink(self, *args, **kwargs):
        if self == paths[failed_id]:
            raise PermissionError("busy")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    response = env.client.post(
        "/api/admin/tracks/bulk-delete",
        json={"ids": [track["id"] for track in tracks]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert sorted(body["deleted"]) == sorted(
        [tracks[0]["id"], tracks[2]["id"]]
    )
    assert body["pending"] == [failed_id]
    assert paths[failed_id].exists()
    assert _conn_row(
        env, "SELECT pending_delete FROM tracks WHERE id = ?", (failed_id,)
    )["pending_delete"] == 1
    assert _conn_row(env, "SELECT id FROM tracks WHERE id = ?", (tracks[0]["id"],)) is None

    monkeypatch.setattr(Path, "unlink", original_unlink)
    conn = db.open_db(env.settings)
    try:
        assert media.delete_pending_after_change(conn, env.settings) == [failed_id]
        conn.commit()
    finally:
        conn.close()
    assert not paths[failed_id].exists()
    assert _conn_row(env, "SELECT id FROM tracks WHERE id = ?", (failed_id,)) is None


def test_purge_waits_for_future_queue_positions(env, admin):
    track = _upload(env, "future-del", "Future delete")
    path = env.settings.music_dir / _conn_row(
        env, "SELECT path FROM tracks WHERE id = ?", (track["id"],)
    )["path"]
    conn = db.open_db(env.settings)
    try:
        conn.execute(
            "UPDATE tracks SET pending_delete = 1 WHERE id = ?", (track["id"],)
        )
        conn.execute(
            "INSERT INTO queue_positions(id, track_id, source_kind, source_ref, "
            "manual, seq, state, created_at) "
            "VALUES('future-position', ?, 'manual', 'manual:queue', 1, 0, 'pending', ?)",
            (track["id"], "2026-01-01T00:00:00Z"),
        )
        conn.commit()
        assert media.delete_pending_after_change(conn, env.settings) == []
        conn.commit()
    finally:
        conn.close()
    assert path.exists()

    conn = db.open_db(env.settings)
    try:
        conn.execute("DELETE FROM queue_positions WHERE id = 'future-position'")
        conn.commit()
        assert media.delete_pending_after_change(conn, env.settings) == [track["id"]]
        conn.commit()
    finally:
        conn.close()
    assert not path.exists()
    assert _conn_row(env, "SELECT id FROM tracks WHERE id = ?", (track["id"],)) is None


def test_upload_copy_runs_outside_write_transaction(env, admin, monkeypatch):
    conn = db.open_db(env.settings)
    original_copy = uploads.shutil.copyfileobj
    observed: list[bool] = []

    def checked_copy(source, destination, *args, **kwargs):
        assert not conn.in_transaction
        other = db.open_db(env.settings)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute(
                "UPDATE settings SET value = value WHERE key = 'station_name'"
            )
            other.commit()
            observed.append(True)
        finally:
            other.close()
        return original_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(uploads.shutil, "copyfileobj", checked_copy)
    try:
        data = write_mp3(
            env.settings.music_dir.parent / "outside-txn.mp3",
            title="Outside transaction",
            seconds=20,
        ).read_bytes()
        result = uploads.receive_upload(
            conn,
            env.settings,
            io.BytesIO(data),
            "outside-txn.mp3",
            env.clock.now(),
        )
        assert result["status"] == "added"
        assert observed
    finally:
        conn.close()


def test_concurrent_identical_uploads_one_track_one_pending(env):
    data = write_mp3(
        env.settings.music_dir.parent / "concurrent-upload.mp3",
        title="Concurrent",
        artist="Threads",
    ).read_bytes()
    outcomes = []
    errors = []

    def upload_once():
        conn = db.open_db(env.settings)
        try:
            result = uploads.receive_upload(
                conn,
                env.settings,
                io.BytesIO(data),
                "concurrent-upload.mp3",
                env.clock.now(),
            )
            outcomes.append(result["status"])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=upload_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not errors
    assert sorted(outcomes) == ["added", "pending"]
    assert _conn_row(env, "SELECT COUNT(*) AS c FROM tracks WHERE title = 'Concurrent'")["c"] == 1
    assert _conn_row(env, "SELECT COUNT(*) AS c FROM pending_uploads WHERE state = 'pending'")["c"] == 1


def test_atomic_editor_invalid_cover_keeps_everything(env, admin):
    track = _upload(env, "atomic", "Atomic")
    before = _conn_row(env, "SELECT * FROM tracks WHERE id = ?", (track["id"],))
    response = env.client.put(
        f"/api/admin/tracks/{track['id']}",
        data={"title": "Changed", "artist": "New", "album": "Album", "cover_action": "replace"},
        files={"cover": ("bad.png", b"not an image", "image/png")},
    )
    assert response.status_code == 422
    after = _conn_row(env, "SELECT * FROM tracks WHERE id = ?", (track["id"],))
    assert after["title"] == before["title"]
    assert after["custom_cover_path"] == before["custom_cover_path"]

    ok = env.client.put(
        f"/api/admin/tracks/{track['id']}",
        data={"title": "Changed", "artist": "New", "album": "Album", "cover_action": "replace"},
        files={"cover": ("cover.png", Path("tests/helpers.py").read_bytes()[:0] or b"", "image/png")},
    )
    # The empty image is invalid too; the unchanged-state assertion above is the
    # important part. A valid cover is covered by the media-02 tests.
    assert ok.status_code == 422


def test_export_snapshot_survives_edit_and_delete(env, admin):
    track = _upload(env, "export-snapshot", "Snapshot")
    conn = db.open_db(env.settings)
    try:
        status, payload = exports.create_export(
            conn,
            env.settings,
            {"scope": "tracks", "ids": [track["id"]], "format": "zip", "metadata": "tags"},
            env.clock.now(),
        )
        assert status == 202
        job_id = payload["job"]["id"]
    finally:
        conn.close()

    env.client.put(
        f"/api/admin/tracks/{track['id']}",
        data={"title": "Changed after export", "artist": "New", "album": "New"},
        files={"cover": ("none.bin", b"", "application/octet-stream")},
    )
    env.client.request("DELETE", f"/api/admin/tracks/{track['id']}", json={"mode": "after"})
    current = _conn_row(env, "SELECT * FROM tracks WHERE id = ?", (track["id"],))
    assert current is not None and current["pending_delete"] == 1

    exports.process_job(env.settings, job_id, env.clock.now())
    status = env.client.get(f"/api/admin/exports/{job_id}").json()["job"]
    assert status["state"] == "ready", status
    archive = env.client.get(f"/api/admin/exports/{job_id}/archive")
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zf:
        name = [n for n in zf.namelist() if n.endswith(".mp3")][0]
        assert "Snapshot" in name and "Changed after export" not in name
        assert zf.read(name)


def test_export_ttl_does_not_delete_downloading_archive(env, admin):
    track = _upload(env, "export-ttl", "TTL")
    conn = db.open_db(env.settings)
    try:
        _status, payload = exports.create_export(
            conn,
            env.settings,
            {"scope": "tracks", "ids": [track["id"]], "format": "zip"},
            env.clock.now(),
        )
        job_id = payload["job"]["id"]
    finally:
        conn.close()
    exports.process_job(env.settings, job_id, env.clock.now())
    conn = db.open_db(env.settings)
    try:
        ready = exports.get_job(conn, job_id)
        assert ready["state"] == "ready"
        archive = Path(_conn_row(env, "SELECT archive_path FROM export_jobs WHERE id = ?", (job_id,))["archive_path"])
        assert archive.exists()
        conn.execute(
            "UPDATE export_jobs SET expires_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00.000Z", job_id),
        )
        conn.commit()
        exports.mark_downloading(conn, job_id, env.clock.now())
        exports.cleanup_exports(conn, env.settings, env.clock.now())
        assert exports.get_job(conn, job_id)["state"] == "downloading"
        assert archive.exists()
        exports.finish_download(env.settings, job_id)
        conn2 = db.open_db(env.settings)
        try:
            exports.cleanup_exports(conn2, env.settings, env.clock.now())
        finally:
            conn2.close()
    finally:
        conn.close()
    assert not archive.exists()
    assert env.client.get(f"/api/admin/exports/{job_id}").status_code == 404


def test_shared_disk_guard_for_exports(env, admin):
    track = _upload(env, "disk-guard", "Disk")
    guarded = replace(env.settings, min_free_bytes=10**18)
    conn = db.open_db(env.settings)
    try:
        with pytest.raises(Exception) as caught:
            exports.create_export(
                conn,
                guarded,
                {"scope": "tracks", "ids": [track["id"]], "format": "zip"},
                env.clock.now(),
            )
        assert getattr(caught.value, "status_code", None) == 507
    finally:
        conn.close()


def test_atomic_editor_replaces_cover_and_text_together(env, admin):
    track = _upload(env, "atomic-ok", "Atomic OK")
    from tests.helpers import jpeg_bytes

    response = env.client.put(
        f"/api/admin/tracks/{track['id']}",
        data={"title": "Atomic new", "artist": "Artist", "album": "Album", "cover_action": "replace"},
        files={"cover": ("cover.png", jpeg_bytes((90, 60)), "image/png")},
    )
    assert response.status_code == 200, response.text
    payload = response.json()["track"]
    assert payload["title"] == "Atomic new"
    assert payload["cover_kind"] == "custom"
    assert _conn_row(env, "SELECT custom_cover_path FROM tracks WHERE id = ?", (track["id"],))[
        "custom_cover_path"
    ]


def test_original_cover_preview_uses_extracted_file(env, admin):
    from tests.helpers import jpeg_bytes
    woven = env.settings.music_dir.parent / "cover-original.mp3"
    track = upload_and_get(
        env.client,
        write_mp3(woven, title="Original cover", artist="Artist", cover=True),
    )
    extracted = _conn_row(
        env, "SELECT cover_path FROM tracks WHERE id = ?", (track["id"],)
    )["cover_path"]
    assert extracted
    original_bytes = (env.settings.cover_dir / extracted).read_bytes()
    listing = env.client.get(
        "/api/admin/tracks", params={"q": "Original cover"}
    ).json()["items"]
    payload = next(item for item in listing if item["id"] == track["id"])
    assert payload["cover_kind"] == "file"
    assert "original_cover_url" not in payload

    custom = env.client.post(
        f"/api/admin/tracks/{track['id']}/cover",
        files={"file": ("custom.jpg", jpeg_bytes((64, 64), (10, 20, 30)), "image/jpeg")},
    )
    assert custom.status_code == 200, custom.text
    payload = custom.json()["track"]
    assert payload["cover_kind"] == "custom"
    original_url = payload["original_cover_url"]
    assert original_url and original_url.endswith(
        f"/api/public/cover/{track['id']}?source=original"
    )

    original = env.client.get(
        f"/api/public/cover/{track['id']}", params={"source": "original"}
    )
    assert original.status_code == 200
    assert original.content == original_bytes
    assert original.headers["cache-control"] == "public, max-age=300"

    removed = env.client.delete(f"/api/admin/tracks/{track['id']}/cover")
    assert removed.status_code == 200
    payload = removed.json()["track"]
    assert "original_cover_url" not in payload

    plain = _upload(env, "cover-none", "No cover")
    plain_items = env.client.get(
        "/api/admin/tracks", params={"q": "No cover"}
    ).json()["items"]
    plain_payload = next(item for item in plain_items if item["id"] == plain["id"])
    assert "original_cover_url" not in plain_payload
    assert (
        env.client.get(
            f"/api/public/cover/{plain['id']}", params={"source": "original"}
        ).status_code
        == 404
    )


def _reverse_random(monkeypatch):
    class ReversingRandom:
        def shuffle(self, values):
            values.reverse()

        def choice(self, values):
            return values[0]

        def randrange(self, start, stop=None):
            return start

    monkeypatch.setattr(queue_engine.random, "SystemRandom", ReversingRandom)


def _queue_track_ids(env) -> list[str]:
    return [
        item["track"]["id"]
        for item in env.client.get("/api/admin/queue").json()["items"]
    ]


def test_shuffle_off_never_reorders_list(env, admin, monkeypatch):
    class ForbiddenRandom:
        def shuffle(self, values):
            raise AssertionError("shuffle must not be called when shuffle is off")

        def choice(self, values):
            raise AssertionError("random choice must not be used when shuffle is off")

        def randrange(self, start, stop=None):
            raise AssertionError("randrange must not be used when shuffle is off")

    tracks = [_upload(env, f"order-{i}", f"Order {i}") for i in range(5)]
    playlist = make_playlist(
        env.client, "Order", [t["id"] for t in tracks], shuffle=False, repeat_mode="all"
    )
    _settings_update(env, queue_depth=5)
    monkeypatch.setattr(queue_engine.random, "SystemRandom", ForbiddenRandom)
    _play(env, playlist["id"], "now")
    expected = [t["id"] for t in tracks]
    assert _queue_track_ids(env) == expected
    for _ in range(10):
        started = play_next(env)
        assert started is not None
        assert started["track"]["id"] == expected[0]
        expected = expected[1:] + expected[:1]
        queued = _queue_track_ids(env)
        assert queued == expected[: len(queued)]
        assert len(queued) == len(set(queued))


def test_shuffle_on_makes_one_pass_without_duplicates(env, admin, monkeypatch):
    _reverse_random(monkeypatch)
    tracks = [_upload(env, f"shuffle-pass-{i}", f"Shuffle {i}") for i in range(4)]
    playlist = make_playlist(
        env.client, "Shuffle pass", [t["id"] for t in tracks], shuffle=True, repeat_mode="all"
    )
    _settings_update(env, queue_depth=4)
    _play(env, playlist["id"], "now")
    queued = _queue_track_ids(env)
    assert queued == [t["id"] for t in reversed(tracks)]
    played = [play_next(env)["track"]["id"] for _ in range(4)]
    assert sorted(played) == sorted(t["id"] for t in tracks)
    assert len(set(played)) == 4


def test_shuffle_toggle_mid_pass_keeps_progress(env, admin, monkeypatch):
    tracks = [_upload(env, f"mid-{i}", f"Mid {i}") for i in range(5)]
    playlist = make_playlist(
        env.client, "Mid pass", [t["id"] for t in tracks], shuffle=False, repeat_mode="all"
    )
    _settings_update(env, queue_depth=4)
    _play(env, playlist["id"], "now")
    first = play_next(env)["track"]["id"]
    remaining = [t["id"] for t in tracks if t["id"] != first]

    _reverse_random(monkeypatch)
    changed = env.client.patch(
        f"/api/admin/playlists/{playlist['id']}", json={"shuffle": True}
    )
    assert changed.status_code == 200
    assert changed.json()["application"] == "applied"
    assert _queue_track_ids(env) == list(reversed(remaining))

    env.client.patch(f"/api/admin/playlists/{playlist['id']}", json={"shuffle": False})
    assert _queue_track_ids(env) == remaining


def test_repeat_one_target_skip_delete_and_exit_progress(env, admin):
    tracks = [_upload(env, f"one-{i}", f"One {i}") for i in range(3)]
    playlist = make_playlist(
        env.client, "One target", [t["id"] for t in tracks], repeat_mode="all"
    )
    _play(env, playlist["id"], "now")
    first = play_next(env)["track"]["id"]
    changed = env.client.patch(
        f"/api/admin/playlists/{playlist['id']}", json={"repeat_mode": "one"}
    )
    assert changed.status_code == 200
    conn = db.open_db(env.settings)
    try:
        state = db.get_json_setting(conn, f"_cycle:playlist:{playlist['id']}", {})
        assert state["repeat_target_id"] == first
        assert _queue_track_ids(env)
        assert all(track_id == first for track_id in _queue_track_ids(env))
        target_before = state["repeat_target_id"]
    finally:
        conn.close()

    skipped = env.client.post("/api/admin/station/skip")
    assert skipped.status_code == 200
    conn = db.open_db(env.settings)
    try:
        target_after = db.get_json_setting(
            conn, f"_cycle:playlist:{playlist['id']}", {}
        )["repeat_target_id"]
    finally:
        conn.close()
    assert target_after != target_before

    exited = env.client.patch(
        f"/api/admin/playlists/{playlist['id']}", json={"repeat_mode": "all"}
    )
    assert exited.status_code == 200
    assert _queue_track_ids(env)
    # Progress is not reset: the remaining ordinary pass continues with the
    # saved ids in order, not from an empty cycle.
    queued_after_exit = _queue_track_ids(env)
    assert queued_after_exit[:2] == [tracks[1]["id"], tracks[2]["id"]]
    resumed = play_next(env)
    assert resumed is not None and resumed["track"]["id"] == tracks[1]["id"]
    resumed_next = play_next(env)
    assert resumed_next is not None and resumed_next["track"]["id"] == tracks[2]["id"]


def test_repeat_one_works_for_system_playlist(env, admin):
    tracks = [_upload(env, f"sys-one-{i}", f"System one {i}") for i in range(2)]
    env.client.patch(
        "/api/admin/playlists/library", json={"shuffle": False, "repeat_mode": "one"}
    )
    _mode(env, "playlist", SYSTEM_PLAYLIST_ID)
    env.client.post("/api/admin/station/start")
    first = play_next(env)["track"]["id"]
    queued = _queue_track_ids(env)
    assert queued and all(track_id == first for track_id in queued)
    conn = db.open_db(env.settings)
    try:
        state = db.get_json_setting(conn, "_cycle:library", {})
        assert state.get("repeat_target_id") == first
    finally:
        conn.close()


def test_inactive_playlist_settings_do_not_touch_engine(env, admin):
    a_track = _upload(env, "prog-a", "Program A")
    b_track = _upload(env, "prog-b", "Program B")
    playlist_a = make_playlist(env.client, "A", [a_track["id"]])
    playlist_b = make_playlist(env.client, "B", [b_track["id"]])
    _play(env, playlist_a["id"], "now")
    play_next(env)
    calls = env.engine.sync_calls
    skips = env.engine.skip_calls
    restarts = len([c for c in env.runner.calls if c and c[-2] == "restart"])

    changed = env.client.patch(
        f"/api/admin/playlists/{playlist_b['id']}",
        json={"shuffle": True, "repeat_mode": "all"},
    )
    assert changed.status_code == 200
    assert changed.json()["application"] == "saved"
    assert env.engine.sync_calls == calls
    assert env.engine.skip_calls == skips
    assert len([c for c in env.runner.calls if c and c[-2] == "restart"]) == restarts
    state = _state(env)
    assert state["program_playlist_id"] == playlist_a["id"]
    payload_b = env.client.get(f"/api/admin/playlists/{playlist_b['id']}").json()["playlist"]
    assert payload_b["repeat_governed_by"] is None


def test_active_settings_change_never_skips_or_restarts(env, admin):
    track = _upload(env, "active-settings", "Active")
    playlist = make_playlist(env.client, "Active", [track["id"]], repeat_mode="all")
    _play(env, playlist["id"], "now")
    play_next(env)
    skips = env.engine.skip_calls
    restarts = len([c for c in env.runner.calls if c and c[-2] == "restart"])
    changed = env.client.patch(
        f"/api/admin/playlists/{playlist['id']}",
        json={"shuffle": True, "repeat_mode": "none"},
    )
    assert changed.status_code == 200
    assert changed.json()["application"] == "applied"
    assert env.engine.skip_calls == skips
    assert len([c for c in env.runner.calls if c and c[-2] == "restart"]) == restarts


def test_rotation_governs_repeat_in_payload(env, admin):
    a = _upload(env, "rot-payload-a", "Rotation A")
    playlist = make_playlist(env.client, "Rotation", [a["id"]], repeat_mode="all")
    env.client.put(
        "/api/admin/rotation",
        json={"mode": "sequential", "items": [{"playlist_id": playlist["id"], "position": 0}]},
    )
    _mode(env, "auto")
    env.app.state.scheduler.tick()
    payload = env.client.get(f"/api/admin/playlists/{playlist['id']}").json()["playlist"]
    assert payload["repeat_governed_by"] == "rotation"
    assert _state(env)["repeat_governed_by"] == "rotation"
    status = env.client.get("/api/admin/station/status").json()
    assert status["repeat_governed_by"] == "rotation"
    assert status["program_playlist_id"] == playlist["id"]


def test_active_user_playlist_replenish_keeps_current_pass(env, admin):
    tracks = [_upload(env, f"repl-old-{i}", f"Old {i}") for i in range(2)]
    playlist = make_playlist(
        env.client, "Replenish", [t["id"] for t in tracks], repeat_mode="all"
    )
    _settings_update(env, queue_depth=2)
    _play(env, playlist["id"], "now")
    first = play_next(env)["track"]["id"]
    new_tracks = [_upload(env, f"repl-new-{i}", f"New {i}") for i in range(2)]
    calls = env.engine.sync_calls
    added = env.client.post(
        f"/api/admin/playlists/{playlist['id']}/tracks",
        json={"track_ids": [t["id"] for t in new_tracks]},
    )
    assert added.status_code == 200, added.text
    assert added.json()["added"] == 2
    assert added.json()["application"] == "applied"
    assert added.json()["in_current_cycle"] is True
    assert env.engine.sync_calls == calls + 1

    conn = db.open_db(env.settings)
    try:
        state = db.get_json_setting(conn, f"_cycle:playlist:{playlist['id']}", {})
        assert state.get("cycle") is not None
        assert set(t["id"] for t in new_tracks) <= set(state.get("order") or [])
    finally:
        conn.close()
    queued = _queue_track_ids(env)
    assert any(t["id"] in queued for t in new_tracks)
    assert first not in queued  # played part does not return immediately


def test_inactive_or_duplicate_add_does_not_sync(env, admin):
    a = _upload(env, "repl-a", "Repl A")
    b = _upload(env, "repl-b", "Repl B")
    playlist_a = make_playlist(env.client, "Repl A", [a["id"]])
    playlist_b = make_playlist(env.client, "Repl B", [b["id"]])
    _play(env, playlist_a["id"], "now")
    play_next(env)
    calls = env.engine.sync_calls

    inactive = env.client.post(
        f"/api/admin/playlists/{playlist_b['id']}/tracks",
        json={"track_ids": [b["id"]]},
    )
    assert inactive.status_code == 200
    assert inactive.json()["added"] == 0
    assert env.engine.sync_calls == calls

    duplicate = env.client.post(
        f"/api/admin/playlists/{playlist_a['id']}/tracks",
        json={"track_ids": [a["id"]]},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["added"] == 0
    assert duplicate.json()["application"] == "saved"
    assert env.engine.sync_calls == calls


def test_empty_active_source_resumes_without_restart(env, admin):
    empty = env.client.post("/api/admin/playlists", json={"name": "Empty resume"}).json()["playlist"]
    _mode(env, "playlist", empty["id"])
    _settings_update(env, queue_depth=2)
    env.client.post("/api/admin/station/start")
    starts = len([c for c in env.runner.calls if c and c[-2] == "start"])
    track = _upload(env, "resume-track", "Resume")
    added = env.client.post(
        f"/api/admin/playlists/{empty['id']}/tracks", json={"track_ids": [track["id"]]}
    )
    assert added.status_code == 200
    assert added.json()["added"] == 1
    assert added.json()["application"] == "applied"
    assert _queue_track_ids(env) == [track["id"]]
    assert len([c for c in env.runner.calls if c and c[-2] == "start"]) == starts


def test_exhausted_repeat_none_plays_only_new_tracks(env, admin):
    old = _upload(env, "exhausted-old", "Old")
    playlist = make_playlist(env.client, "Exhausted", [old["id"]], repeat_mode="none")
    _settings_update(env, queue_depth=2)
    _play(env, playlist["id"], "now")
    assert play_next(env)["track"]["id"] == old["id"]
    env.app.state.scheduler.tick()
    assert _queue_track_ids(env) == []

    new_tracks = [_upload(env, f"exhausted-new-{i}", f"New {i}") for i in range(2)]
    added = env.client.post(
        f"/api/admin/playlists/{playlist['id']}/tracks", json={"track_ids": [t["id"] for t in new_tracks]}
    )
    assert added.status_code == 200
    queued = _queue_track_ids(env)
    assert old["id"] not in queued
    assert set(queued) == {t["id"] for t in new_tracks}


def test_repeat_one_add_keeps_target(env, admin):
    first = _upload(env, "one-add-a", "One add A")
    second = _upload(env, "one-add-b", "One add B")
    playlist = make_playlist(
        env.client, "One add", [first["id"], second["id"]], repeat_mode="one"
    )
    _settings_update(env, queue_depth=2)
    _play(env, playlist["id"], "now")
    play_next(env)
    conn = db.open_db(env.settings)
    try:
        target = db.get_json_setting(conn, f"_cycle:playlist:{playlist['id']}", {})[
            "repeat_target_id"
        ]
    finally:
        conn.close()
    third = _upload(env, "one-add-c", "One add C")
    added = env.client.post(
        f"/api/admin/playlists/{playlist['id']}/tracks", json={"track_ids": [third["id"]]}
    )
    assert added.status_code == 200
    conn = db.open_db(env.settings)
    try:
        state = db.get_json_setting(conn, f"_cycle:playlist:{playlist['id']}", {})
        assert state["repeat_target_id"] == target
        assert third["id"] in (state.get("order") or [])
    finally:
        conn.close()


def test_bulk_add_while_playing_uses_one_sync(env, admin):
    base = [_upload(env, f"bulk-base-{i}", f"Base {i}") for i in range(2)]
    playlist = make_playlist(env.client, "Bulk", [t["id"] for t in base], repeat_mode="all")
    _settings_update(env, queue_depth=2)
    _play(env, playlist["id"], "now")
    play_next(env)
    new_tracks = [_upload(env, f"bulk-new-{i}", f"New {i}") for i in range(4)]
    calls = env.engine.sync_calls
    added = env.client.post(
        f"/api/admin/playlists/{playlist['id']}/tracks",
        json={"track_ids": [t["id"] for t in new_tracks]},
    )
    assert added.status_code == 200
    assert added.json()["added"] == 4
    assert env.engine.sync_calls == calls + 1


def test_replenish_sync_failure_keeps_tracks_and_recovers(env, admin):
    base = _upload(env, "repl-fail-base", "Base")
    playlist = make_playlist(
        env.client, "Repl fail", [base["id"]], repeat_mode="none"
    )
    _settings_update(env, queue_depth=3)
    _play(env, playlist["id"], "now")
    play_next(env)
    new_track = _upload(env, "repl-fail-new", "Recovered")
    env.engine.sync_ok = False
    added = env.client.post(
        f"/api/admin/playlists/{playlist['id']}/tracks", json={"track_ids": [new_track["id"]]}
    )
    assert added.status_code == 200
    assert added.json()["application"] == "pending"
    assert _conn_row(
        env,
        "SELECT 1 FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
        (playlist["id"], new_track["id"]),
    )
    env.engine.sync_ok = True
    env.app.state.scheduler.tick()
    items = env.client.get("/api/admin/queue").json()["items"]
    assert any(
        item["track"]["id"] == new_track["id"] and item["prepared"] for item in items
    )


def test_upload_to_active_system_playlist_joins_cycle(env, admin):
    _mode(env, "playlist", SYSTEM_PLAYLIST_ID)
    _settings_update(env, queue_depth=2)
    env.client.post("/api/admin/station/start")
    track = _upload(env, "system-leak", "System leak")
    body = env.client.post(
        "/api/admin/tracks/upload",
        files={"file": ("system-leak-2.mp3", write_mp3(
            env.settings.music_dir.parent / "system-leak-2.mp3", title="System leak 2"
        ).read_bytes(), "audio/mpeg")},
    )
    assert body.status_code == 201
    assert body.json()["application"] == "applied"
    assert body.json()["in_current_cycle"] is True
    queued = _queue_track_ids(env)
    assert track["id"] in queued or any(
        item["track"]["id"] == track["id"] for item in env.client.get("/api/admin/queue").json()["items"]
    )


def test_migration_110_converts_old_playlist_override(settings):
    conn = db.open_db(settings)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS playlists (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
                cover_path TEXT, shuffle INTEGER DEFAULT 0,
                repeat_mode TEXT DEFAULT 'none', system INTEGER NOT NULL DEFAULT 0,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS overrides (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, playlist_id TEXT,
                scope TEXT NOT NULL, expires_at TEXT, active INTEGER DEFAULT 1,
                created_at TEXT, note TEXT
            );
            CREATE TABLE IF NOT EXISTS export_jobs (
                id TEXT PRIMARY KEY, scope TEXT, scope_ref TEXT, track_ids TEXT,
                metadata_mode TEXT, format TEXT, state TEXT NOT NULL DEFAULT 'queued',
                processed INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
                archive_path TEXT, archive_name TEXT, size INTEGER, error TEXT,
                created_at TEXT, updated_at TEXT, expires_at TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS schedule_rules (
                id TEXT PRIMARY KEY, name TEXT, playlist_id TEXT, rotation INTEGER DEFAULT 1,
                days TEXT NOT NULL, start_min INTEGER NOT NULL, end_min INTEGER NOT NULL,
                enabled INTEGER DEFAULT 1, priority INTEGER DEFAULT 0,
                transition TEXT DEFAULT 'after_track', created_at TEXT, updated_at TEXT
            );
            """
        )
        # Force the pre-1.2 export_jobs shape even if schema.sql created it.
        conn.execute("DROP TABLE export_jobs")
        conn.execute(
            "CREATE TABLE export_jobs (id TEXT PRIMARY KEY, scope TEXT, scope_ref TEXT, "
            "track_ids TEXT, metadata_mode TEXT, format TEXT, state TEXT NOT NULL DEFAULT 'queued', "
            "processed INTEGER DEFAULT 0, total INTEGER DEFAULT 0, archive_path TEXT, "
            "archive_name TEXT, size INTEGER, error TEXT, created_at TEXT, updated_at TEXT, "
            "expires_at TEXT)"
        )
        conn.execute("PRAGMA user_version = 3")
        stamp = "2026-01-01T00:00:00Z"
        conn.execute(
            "INSERT INTO playlists(id, name, shuffle, repeat_mode, system, created_at, updated_at) "
            "VALUES('old-program', 'Old program', 0, 'none', 0, ?, ?)",
            (stamp, stamp),
        )
        conn.execute(
            "INSERT INTO playlists(id, name, shuffle, repeat_mode, system, created_at, updated_at) "
            "VALUES('library', 'Вся медиатека', 1, 'all', 1, ?, ?)",
            (stamp, stamp),
        )
        conn.execute(
            "INSERT INTO overrides(id, kind, playlist_id, scope, expires_at, active, created_at, note) "
            "VALUES('launch', 'playlist', 'old-program', 'indefinite', NULL, 1, ?, "
            "'Запуск плейлиста из панели')",
            (stamp,),
        )
        conn.execute(
            "INSERT INTO overrides(id, kind, playlist_id, scope, expires_at, active, created_at, note) "
            "VALUES('temp', 'playlist', 'old-program', 'one_track', NULL, 1, ?, 'temporary')",
            (stamp,),
        )
        conn.commit()
    finally:
        conn.close()

    db.init_db(settings)
    db.init_db(settings)
    conn = db.open_db(settings)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        mode = db.get_setting(conn, "playback_mode")
        selected = db.get_setting(conn, "selected_playlist_id")
        launch = conn.execute("SELECT active FROM overrides WHERE id = 'launch'").fetchone()
        temp = conn.execute("SELECT active FROM overrides WHERE id = 'temp'").fetchone()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(export_jobs)")}
        assert db.get_setting(conn, "pending_source") == ""
        assert db.get_setting(conn, "state_revision") == "0"
    finally:
        conn.close()
    assert version == db.SCHEMA_VERSION == 5
    assert mode == "playlist"
    assert selected == "old-program"
    assert launch["active"] == 0
    assert temp["active"] == 1
    assert {"snapshot", "finished_at"} <= columns


def test_migration_110_infers_auto_when_configured(settings):
    db.init_db(settings)
    conn = db.open_db(settings)
    try:
        stamp = "2026-01-01T00:00:00Z"
        conn.execute(
            "INSERT INTO playlists(id, name, shuffle, repeat_mode, system, created_at, updated_at) "
            "VALUES('auto-program', 'Auto program', 0, 'all', 0, ?, ?)",
            (stamp, stamp),
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('default_playlist_id', 'auto-program') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        conn.execute("DELETE FROM settings WHERE key IN ('playback_mode', 'selected_playlist_id')")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    finally:
        conn.close()
    db.init_db(settings)
    conn = db.open_db(settings)
    try:
        assert db.get_setting(conn, "playback_mode") == "auto"
        assert db.get_setting(conn, "selected_playlist_id") == SYSTEM_PLAYLIST_ID
    finally:
        conn.close()


def test_migration_110_moves_override_refs_and_cycle(env, admin):
    tracks = [_upload(env, f"mig-ref-{i}", f"Migration {i}") for i in range(8)]
    playlist = make_playlist(
        env.client,
        "Migrated program",
        [track["id"] for track in tracks],
        repeat_mode="all",
    )
    playlist_id = playlist["id"]
    old_prefix = f"override:launch-ref:playlist:{playlist_id}"
    new_key = f"playlist:{playlist_id}"
    stamp = "2026-01-01T00:00:00Z"
    cycle = {
        "cycle": 1,
        "order": [track["id"] for track in tracks],
        "remaining": [tracks[6]["id"], tracks[7]["id"]],
        "started": True,
        "tail": tracks[1]["id"],
        "repeat_target_id": tracks[1]["id"],
    }
    positions = [
        ("mig-pos-c", tracks[2]["id"], "prepared", 0),
        ("mig-pos-d", tracks[3]["id"], "prepared", 1),
        ("mig-pos-e", tracks[4]["id"], "pending", 2),
        ("mig-pos-f", tracks[5]["id"], "pending", 3),
    ]
    conn = db.open_db(env.settings)
    try:
        conn.execute("DELETE FROM queue_positions")
        conn.execute("DELETE FROM playback_state")
        conn.execute(
            "DELETE FROM settings WHERE key LIKE '_cycle:%' "
            "OR key LIKE '_source_sig:%' OR key LIKE '_queue_excluded:%'"
        )
        conn.execute(
            "DELETE FROM settings WHERE key IN ('playback_mode', 'selected_playlist_id')"
        )
        conn.execute("PRAGMA user_version = 3")
        conn.execute(
            "INSERT INTO overrides(id, kind, playlist_id, scope, expires_at, active, "
            "created_at, note) VALUES('launch-ref', 'playlist', ?, 'indefinite', NULL, 1, ?, "
            "'Запуск плейлиста из панели')",
            (playlist_id, stamp),
        )
        db.set_json_setting(conn, f"_cycle:{old_prefix}", cycle)
        db.set_json_setting(
            conn,
            f"_source_sig:{old_prefix}",
            {
                "ids": [track["id"] for track in tracks],
                "shuffle": False,
                "repeat": "all",
            },
        )
        db.set_json_setting(
            conn,
            f"_queue_excluded:{old_prefix}:cycle:1",
            [tracks[1]["id"]],
        )
        for position_id, track_id, state, seq in positions:
            conn.execute(
                "INSERT INTO queue_positions(id, track_id, source_kind, source_ref, "
                "manual, seq, state, created_at) "
                "VALUES(?, ?, 'override', ?, 0, ?, ?, ?)",
                (position_id, track_id, f"{old_prefix}:cycle:1", seq, state, stamp),
            )
        conn.execute(
            "INSERT INTO playback_state(singleton, position_id, track_id, started_at, "
            "source_kind, source_ref, updated_at) "
            "VALUES(1, 'mig-current', ?, ?, 'override', ?, ?)",
            (tracks[0]["id"], stamp, f"{old_prefix}:cycle:1", stamp),
        )
        conn.execute(
            "INSERT INTO history(track_id, position_id, started_at, ended_at, "
            "source_kind, source_ref, title) "
            "VALUES(?, 'mig-played', ?, ?, 'override', ?, 'Played')",
            (tracks[0]["id"], stamp, stamp, f"{old_prefix}:cycle:1"),
        )
        conn.commit()
    finally:
        conn.close()

    db.init_db(env.settings)
    conn = db.open_db(env.settings)
    try:
        first_run = {
            "cycle": db.get_json_setting(conn, f"_cycle:{new_key}", {}),
            "signature": db.get_json_setting(conn, f"_source_sig:{new_key}", {}),
            "exclusion": db.get_json_setting(
                conn, f"_queue_excluded:{new_key}:cycle:1", []
            ),
            "refs": {
                row["id"]: row["source_ref"]
                for row in conn.execute(
                    "SELECT id, source_ref FROM queue_positions ORDER BY seq"
                )
            },
            "playback": conn.execute(
                "SELECT source_ref FROM playback_state WHERE singleton = 1"
            ).fetchone()["source_ref"],
        }
        assert db.get_setting(conn, "playback_mode") == "playlist"
        assert db.get_setting(conn, "selected_playlist_id") == playlist_id
        assert first_run["cycle"].get("remaining") == [
            tracks[6]["id"],
            tracks[7]["id"],
        ]
        assert first_run["cycle"].get("cycle") == 1
        assert first_run["signature"].get("ids") == [
            track["id"] for track in tracks
        ]
        assert db.get_json_setting(conn, f"_cycle:{old_prefix}", {}).get(
            "remaining"
        ) == cycle["remaining"]
        assert set(first_run["refs"]) == {item[0] for item in positions}
        assert all(
            ref == f"{new_key}:cycle:1" for ref in first_run["refs"].values()
        )
        assert first_run["playback"] == f"{new_key}:cycle:1"
        assert first_run["exclusion"] == [tracks[1]["id"]]
        assert db.get_setting(conn, f"_queue_excluded:{old_prefix}:cycle:1") == ""
        history_ref = conn.execute(
            "SELECT source_ref FROM history WHERE position_id = 'mig-played'"
        ).fetchone()["source_ref"]
        assert history_ref == f"{new_key}:cycle:1"
        assert db.get_setting(conn, "pending_source") == ""
    finally:
        conn.close()

    db.init_db(env.settings)
    conn = db.open_db(env.settings)
    try:
        second_run = {
            "cycle": db.get_json_setting(conn, f"_cycle:{new_key}", {}),
            "signature": db.get_json_setting(conn, f"_source_sig:{new_key}", {}),
            "exclusion": db.get_json_setting(
                conn, f"_queue_excluded:{new_key}:cycle:1", []
            ),
            "refs": {
                row["id"]: row["source_ref"]
                for row in conn.execute(
                    "SELECT id, source_ref FROM queue_positions ORDER BY seq"
                )
            },
            "playback": conn.execute(
                "SELECT source_ref FROM playback_state WHERE singleton = 1"
            ).fetchone()["source_ref"],
        }
        assert second_run == first_run
    finally:
        conn.close()

    # The first ensure_queue after the migration must keep the planned
    # positions and continue with the unplayed tail, never replay A/B.
    conn = db.open_db(env.settings)
    try:
        queue_engine.ensure_queue(conn, env.settings, env.clock.now())
        conn.commit()
        rows = conn.execute(
            "SELECT id, track_id, source_ref FROM queue_positions ORDER BY seq"
        ).fetchall()
        positions_after = {row["id"]: row["track_id"] for row in rows}
        assert {item[0] for item in positions} <= set(positions_after)
        queued_tracks = [row["track_id"] for row in rows]
        assert tracks[0]["id"] not in queued_tracks
        assert tracks[1]["id"] not in queued_tracks
        assert tracks[6]["id"] in queued_tracks
        assert tracks[7]["id"] in queued_tracks
        assert all(row["source_ref"].startswith(f"{new_key}:cycle:1") for row in rows)
        cycle_after = db.get_json_setting(conn, f"_cycle:{new_key}", {})
        assert cycle_after.get("remaining") == []
    finally:
        conn.close()


def test_init_db_keeps_override_created_after_upgrade(settings):
    db.init_db(settings)
    conn = db.open_db(settings)
    try:
        conn.execute(
            "INSERT INTO overrides(id, kind, playlist_id, scope, expires_at, active, "
            "created_at, note) VALUES('genuine', 'playlist', 'library', 'indefinite', "
            "NULL, 1, '2026-02-01T00:00:00Z', 'Запуск плейлиста из панели')"
        )
        conn.commit()
    finally:
        conn.close()
    db.init_db(settings)
    conn = db.open_db(settings)
    try:
        row = conn.execute(
            "SELECT active FROM overrides WHERE id = 'genuine'"
        ).fetchone()
        assert row is not None and row["active"] == 1
        assert db.get_setting(conn, "playback_mode") == "playlist"
    finally:
        conn.close()


def test_stale_sync_confirmation_is_ignored(env, admin, monkeypatch):
    _upload(env, "stale-sync", "Stale sync")
    _settings_update(env, default_playlist_id=SYSTEM_PLAYLIST_ID)
    env.client.post("/api/admin/station/start")
    env.app.state.scheduler.tick()
    original = env.engine.sync_report

    def stale_sync(expected_ids=None):
        conn = db.open_db(env.settings)
        try:
            queue_engine.bump_state_revision(conn)
            conn.commit()
        finally:
            conn.close()
        return original(expected_ids)

    monkeypatch.setattr(env.engine, "sync_report", stale_sync)
    conn = db.open_db(env.settings)
    try:
        prepared_before = conn.execute(
            "SELECT COUNT(*) FROM queue_positions WHERE state = 'prepared'"
        ).fetchone()[0]
        ok = queue_engine.sync_engine(conn, env.settings, env.engine, force=True)
        assert ok is False
        assert conn.execute(
            "SELECT COUNT(*) FROM queue_positions WHERE state = 'prepared'"
        ).fetchone()[0] == prepared_before
    finally:
        conn.close()


def test_unchanged_sync_records_applied_revision(env, admin):
    _upload(env, "dedupe-sync", "Dedupe sync")
    _settings_update(env, default_playlist_id=SYSTEM_PLAYLIST_ID)
    env.client.post("/api/admin/station/start")
    env.app.state.scheduler.tick()
    conn = db.open_db(env.settings)
    try:
        sync_state: dict = {}
        now = env.clock.now()
        assert queue_engine.sync_engine(
            conn, env.settings, env.engine, now=now, force=True, sync_state=sync_state
        )
        calls = env.engine.sync_calls
        queue_engine.bump_state_revision(conn)
        conn.commit()
        revision = queue_engine.state_revision(conn)
        assert queue_engine.sync_engine(
            conn,
            env.settings,
            env.engine,
            now=now,
            force=False,
            sync_state=sync_state,
        )
        assert env.engine.sync_calls == calls
        assert sync_state["applied_revision"] == revision
        assert db.get_setting(conn, "applied_revision") == str(revision)
    finally:
        conn.close()


def test_delete_target_repeat_one_chooses_next(env, admin):
    tracks = [_upload(env, f"del-target-{i}", f"Target {i}") for i in range(2)]
    playlist = make_playlist(
        env.client, "Delete target", [t["id"] for t in tracks], repeat_mode="one"
    )
    _settings_update(env, queue_depth=2)
    _play(env, playlist["id"], "now")
    play_next(env)
    env.client.request("DELETE", f"/api/admin/tracks/{tracks[0]['id']}", json={"mode": "after"})
    env.app.state.scheduler.tick()
    conn = db.open_db(env.settings)
    try:
        state = db.get_json_setting(conn, f"_cycle:playlist:{playlist['id']}", {})
        assert state.get("repeat_target_id") == tracks[1]["id"]
    finally:
        conn.close()


def test_delete_non_target_preserves_repeat_one_target(env, admin):
    tracks = [_upload(env, f"keep-target-{i}", f"Keep {i}") for i in range(3)]
    playlist = make_playlist(
        env.client, "Keep target", [t["id"] for t in tracks], repeat_mode="one"
    )
    _settings_update(env, queue_depth=2)
    _play(env, playlist["id"], "now")
    play_next(env)

    removed = env.client.delete(
        f"/api/admin/playlists/{playlist['id']}/tracks/{tracks[2]['id']}"
    )
    assert removed.status_code == 200, removed.text
    conn = db.open_db(env.settings)
    try:
        state = db.get_json_setting(conn, f"_cycle:playlist:{playlist['id']}", {})
        assert state.get("repeat_target_id") == tracks[0]["id"]
    finally:
        conn.close()

    env.client.request(
        "DELETE", f"/api/admin/tracks/{tracks[0]['id']}", json={"mode": "after"}
    )
    env.app.state.scheduler.tick()
    conn = db.open_db(env.settings)
    try:
        state = db.get_json_setting(conn, f"_cycle:playlist:{playlist['id']}", {})
        assert state.get("repeat_target_id") == tracks[1]["id"]
    finally:
        conn.close()


def test_now_switch_skips_after_sync_and_keeps_manual(env, admin):
    a_tracks = [_upload(env, f"now-a-{i}", f"Now A {i}") for i in range(2)]
    b_track = _upload(env, "now-b", "Now B")
    playlist_a = make_playlist(env.client, "Now A", [t["id"] for t in a_tracks], repeat_mode="all")
    playlist_b = make_playlist(env.client, "Now B", [b_track["id"]], repeat_mode="all")
    _settings_update(env, queue_depth=3)
    _play(env, playlist_a["id"], "now")
    play_next(env)
    manual = _upload(env, "now-manual", "Now Manual")
    env.client.post(f"/api/admin/tracks/{manual['id']}/play-next")
    skips = env.engine.skip_calls
    switched = _play(env, playlist_b["id"], "now")
    assert switched["status"] == "switching"
    assert switched["started"] is False
    assert env.engine.skip_calls == skips + 1
    items = env.client.get("/api/admin/queue").json()["items"]
    assert items[0]["track"]["id"] == b_track["id"]
    assert any(item["manual"] and item["track"]["id"] == manual["id"] for item in items)


def test_delete_now_skips_and_purges_after_next_start(env, admin):
    first = _upload(env, "dnow-a", "Now A")
    second = _upload(env, "dnow-b", "Now B")
    playlist = make_playlist(env.client, "Delete now", [first["id"], second["id"]], repeat_mode="all")
    _play(env, playlist["id"], "now")
    current = play_next(env)
    assert current["track"]["id"] == first["id"]
    path = env.settings.music_dir / _conn_row(
        env, "SELECT path FROM tracks WHERE id = ?", (first["id"],)
    )["path"]
    skips = env.engine.skip_calls
    deleted = env.client.request(
        "DELETE", f"/api/admin/tracks/{first['id']}", json={"mode": "now"}
    )
    assert deleted.status_code == 200
    assert deleted.json()["skipped"] is True
    assert deleted.json()["deleted"] is False
    assert deleted.json()["pending"] is True
    assert env.engine.skip_calls == skips + 1
    assert path.exists()

    # The playback state has not moved yet, so a scheduler tick must not purge.
    env.app.state.scheduler.tick()
    assert path.exists()

    queue = env.client.get("/api/admin/queue").json()
    position = queue["items"][0]
    link = env.settings.queue_link_dir / f"{position['id']}.mp3"
    env.client.post(
        "/api/internal/engine/track-start",
        data={"f": str(link), "ts": f"{env.clock.now().timestamp():.3f}"},
        headers={"X-Internal-Token": env.internal_token},
    )
    env.app.state.scheduler.tick()
    assert not path.exists()


def test_delete_last_track_completes_on_silence(env, admin):
    track = _upload(env, "last-track", "Last")
    playlist = make_playlist(env.client, "Last", [track["id"]], repeat_mode="none")
    _play(env, playlist["id"], "now")
    play_next(env)
    path = env.settings.music_dir / _conn_row(
        env, "SELECT path FROM tracks WHERE id = ?", (track["id"],)
    )["path"]
    env.client.request("DELETE", f"/api/admin/tracks/{track['id']}", json={"mode": "after"})
    env.icecast.payload = {
        "available": True,
        "mount_present": True,
        "listeners": 0,
        "bitrate": 128,
        "title": None,
        "error": None,
    }
    env.clock.advance(30)
    env.app.state.scheduler.tick()
    assert not path.exists()


def test_confirmed_duplicate_joins_active_system_playlist(env, admin):
    env.client.patch(
        "/api/admin/playlists/library",
        json={"repeat_mode": "none"},
    )
    _mode(env, "playlist", SYSTEM_PLAYLIST_ID)
    _settings_update(env, queue_depth=4)
    env.client.post("/api/admin/station/start")
    base_path = write_mp3(
        env.settings.music_dir.parent / "sysdup-base.mp3",
        title="System duplicate",
        artist="Artist",
    )
    base_data = base_path.read_bytes()
    base_response = env.client.post(
        "/api/admin/tracks/upload",
        files={"file": ("sysdup-base.mp3", base_data, "audio/mpeg")},
    )
    assert base_response.status_code == 201
    base_track = base_response.json()["track"]
    started = play_next(env)
    assert started is not None and started["track"]["id"] == base_track["id"]
    pending_response = env.client.post(
        "/api/admin/tracks/upload",
        files={"file": ("sysdup-copy.mp3", base_data, "audio/mpeg")},
    )
    assert pending_response.status_code == 202
    pending_id = pending_response.json()["pending"]["id"]
    saved = env.client.post(
        f"/api/admin/uploads/{pending_id}/decide", json={"action": "save"}
    )
    assert saved.status_code == 200, saved.text
    saved_track = saved.json()["track"]
    assert saved.json()["application"] == "applied"
    assert saved.json()["in_current_cycle"] is True
    queued = _queue_track_ids(env)
    assert saved_track["id"] in queued
    assert all(track_id == saved_track["id"] for track_id in queued)


def test_inactive_new_add_and_user_reorder_preserved(env, admin):
    a = _upload(env, "inactive-a", "Inactive A")
    b = _upload(env, "inactive-b", "Inactive B")
    playlist_a = make_playlist(env.client, "Inactive A", [a["id"]])
    playlist_b = make_playlist(env.client, "Inactive B", [b["id"]])
    _play(env, playlist_a["id"], "now")
    play_next(env)
    manual = _upload(env, "inactive-manual", "Manual")
    env.client.post(f"/api/admin/tracks/{manual['id']}/play-next")
    calls = env.engine.sync_calls
    new_track = _upload(env, "inactive-new", "Inactive new")
    added = env.client.post(
        f"/api/admin/playlists/{playlist_b['id']}/tracks",
        json={"track_ids": [new_track["id"]]},
    )
    assert added.status_code == 200
    assert added.json()["added"] == 1
    assert added.json()["application"] == "saved"
    assert env.engine.sync_calls == calls

    # Adding to the active playlist keeps the manual item ahead of new autos.
    active_add = env.client.post(
        f"/api/admin/playlists/{playlist_a['id']}/tracks",
        json={"track_ids": [new_track["id"]]},
    )
    assert active_add.status_code == 200
    items = env.client.get("/api/admin/queue").json()["items"]
    manual_index = next(i for i, item in enumerate(items) if item["manual"])
    auto_index = next(
        i for i, item in enumerate(items) if not item["manual"]
    )
    assert manual_index < auto_index


def test_shuffle_replenish_keeps_played_and_queued(env, admin, monkeypatch):
    tracks = [_upload(env, f"shrepl-{i}", f"Shuffle repl {i}") for i in range(3)]
    playlist = make_playlist(
        env.client, "Shuffle replenish", [t["id"] for t in tracks],
        shuffle=True, repeat_mode="all",
    )
    _settings_update(env, queue_depth=4)
    _play(env, playlist["id"], "now")
    first = play_next(env)["track"]["id"]
    new_tracks = [_upload(env, f"shrepl-new-{i}", f"New {i}") for i in range(2)]
    _reverse_random(monkeypatch)
    added = env.client.post(
        f"/api/admin/playlists/{playlist['id']}/tracks",
        json={"track_ids": [t["id"] for t in new_tracks]},
    )
    assert added.status_code == 200
    queued = _queue_track_ids(env)
    assert first not in queued
    assert any(t["id"] in queued for t in new_tracks)
    conn = db.open_db(env.settings)
    try:
        state = db.get_json_setting(conn, f"_cycle:playlist:{playlist['id']}", {})
        assert all(t["id"] in (state.get("order") or []) for t in new_tracks)
    finally:
        conn.close()
    assert len(queued) == len(set(queued))


def test_settings_save_keeps_playback_mode(env, admin):
    tracks = [_upload(env, f"settings-mode-{i}", f"Mode {i}") for i in range(2)]
    playlist = make_playlist(
        env.client, "Settings mode", [t["id"] for t in tracks], repeat_mode="all"
    )
    _settings_update(env, queue_depth=3)
    _play(env, playlist["id"], "now")
    play_next(env)
    before = _state(env)
    queue_before = _queue_track_ids(env)
    engine_before = _engine_queue_ids(env)

    response = env.client.put(
        "/api/admin/settings",
        json={"station_name": "Renamed station", "default_playlist_id": ""},
    )
    assert response.status_code == 200, response.text
    state = _state(env)
    assert state["mode"] == "playlist"
    assert state["selected_playlist_id"] == playlist["id"]
    assert state["program_playlist_id"] == playlist["id"]
    assert _queue_track_ids(env) == queue_before
    assert _engine_queue_ids(env) == engine_before
    assert _conn_row(
        env, "SELECT value FROM settings WHERE key = 'default_playlist_id'"
    )["value"] == ""

    _mode(env, "auto")
    configured = env.client.put(
        "/api/admin/settings", json={"default_playlist_id": playlist["id"]}
    )
    assert configured.status_code == 200, configured.text
    assert _conn_row(
        env, "SELECT value FROM settings WHERE key = 'default_playlist_id'"
    )["value"] == playlist["id"]
    assert _state(env)["mode"] == "auto"
    assert before["state_revision"] <= state["state_revision"]


def test_play_now_requires_sync_confirmation(env, admin):
    first = _upload(env, "manual-sync-a", "Manual A")
    second = _upload(env, "manual-sync-b", "Manual B")
    playlist = make_playlist(
        env.client, "Manual sync", [first["id"], second["id"]], repeat_mode="all"
    )
    _play(env, playlist["id"], "now")
    play_next(env)

    requested = _upload(env, "manual-sync-ok", "Manual OK")
    sync_calls = env.engine.sync_calls
    skips = env.engine.skip_calls
    ok = env.client.post(f"/api/admin/station/play-now/{requested['id']}")
    assert ok.status_code == 200, ok.text
    assert ok.json()["engine_synced"] is True
    assert env.engine.sync_calls == sync_calls + 1
    assert env.engine.skip_calls == skips + 1

    failing = _upload(env, "manual-sync-fail", "Manual fail")
    env.engine.sync_ok = False
    skips = env.engine.skip_calls
    failed = env.client.post(f"/api/admin/station/play-now/{failing['id']}")
    assert failed.status_code == 502
    assert failed.json()["detail"]["code"] == "engine_sync_failed"
    assert env.engine.skip_calls == skips
    assert _conn_row(
        env, "SELECT COUNT(*) AS c FROM queue_positions WHERE track_id = ?", (failing["id"],)
    )["c"] == 0
    played = _conn_row(
        env, "SELECT track_id FROM playback_state WHERE singleton = 1"
    )["track_id"]
    assert played != failing["id"]
    assert _conn_row(
        env, "SELECT COUNT(*) AS c FROM history WHERE track_id = ?", (failing["id"],)
    )["c"] == 0


def test_queue_shuffle_preserves_manual_order(env, admin):
    tracks = [_upload(env, f"manual-shuffle-{i}", f"Automatic {i}") for i in range(3)]
    playlist = make_playlist(
        env.client, "Manual shuffle", [t["id"] for t in tracks], repeat_mode="all"
    )
    _settings_update(env, queue_depth=5)
    _play(env, playlist["id"], "now")
    play_next(env)
    manual_one = _upload(env, "manual-order-1", "Manual 1")
    manual_two = _upload(env, "manual-order-2", "Manual 2")
    env.client.post(f"/api/admin/tracks/{manual_one['id']}/queue")
    env.client.post(f"/api/admin/tracks/{manual_two['id']}/queue")
    before = [
        item["track"]["id"]
        for item in env.client.get("/api/admin/queue").json()["items"]
        if item["manual"]
    ]
    assert before == [manual_one["id"], manual_two["id"]]

    shuffled = env.client.post("/api/admin/queue/shuffle")
    assert shuffled.status_code == 200, shuffled.text
    after = [
        item["track"]["id"]
        for item in shuffled.json()["items"]
        if item["manual"]
    ]
    assert after == before


def _revisions(env) -> tuple[int, int]:
    payload = env.client.get("/api/public/now-playing").json()
    return int(payload["queue_revision"]), int(payload["history_revision"])


def _arm_pending_switch(env):
    a_tracks = [_upload(env, f"pend-a-{i}", f"Pending A {i}") for i in range(2)]
    b_tracks = [_upload(env, f"pend-b-{i}", f"Pending B {i}") for i in range(2)]
    playlist_a = make_playlist(
        env.client, "Pending A", [t["id"] for t in a_tracks], repeat_mode="all"
    )
    playlist_b = make_playlist(
        env.client, "Pending B", [t["id"] for t in b_tracks], repeat_mode="all"
    )
    _settings_update(env, queue_depth=3)
    _play(env, playlist_a["id"], "now")
    play_next(env)
    pending = _play(env, playlist_b["id"], "next")
    assert pending["status"] == "switching"
    state = _state(env)
    head_id = state["pending_source"]["head_position_id"]
    head = next(
        item
        for item in env.client.get("/api/admin/queue").json()["items"]
        if item["id"] == head_id
    )
    return playlist_a, playlist_b, head


def _on_air(env) -> None:
    env.icecast.payload = {
        "available": True,
        "mount_present": True,
        "listeners": 1,
        "bitrate": 128,
        "title": None,
        "error": None,
    }


def test_delete_pending_head_cancels_switch(env, admin):
    playlist_a, playlist_b, head = _arm_pending_switch(env)
    before = _state(env)
    assert before["program_playlist_id"] == playlist_a["id"]
    assert before["pending_source"]["playlist_id"] == playlist_b["id"]

    response = env.client.delete(f"/api/admin/queue/{head['id']}")
    assert response.status_code == 200, response.text
    snapshot = response.json()
    assert snapshot["items"]
    state = _state(env)
    assert state["pending_source"] is None
    assert state["program_playlist_id"] == playlist_a["id"]
    items = env.client.get("/api/admin/queue").json()["items"]
    assert items
    assert all(item["source"]["playlist_id"] == playlist_a["id"] for item in items)
    assert head["id"] not in _engine_queue_ids(env)
    assert _conn_row(env, "SELECT value FROM settings WHERE key = '_engine_sync_error'") is None
    after = _state(env)

    _on_air(env)
    env.app.state.scheduler.tick()
    assert env.client.get("/api/admin/station/status").json()["state"] == "on_air"


def test_delete_pending_head_track_cancels_switch(env, admin):
    playlist_a, playlist_b, head = _arm_pending_switch(env)
    head_track = head["track"]["id"]
    response = env.client.request(
        "DELETE", f"/api/admin/tracks/{head_track}", json={"mode": "after"}
    )
    assert response.status_code == 200, response.text
    state = _state(env)
    assert state["pending_source"] is None
    assert state["program_playlist_id"] == playlist_a["id"]
    items = env.client.get("/api/admin/queue").json()["items"]
    assert items
    assert all(item["track"]["id"] != head_track for item in items)
    assert _conn_row(env, "SELECT value FROM settings WHERE key = '_engine_sync_error'") is None

    _on_air(env)
    env.app.state.scheduler.tick()
    assert env.client.get("/api/admin/station/status").json()["state"] == "on_air"


def test_delete_pending_target_playlist_cancels_switch(env, admin):
    playlist_a, playlist_b, head = _arm_pending_switch(env)
    response = env.client.delete(f"/api/admin/playlists/{playlist_b['id']}")
    assert response.status_code == 200, response.text
    state = _state(env)
    assert state["pending_source"] is None
    assert state["program_playlist_id"] == playlist_a["id"]
    assert _conn_row(
        env, "SELECT value FROM settings WHERE key = '_pending_target_cycle'"
    ) is None
    _on_air(env)
    env.app.state.scheduler.tick()
    assert env.client.get("/api/admin/station/status").json()["state"] == "on_air"


def test_station_mode_cancels_pending_and_restores_target_cycle(env, admin):
    a_track = _upload(env, "mode-cancel-a", "Mode cancel A")
    b_track = _upload(env, "mode-cancel-b", "Mode cancel B")
    playlist_a = make_playlist(
        env.client, "Mode cancel A", [a_track["id"]], repeat_mode="all"
    )
    playlist_b = make_playlist(
        env.client, "Mode cancel B", [b_track["id"]], repeat_mode="all"
    )
    expected = {
        "cycle": 7,
        "order": [b_track["id"]],
        "remaining": [b_track["id"]],
        "started": True,
        "tail": None,
    }
    conn = db.open_db(env.settings)
    try:
        db.set_json_setting(conn, f"_cycle:playlist:{playlist_b['id']}", expected)
        conn.commit()
    finally:
        conn.close()

    _settings_update(env, queue_depth=3)
    _play(env, playlist_a["id"], "now")
    play_next(env)
    _play(env, playlist_b["id"], "next")
    assert _state(env)["pending_source"]["playlist_id"] == playlist_b["id"]
    assert _conn_row(
        env, "SELECT value FROM settings WHERE key = '_pending_target_cycle'"
    ) is not None

    response = env.client.put(
        "/api/admin/station/mode",
        json={"mode": "playlist", "playlist_id": playlist_a["id"]},
    )
    assert response.status_code == 200, response.text
    state = _state(env)
    assert state["pending_source"] is None
    assert state["mode"] == "playlist"
    assert state["selected_playlist_id"] == playlist_a["id"]
    assert _conn_row(
        env, "SELECT value FROM settings WHERE key = '_pending_target_cycle'"
    ) is None
    conn = db.open_db(env.settings)
    try:
        restored = db.get_json_setting(conn, f"_cycle:playlist:{playlist_b['id']}", {})
    finally:
        conn.close()
    assert restored == expected


def test_missing_pending_head_is_recovered_by_tick(env, admin):
    playlist_a, playlist_b, head = _arm_pending_switch(env)
    conn = db.open_db(env.settings)
    try:
        conn.execute("DELETE FROM queue_positions WHERE id = ?", (head["id"],))
        conn.commit()
    finally:
        conn.close()

    _on_air(env)
    env.app.state.scheduler.tick()
    state = _state(env)
    assert state["pending_source"] is None
    assert state["program_playlist_id"] == playlist_a["id"]
    assert env.client.get("/api/admin/queue").json()["items"]
    assert _conn_row(env, "SELECT value FROM settings WHERE key = '_engine_sync_error'") is None
    assert env.client.get("/api/admin/station/status").json()["state"] == "on_air"


def test_queue_and_history_revisions(env, admin):
    tracks = [_upload(env, f"rev-{i}", f"Revision {i}") for i in range(4)]
    playlist = make_playlist(
        env.client, "Revisions", [t["id"] for t in tracks], repeat_mode="all"
    )
    _settings_update(env, queue_depth=4)
    _play(env, playlist["id"], "now")
    before_queue, before_history = _revisions(env)
    play_next(env)
    after_queue, after_history = _revisions(env)
    assert after_queue > before_queue
    assert after_history > before_history
    assert _conn_row(
        env, "SELECT value FROM settings WHERE key = 'queue_revision'"
    )["value"] == str(after_queue)
    assert _conn_row(
        env, "SELECT value FROM settings WHERE key = 'history_revision'"
    )["value"] == str(after_history)
    assert _revisions(env) == (after_queue, after_history)

    items = env.client.get("/api/admin/queue").json()["items"]
    assert items
    moved = env.client.patch(
        f"/api/admin/queue/{items[0]['id']}", json={"move_to": len(items) - 1}
    )
    assert moved.status_code == 200, moved.text
    queue_after_move, history_after_move = _revisions(env)
    assert queue_after_move > after_queue
    assert history_after_move == after_history

    victim = env.client.get("/api/admin/queue").json()["items"][0]
    deleted = env.client.delete(f"/api/admin/queue/{victim['id']}")
    assert deleted.status_code == 200, deleted.text
    queue_after_delete, history_after_delete = _revisions(env)
    assert queue_after_delete > queue_after_move
    assert history_after_delete == history_after_move

    shuffled = env.client.post("/api/admin/queue/shuffle")
    assert shuffled.status_code == 200, shuffled.text
    queue_after_shuffle, history_after_shuffle = _revisions(env)
    assert queue_after_shuffle > queue_after_delete
    assert history_after_shuffle == history_after_delete

    manual = _upload(env, "rev-manual", "Manual revision")
    env.client.post(f"/api/admin/tracks/{manual['id']}/queue")
    queue_after_add, history_after_add = _revisions(env)
    cleared = env.client.post("/api/admin/queue/clear-manual")
    assert cleared.status_code == 200, cleared.text
    queue_after_clear, history_after_clear = _revisions(env)
    assert queue_after_clear > queue_after_add
    assert history_after_clear == history_after_add

    conn = db.open_db(env.settings)
    try:
        assert queue_engine.finish_silence(conn, env.clock.now()) is True
        conn.commit()
    finally:
        conn.close()
    queue_after_silence, history_after_silence = _revisions(env)
    assert queue_after_silence > queue_after_clear
    assert history_after_silence > history_after_clear


def test_track_payload_playlists_count(env, admin):
    first = _upload(env, "member-first", "Member first")
    second = _upload(env, "member-second", "Member second")
    playlist_one = make_playlist(env.client, "Count one", [first["id"]])
    playlist_two = make_playlist(env.client, "Count two", [first["id"], second["id"]])

    listing = env.client.get(
        "/api/admin/tracks", params={"q": "Member first"}
    ).json()["items"]
    assert listing[0]["playlists_count"] == 2
    other = env.client.get(
        "/api/admin/tracks", params={"q": "Member second"}
    ).json()["items"]
    assert other[0]["playlists_count"] == 1

    detail = env.client.get(f"/api/admin/playlists/{playlist_one['id']}").json()
    assert detail["playlist"]["tracks"][0]["playlists_count"] == 2

    removed = env.client.delete(
        f"/api/admin/playlists/{playlist_one['id']}/tracks/{first['id']}"
    )
    assert removed.status_code == 200
    listing = env.client.get(
        "/api/admin/tracks", params={"q": "Member first"}
    ).json()["items"]
    assert listing[0]["playlists_count"] == 1
    env.client.delete(f"/api/admin/playlists/{playlist_two['id']}/tracks/{first['id']}")
    listing = env.client.get(
        "/api/admin/tracks", params={"q": "Member first"}
    ).json()["items"]
    assert listing[0]["playlists_count"] == 0

    library_only = _upload(env, "library-only-count", "Library only")
    listing = env.client.get(
        "/api/admin/tracks", params={"q": "Library only"}
    ).json()["items"]
    assert listing[0]["playlists_count"] == 0


def test_settings_survive_restart(env, admin):
    track = _upload(env, "persist", "Persist")
    playlist = make_playlist(env.client, "Persist", [track["id"]], shuffle=False, repeat_mode="all")
    env.client.patch(
        f"/api/admin/playlists/{playlist['id']}",
        json={"shuffle": True, "repeat_mode": "one"},
    )
    from fastapi.testclient import TestClient
    from app.main import create_app

    app2 = create_app(
        settings=env.settings,
        clock=env.clock,
        station=env.station,
        engine=env.engine,
        icecast=env.icecast,
    )
    with TestClient(app2) as client2:
        login = client2.post("/api/auth/login", json={"password": env.admin_password})
        assert login.status_code == 200
        payload = client2.get(f"/api/admin/playlists/{playlist['id']}").json()["playlist"]
        assert payload["shuffle"] is True
        assert payload["repeat_mode"] == "one"


def test_engine_instance_identity_keeps_or_resets_prepared(env, admin, monkeypatch):
    tracks = [_upload(env, f"ident-{i}", f"Identity {i}") for i in range(3)]
    playlist = make_playlist(
        env.client, "Identity", [t["id"] for t in tracks], repeat_mode="all"
    )
    _settings_update(env, queue_depth=2)
    _play(env, playlist["id"], "now")
    play_next(env)
    conn = db.open_db(env.settings)
    try:
        prepared = {
            str(row["id"])
            for row in conn.execute(
                "SELECT id FROM queue_positions WHERE state = 'prepared'"
            ).fetchall()
        }
        assert prepared
        current = conn.execute(
            "SELECT position_id, track_id FROM playback_state WHERE singleton = 1"
        ).fetchone()
        assert current is not None and current["track_id"]
        instance = db.get_setting(conn, "_engine_instance")
        assert instance
        db.set_setting(conn, "desired_state", "stopped")
        conn.commit()
    finally:
        conn.close()

    from fastapi.testclient import TestClient
    from app.main import create_app

    same = create_app(
        settings=env.settings,
        clock=env.clock,
        station=env.station,
        engine=env.engine,
        icecast=env.icecast,
    )
    with TestClient(same):
        conn = db.open_db(env.settings)
        try:
            kept = {
                str(row["id"])
                for row in conn.execute(
                    "SELECT id FROM queue_positions WHERE state = 'prepared'"
                ).fetchall()
            }
            assert kept == prepared
            playback = conn.execute(
                "SELECT track_id FROM playback_state WHERE singleton = 1"
            ).fetchone()
            assert playback is not None and playback["track_id"] == current["track_id"]
            assert db.get_setting(conn, "_engine_instance") == instance
        finally:
            conn.close()

    monkeypatch.setattr(
        env.station, "unit_last_start", lambda unit=None: "Tue 2026-11-24 10:00:00 UTC"
    )
    changed = create_app(
        settings=env.settings,
        clock=env.clock,
        station=env.station,
        engine=env.engine,
        icecast=env.icecast,
    )
    with TestClient(changed):
        conn = db.open_db(env.settings)
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM queue_positions WHERE state = 'prepared'"
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute("SELECT COUNT(*) FROM playback_state").fetchone()[0] == 0
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM queue_positions WHERE id = ?",
                    (current["position_id"],),
                ).fetchone()[0]
                == 0
            )
            assert (
                db.get_setting(conn, "_engine_instance")
                == "Tue 2026-11-24 10:00:00 UTC|"
            )
        finally:
            conn.close()


def test_scheduler_detects_engine_restart_and_resyncs_once(env, admin, monkeypatch):
    tracks = [_upload(env, f"tick-restart-{i}", f"Tick {i}") for i in range(4)]
    playlist = make_playlist(
        env.client, "Tick restart", [t["id"] for t in tracks], repeat_mode="all"
    )
    _settings_update(env, queue_depth=3)
    _play(env, playlist["id"], "now")
    play_next(env)
    conn = db.open_db(env.settings)
    try:
        current = conn.execute(
            "SELECT position_id, track_id FROM playback_state WHERE singleton = 1"
        ).fetchone()
        assert current is not None and current["position_id"]
        prepared_before = conn.execute(
            "SELECT COUNT(*) FROM queue_positions WHERE state = 'prepared'"
        ).fetchone()[0]
        assert prepared_before > 0
    finally:
        conn.close()

    sync_calls = env.engine.sync_calls
    monkeypatch.setattr(
        env.station,
        "unit_last_start",
        lambda unit=None: "Thu 2026-11-26 10:00:00 UTC",
    )
    env.icecast.payload = {
        "available": True,
        "mount_present": False,
        "listeners": 0,
        "bitrate": None,
        "title": None,
        "error": None,
    }
    env.clock.advance(1)
    env.app.state.scheduler.tick()

    assert env.engine.sync_calls == sync_calls + 1
    conn = db.open_db(env.settings)
    try:
        assert db.get_setting(conn, "_engine_instance", "").startswith(
            "Thu 2026-11-26 10:00:00 UTC|"
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM playback_state").fetchone()[0] == 0
        )
        history = conn.execute(
            "SELECT ended_at FROM history WHERE position_id = ?",
            (current["position_id"],),
        ).fetchone()
        assert history is not None and history["ended_at"]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM queue_positions WHERE id = ?",
                (current["position_id"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM queue_positions WHERE state = 'prepared'"
            ).fetchone()[0]
            > 0
        )
    finally:
        conn.close()

    # A second tick with the same instance performs no second detection or
    # duplicate resync.
    env.clock.advance(1)
    env.app.state.scheduler.tick()
    assert env.engine.sync_calls == sync_calls + 1


def test_tick_caches_engine_instance_and_keeps_unchanged_state(env, admin):
    tracks = [_upload(env, f"tick-stable-{i}", f"Stable {i}") for i in range(3)]
    playlist = make_playlist(
        env.client, "Tick stable", [t["id"] for t in tracks], repeat_mode="all"
    )
    _settings_update(env, queue_depth=3)
    _play(env, playlist["id"], "now")
    play_next(env)
    conn = db.open_db(env.settings)
    try:
        conn.execute("UPDATE tracks SET duration = 10000")
        current = conn.execute(
            "SELECT position_id, track_id FROM playback_state WHERE singleton = 1"
        ).fetchone()
        assert current is not None and current["position_id"]
        instance = db.get_setting(conn, "_engine_instance")
        conn.commit()
    finally:
        conn.close()
    env.icecast.payload = {
        "available": True,
        "mount_present": True,
        "listeners": 0,
        "bitrate": 128,
        "title": None,
        "error": None,
    }
    show_calls_before = len(
        [call for call in env.runner.calls if call and call[-2] == "ActiveEnterTimestamp"]
    )
    for _ in range(20):
        env.clock.advance(1)
        env.app.state.scheduler.tick()
    show_calls_after = len(
        [call for call in env.runner.calls if call and call[-2] == "ActiveEnterTimestamp"]
    )

    assert show_calls_after - show_calls_before <= 6
    conn = db.open_db(env.settings)
    try:
        assert db.get_setting(conn, "_engine_instance") == instance
        playback = conn.execute(
            "SELECT position_id, track_id FROM playback_state WHERE singleton = 1"
        ).fetchone()
        assert playback is not None
        assert playback["position_id"] == current["position_id"]
        assert playback["track_id"] == current["track_id"]
        history = conn.execute(
            "SELECT ended_at FROM history WHERE position_id = ?",
            (current["position_id"],),
        ).fetchone()
        assert history is not None and history["ended_at"] is None
    finally:
        conn.close()


def test_history_retention_is_by_age_not_count(env, admin, monkeypatch):
    conn = db.open_db(env.settings)
    try:
        conn.execute(
            "INSERT INTO history(track_id, started_at, title) VALUES('old', '2020-01-01T00:00:00Z', 'Old')"
        )
        for index in range(1005):
            conn.execute(
                "INSERT INTO history(track_id, started_at, title) VALUES(?, ?, ?)",
                (f"t{index}", "2026-11-23T09:00:00Z", f"T{index}"),
            )
        conn.commit()
    finally:
        conn.close()
    scheduler = env.app.state.scheduler
    scheduler.settings = replace(env.settings, history_retention_days=30)
    scheduler.tick()
    assert _conn_row(env, "SELECT COUNT(*) AS c FROM history WHERE title = 'Old'")["c"] == 0
    assert _conn_row(env, "SELECT COUNT(*) AS c FROM history WHERE title = 'T0'")["c"] == 1
