"""Contract 1.0.1 backend acceptance: system playlist, covers, cycles, sync, status."""

from __future__ import annotations

import sqlite3

from app import db, queue_engine
from app.config import SYSTEM_PLAYLIST_DESCRIPTION, SYSTEM_PLAYLIST_ID
from tests.helpers import (
    jpeg_bytes,
    make_playlist,
    play_next,
    upload_and_get,
    write_mp3,
)


def _upload(env, name: str, *, cover: bool = False) -> dict:
    return upload_and_get(
        env.client,
        write_mp3(env.settings.music_dir.parent / f"{name}.mp3", title=name, cover=cover),
    )


def _library(env) -> dict:
    payload = env.client.get("/api/admin/playlists").json()["items"]
    assert payload[0]["id"] == SYSTEM_PLAYLIST_ID
    return payload[0]


def _detail(env, playlist_id: str) -> dict:
    response = env.client.get(f"/api/admin/playlists/{playlist_id}")
    assert response.status_code == 200, response.text
    return response.json()["playlist"]


def _start(env) -> None:
    response = env.client.post("/api/admin/station/start")
    assert response.status_code == 200, response.text
    env.app.state.scheduler.tick()


def _source_refs(env) -> list[str]:
    conn = db.open_db(env.settings)
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT source_ref FROM queue_positions "
                "WHERE state IN ('pending','prepared') ORDER BY seq"
            ).fetchall()
        ]
    finally:
        conn.close()


def test_system_playlist_virtual_growth_shrink_and_immutability(env, admin):
    first = _upload(env, "sys-a", cover=True)
    second = _upload(env, "sys-b")
    library = _library(env)
    assert library["system"] is True
    assert library["name"] == "Вся медиатека"
    assert library["description"] == SYSTEM_PLAYLIST_DESCRIPTION
    assert library["shuffle"] is True
    assert library["repeat_mode"] == "all"
    assert library["track_count"] == 2
    assert library["cover_kind"] == "auto"

    detail = _detail(env, SYSTEM_PLAYLIST_ID)
    assert detail["tracks_total"] == 2
    assert len(detail["tracks"]) == 2
    assert {track["id"] for track in detail["tracks"]} == {first["id"], second["id"]}

    conn = db.open_db(env.settings)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?",
            (SYSTEM_PLAYLIST_ID,),
        ).fetchone()[0] == 0
    finally:
        conn.close()

    third = _upload(env, "sys-c")
    assert _library(env)["track_count"] == 3
    env.client.delete(f"/api/admin/tracks/{third['id']}")
    assert _library(env)["track_count"] == 2

    assert env.client.delete("/api/admin/playlists/library").status_code == 409
    assert (
        env.client.post(
            "/api/admin/playlists/library/tracks", json={"track_ids": [first["id"]]}
        ).status_code
        == 409
    )
    assert (
        env.client.delete(
            f"/api/admin/playlists/library/tracks/{first['id']}"
        ).status_code
        == 409
    )
    assert (
        env.client.put(
            "/api/admin/playlists/library/order", json={"track_ids": [first["id"]]}
        ).status_code
        == 409
    )
    assert (
        env.client.patch(
            "/api/admin/playlists/library", json={"name": "Другое имя"}
        ).status_code
        == 409
    )
    changed = env.client.patch(
        "/api/admin/playlists/library", json={"shuffle": False, "repeat_mode": "all"}
    )
    assert changed.status_code == 200
    assert changed.json()["playlist"]["shuffle"] is False


def test_add_tracks_result_counters(env, admin):
    existing = _upload(env, "add-existing")
    missing_file_track = _upload(env, "add-missing-file")
    playlist = make_playlist(env.client, "Counters", [existing["id"]])
    conn = db.open_db(env.settings)
    try:
        path = conn.execute(
            "SELECT path FROM tracks WHERE id = ?", (missing_file_track["id"],)
        ).fetchone()["path"]
    finally:
        conn.close()
    (env.settings.music_dir / path).unlink()

    response = env.client.post(
        f"/api/admin/playlists/{playlist['id']}/tracks",
        json={"track_ids": [existing["id"], existing["id"], "unknown", missing_file_track["id"]]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["added"] == 0
    assert body["skipped"] == 1
    assert body["unavailable"] == 2
    assert "playlist" in body


def test_tracks_ids_bulk_selection(env, admin):
    tracks = [_upload(env, f"bulk-id{i}") for i in range(3)]
    playlist = make_playlist(env.client, "Selection", [tracks[0]["id"]])
    all_ids = env.client.get("/api/admin/tracks/ids").json()
    assert all_ids["total"] == 3
    assert set(all_ids["ids"]) == {track["id"] for track in tracks}
    filtered = env.client.get(
        "/api/admin/tracks/ids", params={"exclude_playlist_id": playlist["id"]}
    ).json()
    assert filtered["total"] == 2
    assert tracks[0]["id"] not in filtered["ids"]
    search = env.client.get("/api/admin/tracks/ids", params={"q": "bulk-id1"}).json()
    assert search["total"] == 1
    assert len(search["ids"]) == 1


def test_playlist_custom_cover_priority_reset_and_delete(env, admin):
    track = _upload(env, "cover-track", cover=True)
    playlist = make_playlist(env.client, "Covered", [track["id"]])
    detail = _detail(env, playlist["id"])
    assert detail["cover_kind"] == "auto"
    assert detail["cover_url"].split("?")[0].endswith(
        f"/api/public/playlist-cover/{playlist['id']}"
    )

    response = env.client.post(
        f"/api/admin/playlists/{playlist['id']}/cover",
        files={"file": ("cover.jpg", jpeg_bytes((300, 200)), "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    custom = response.json()["playlist"]
    assert custom["cover_kind"] == "custom"
    assert "?v=" in custom["cover_url"]
    files = list(env.settings.playlist_cover_dir.glob(f"{playlist['id']}-*.jpg"))
    assert len(files) == 1
    from PIL import Image

    with Image.open(files[0]) as image:
        assert image.size == (600, 600)
    public = env.client.get(f"/api/public/playlist-cover/{playlist['id']}")
    assert public.status_code == 200
    assert public.headers["content-type"].startswith("image/jpeg")

    # Membership changes never clear the custom cover.
    second = _upload(env, "cover-track-2")
    env.client.post(
        f"/api/admin/playlists/{playlist['id']}/tracks",
        json={"track_ids": [second["id"]]},
    )
    assert _detail(env, playlist["id"])["cover_kind"] == "custom"

    reset = env.client.delete(f"/api/admin/playlists/{playlist['id']}/cover")
    assert reset.status_code == 200
    assert reset.json()["playlist"]["cover_kind"] == "auto"
    assert not files[0].exists()

    again = env.client.post(
        f"/api/admin/playlists/{playlist['id']}/cover",
        files={"file": ("cover.png", jpeg_bytes((100, 100)), "image/png")},
    )
    assert again.status_code == 200
    cover_file = next(env.settings.playlist_cover_dir.glob(f"{playlist['id']}-*.jpg"), None)
    assert cover_file is not None
    track_cover = env.settings.cover_dir / f"{track['id']}.jpg"
    assert track_cover.exists()
    env.client.delete(f"/api/admin/playlists/{playlist['id']}")
    assert not cover_file.exists()
    assert track_cover.exists()


def test_system_cycle_repeat_all_and_new_tracks(env, admin):
    tracks = [_upload(env, "lib-a"), _upload(env, "lib-b")]
    env.client.patch("/api/admin/playlists/library", json={"shuffle": False, "repeat_mode": "all"})
    _start(env)
    first = play_next(env)
    second = play_next(env)
    assert first and second
    refs = _source_refs(env)
    assert refs and all(ref.startswith("library:cycle:2") for ref in refs)
    third = play_next(env)
    fourth = play_next(env)
    assert third and fourth
    refs = _source_refs(env)
    assert refs and all(ref.startswith("library:cycle:3") for ref in refs)

    new_track = _upload(env, "lib-new")
    env.app.state.scheduler.tick()
    queued = [item["track"]["id"] for item in env.client.get("/api/admin/queue").json()["items"]]
    assert new_track["id"] in queued


def test_playlist_detail_pagination(env, admin):
    tracks = [_upload(env, f"page-{i}") for i in range(4)]
    playlist = make_playlist(env.client, "Paginated", [track["id"] for track in tracks])
    detail = env.client.get(
        f"/api/admin/playlists/{playlist['id']}",
        params={"tracks_limit": 2, "tracks_offset": 1},
    ).json()["playlist"]
    assert detail["tracks_total"] == 4
    assert [track["id"] for track in detail["tracks"]] == [t["id"] for t in tracks[1:3]]
    library = env.client.get(
        "/api/admin/playlists/library",
        params={"tracks_limit": 2, "tracks_offset": 0},
    ).json()["playlist"]
    assert library["tracks_total"] == 4
    assert len(library["tracks"]) == 2


def test_default_system_playlist_and_on_air(env, admin):
    track = _upload(env, "default-system")
    response = env.client.put("/api/admin/settings", json={"default_playlist_id": "library"})
    assert response.status_code == 200
    assert response.json()["settings"]["default_playlist_id"] == "library"
    _start(env)
    queue = env.client.get("/api/admin/queue").json()["items"]
    assert queue and queue[0]["source"]["kind"] == "library"

    env.icecast.payload = {
        "available": True,
        "mount_present": True,
        "listeners": 1,
        "bitrate": 128,
        "title": None,
        "error": None,
    }
    play_next(env)
    env.app.state.scheduler.tick()
    payload = env.client.get("/api/public/now-playing").json()
    assert payload["station"]["status"] == "on_air"
    assert payload["station"]["reason"] == "playing"
    assert payload["track"]["id"] == track["id"]


def test_http_links_only_when_configured(env, admin):
    import dataclasses

    original = env.app.state.settings
    env.app.state.settings = dataclasses.replace(
        original, public_http_base_url="http://radio.example.test"
    )
    try:
        links = env.client.get("/api/admin/links").json()["links"]
    finally:
        env.app.state.settings = original
    by_key = {item["key"]: item for item in links}
    assert by_key["http_mp3"]["url"] == "http://radio.example.test/radio.mp3"
    assert by_key["mp3"]["primary"] is True


def test_engine_restart_resets_prepared_and_resyncs(env, admin):
    tracks = [_upload(env, f"restart-{i}") for i in range(3)]
    playlist = make_playlist(env.client, "Restart", [track["id"] for track in tracks])
    env.client.put("/api/admin/settings", json={"default_playlist_id": playlist["id"]})
    _start(env)
    env.app.state.scheduler.tick()
    syncs = env.engine.sync_calls
    response = env.client.post("/api/admin/station/restart")
    assert response.status_code == 200
    assert env.engine.sync_calls > syncs
    queue = env.client.get("/api/admin/queue").json()
    assert queue["current"] is None or queue["current"]["id"]
    assert queue["items"]


def test_short_and_single_track_playlists_repeat(env, admin):
    track = _upload(env, "single")
    playlist = make_playlist(
        env.client, "Single", [track["id"]], repeat_mode="all"
    )
    env.client.put("/api/admin/settings", json={"default_playlist_id": playlist["id"]})
    _start(env)
    started = [play_next(env) for _ in range(3)]
    assert all(item and item["track"]["id"] == track["id"] for item in started)


def test_system_playlist_play_and_cover(env, admin):
    track = _upload(env, "system-play", cover=True)
    response = env.client.post("/api/admin/playlists/library/play", json={"mode": "now"})
    assert response.status_code == 200
    body = response.json()
    assert body["started"] is True
    assert body["station"]["desired"] == "playing"
    assert body["playlist"]["system"] is True
    queue = env.client.get("/api/admin/queue").json()["items"]
    assert any(item["track"]["id"] == track["id"] for item in queue)

    cover = env.client.post(
        "/api/admin/playlists/library/cover",
        files={"file": ("library.jpg", jpeg_bytes((200, 200)), "image/jpeg")},
    )
    assert cover.status_code == 200
    assert cover.json()["playlist"]["cover_kind"] == "custom"


def test_membership_removal_drops_prepared_position(env, admin):
    tracks = [_upload(env, f"member-{i}") for i in range(4)]
    playlist = make_playlist(env.client, "Members", [track["id"] for track in tracks])
    env.client.put("/api/admin/settings", json={"default_playlist_id": playlist["id"]})
    _start(env)
    env.app.state.scheduler.tick()
    items = env.client.get("/api/admin/queue").json()["items"]
    prepared = [item for item in items if item["prepared"]]
    assert prepared
    victim = prepared[0]["track"]["id"]
    response = env.client.delete(
        f"/api/admin/playlists/{playlist['id']}/tracks/{victim}"
    )
    assert response.status_code == 200
    after = env.client.get("/api/admin/queue").json()["items"]
    assert victim not in [item["track"]["id"] for item in after]


def test_system_playlist_allowed_in_rotation_and_schedule(env, admin):
    _upload(env, "system-rotation-a")
    _upload(env, "system-rotation-b")
    rotation = env.client.put(
        "/api/admin/rotation",
        json={"mode": "sequential", "items": [{"playlist_id": "library", "position": 0}]},
    )
    assert rotation.status_code == 200
    env.app.state.scheduler.tick()
    items = env.client.get("/api/admin/queue").json()["items"]
    assert items and items[0]["source"]["kind"] == "rotation"

    rule = env.client.post(
        "/api/admin/schedule",
        json={
            "name": "System schedule",
            "playlist_id": "library",
            "use_rotation": False,
            "days": [0, 1, 2, 3, 4, 5, 6],
            "start": "00:00",
            "end": "23:59",
            "enabled": True,
            "priority": 50,
        },
    )
    assert rule.status_code == 201, rule.text
    env.app.state.scheduler.tick()
    items = env.client.get("/api/admin/queue").json()["items"]
    assert items and items[0]["source"]["kind"] == "schedule"
    assert items[0]["source"]["playlist_id"] == "library"


def test_pending_delete_count_in_playlist_payload(env, admin):
    first = _upload(env, "pending-a")
    second = _upload(env, "pending-b")
    playlist = make_playlist(env.client, "Pending", [first["id"], second["id"]])
    conn = db.open_db(env.settings)
    try:
        conn.execute("UPDATE tracks SET pending_delete = 1 WHERE id = ?", (first["id"],))
        conn.commit()
    finally:
        conn.close()
    payload = env.client.get(f"/api/admin/playlists/{playlist['id']}").json()["playlist"]
    assert payload["track_count"] == 1
    assert payload["pending_delete_count"] == 1


def test_playlist_cover_accepts_png_and_webp(env, admin):
    import io

    from PIL import Image

    track = _upload(env, "cover-formats")
    playlist = make_playlist(env.client, "Formats", [track["id"]])
    for fmt in ("PNG", "WEBP"):
        buffer = io.BytesIO()
        Image.new("RGB", (120, 80), (80, 120, 160)).save(buffer, format=fmt)
        response = env.client.post(
            f"/api/admin/playlists/{playlist['id']}/cover",
            files={"file": (f"cover.{fmt.lower()}", buffer.getvalue(), f"image/{fmt.lower()}")},
        )
        assert response.status_code == 200, response.text
        assert response.json()["playlist"]["cover_kind"] == "custom"


def test_migration_is_idempotent_and_preserves_system_settings(settings):
    db.init_db(settings)
    conn = db.open_db(settings)
    try:
        conn.execute(
            "UPDATE playlists SET shuffle = 0, repeat_mode = 'none' WHERE id = 'library'"
        )
        conn.commit()
    finally:
        conn.close()
    db.init_db(settings)
    conn = db.open_db(settings)
    try:
        row = conn.execute("SELECT shuffle, repeat_mode, system FROM playlists WHERE id = 'library'").fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version == 2
    assert row["shuffle"] == 0
    assert row["repeat_mode"] == "none"
    assert row["system"] == 1


def test_user_source_exhaustion_is_not_replaced_by_library(env, admin):
    track = _upload(env, "user-once")
    _upload(env, "library-extra")
    playlist = make_playlist(env.client, "Once", [track["id"]], repeat_mode="none")
    env.client.put("/api/admin/settings", json={"default_playlist_id": playlist["id"]})
    _start(env)
    env.icecast.payload = {
        "available": True,
        "mount_present": True,
        "listeners": 1,
        "bitrate": 128,
        "title": None,
        "error": None,
    }
    assert play_next(env)["track"]["id"] == track["id"]
    env.app.state.scheduler.tick()
    conn = db.open_db(env.settings)
    try:
        conn.execute("DELETE FROM playback_state")
        conn.commit()
    finally:
        conn.close()
    env.app.state.scheduler.tick()
    payload = env.client.get("/api/public/now-playing").json()
    assert payload["station"]["status"] == "no_music"
    assert payload["station"]["reason"] == "playlist_exhausted"
    items = env.client.get("/api/admin/queue").json()["items"]
    assert items == []
    assert all(item["source"]["kind"] != "library" for item in items)


def test_explicit_play_resets_exhausted_source(env, admin):
    track = _upload(env, "reset-play")
    playlist = make_playlist(env.client, "Reset", [track["id"]], repeat_mode="none")
    env.client.put("/api/admin/settings", json={"default_playlist_id": playlist["id"]})
    _start(env)
    assert play_next(env)
    env.app.state.scheduler.tick()
    assert env.client.get("/api/admin/queue").json()["items"] == []
    played = env.client.post(f"/api/admin/playlists/{playlist['id']}/play", json={"mode": "now"})
    assert played.status_code == 200
    assert played.json()["started"] is True
    assert env.client.get("/api/admin/queue").json()["items"]


def test_playlist_play_starts_stopped_station(env, admin):
    track = _upload(env, "play-stopped")
    playlist = make_playlist(env.client, "Stopped play", [track["id"]])
    response = env.client.post(f"/api/admin/playlists/{playlist['id']}/play")
    assert response.status_code == 200
    body = response.json()
    assert body["started"] is True
    assert body["station"]["desired"] == "playing"
    assert body["playlist"]["id"] == playlist["id"]
    conn = db.open_db(env.settings)
    try:
        assert db.get_setting(conn, "desired_state") == "playing"
    finally:
        conn.close()


def test_queue_current_has_id(env, admin):
    _upload(env, "current-id")
    _start(env)
    position = play_next(env)
    current = env.client.get("/api/admin/queue").json()["current"]
    assert current["position_id"] == position["id"]
    assert current["id"] == position["id"]


def test_engine_sync_unresolved_requests_are_not_prepared(env, admin):
    for index in range(3):
        _upload(env, f"sync-fail-{index}")
    _start(env)
    env.app.state.scheduler.tick()
    env.engine.sync_report_result = {
        "ok": True,
        "resolved": 1,
        "failed": 1,
        "reply": "OK 1 1",
    }
    conn = db.open_db(env.settings)
    try:
        ok = queue_engine.sync_engine(
            conn, env.settings, env.engine, force=True
        )
        items = conn.execute(
            "SELECT id, state FROM queue_positions WHERE state IN ('pending','prepared') ORDER BY seq"
        ).fetchall()
        warnings = conn.execute(
            "SELECT COUNT(*) FROM events WHERE message LIKE '%не подготовил часть%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert ok is True
    assert sum(1 for row in items if row["state"] == "prepared") == 1
    assert sum(1 for row in items if row["state"] == "pending") >= 1
    assert warnings == 1

    env.engine.sync_report_result = {
        "ok": True,
        "resolved": 0,
        "failed": 2,
        "reply": "OK 0 2",
    }
    conn = db.open_db(env.settings)
    try:
        ok = queue_engine.sync_engine(conn, env.settings, env.engine, force=True)
        prepared = conn.execute(
            "SELECT COUNT(*) FROM queue_positions WHERE state = 'prepared'"
        ).fetchone()[0]
        detail = queue_engine.queue_snapshot(conn, env.settings, env.clock.now())
    finally:
        conn.close()
    assert ok is False
    assert prepared == 0
    assert detail is not None


def test_services_storage_links_contract(env, admin):
    _upload(env, "contract-services")
    services = env.client.get("/api/admin/services").json()["services"]
    assert {item["id"] for item in services} == {"api", "liquidsoap", "icecast"}
    assert all("unit" in item and "state_label" in item for item in services)
    storage = env.client.get("/api/admin/storage").json()
    for key in ("tracks", "bytes", "cover_bytes", "db_bytes", "free_bytes", "total_bytes", "limit_gb"):
        assert isinstance(storage[key], (int, float))
    links = env.client.get("/api/admin/links").json()["links"]
    assert [item["key"] for item in links] == ["mp3", "player", "m3u", "pls", "metadata"]
    assert all("world_radio" not in item for item in links)


def test_schedule_skips_empty_rule_to_next_rule(env, admin):
    valid_track = _upload(env, "skip-rule-valid")
    empty = env.client.post("/api/admin/playlists", json={"name": "Empty rule"}).json()["playlist"]
    valid = make_playlist(env.client, "Valid rule", [valid_track["id"]])
    base = {
        "use_rotation": False,
        "days": [0, 1, 2, 3, 4, 5, 6],
        "start": "00:00",
        "end": "23:59",
        "enabled": True,
        "transition": "after_track",
    }
    first = env.client.post(
        "/api/admin/schedule",
        json={**base, "name": "Empty first", "playlist_id": empty["id"], "priority": 20},
    )
    second = env.client.post(
        "/api/admin/schedule",
        json={**base, "name": "Valid second", "playlist_id": valid["id"], "priority": 10},
    )
    assert first.status_code == 201 and second.status_code == 201
    env.app.state.scheduler.tick()
    items = env.client.get("/api/admin/queue").json()["items"]
    assert items
    assert items[0]["source"]["kind"] == "schedule"
    assert items[0]["source"]["playlist_id"] == valid["id"]


def test_empty_default_user_playlist_reports_reason_without_library_fallback(env, admin):
    _upload(env, "empty-default-extra")
    empty = env.client.post("/api/admin/playlists", json={"name": "Empty default"}).json()["playlist"]
    env.client.put("/api/admin/settings", json={"default_playlist_id": empty["id"]})
    _start(env)
    env.icecast.payload = {
        "available": True,
        "mount_present": True,
        "listeners": 0,
        "bitrate": 128,
        "title": None,
        "error": None,
    }
    env.app.state.scheduler.tick()
    status = env.client.get("/api/admin/station/status").json()
    assert status["state"] == "no_music"
    assert status["reason"] == "playlist_empty"
    assert env.client.get("/api/admin/queue").json()["items"] == []


def test_empty_sync_is_rejected_while_source_has_tracks(env, admin):
    _upload(env, "empty-sync")
    conn = db.open_db(env.settings)
    try:
        conn.execute("DELETE FROM queue_positions")
        conn.commit()
        before = env.engine.sync_calls
        ok = queue_engine.sync_engine(conn, env.settings, env.engine, force=True)
        after = env.engine.sync_calls
        error = db.get_setting(conn, "_engine_sync_error")
    finally:
        conn.close()
    assert ok is False
    assert after == before
    assert error


def test_empty_sync_is_allowed_with_empty_library(env, admin):
    conn = db.open_db(env.settings)
    try:
        conn.execute("DELETE FROM queue_positions")
        conn.commit()
        before = env.engine.sync_calls
        ok = queue_engine.sync_engine(conn, env.settings, env.engine, force=True)
        after = env.engine.sync_calls
    finally:
        conn.close()
    assert ok is True
    assert after == before + 1


def test_restart_clears_stale_current(env, admin):
    _upload(env, "restart-stale")
    _start(env)
    play_next(env)
    assert env.client.get("/api/admin/queue").json()["current"] is not None
    env.client.post("/api/admin/station/restart")
    assert env.client.get("/api/admin/queue").json()["current"] is None


def test_services_last_error_only_for_liquidsoap(env, admin):
    conn = db.open_db(env.settings)
    try:
        db.set_setting(conn, "engine_last_error", "engine boom")
        conn.commit()
    finally:
        conn.close()
    services = {item["id"]: item for item in env.client.get("/api/admin/services").json()["services"]}
    assert services["liquidsoap"]["last_error"] == "engine boom"
    assert services["api"]["last_error"] is None
    assert services["icecast"]["last_error"] is None


def test_playlist_cover_public_priority(env, admin):
    track = _upload(env, "public-cover-track", cover=True)
    playlist = make_playlist(env.client, "Public cover", [track["id"]])
    auto = env.client.get(f"/api/public/playlist-cover/{playlist['id']}")
    assert auto.status_code == 200

    env.client.post(
        f"/api/admin/playlists/{playlist['id']}/cover",
        files={"file": ("custom.jpg", jpeg_bytes((64, 64)), "image/jpeg")},
    )
    custom = env.client.get(f"/api/public/playlist-cover/{playlist['id']}")
    assert custom.status_code == 200
    assert custom.content != auto.content
    env.client.delete(f"/api/admin/playlists/{playlist['id']}/cover")
    restored = env.client.get(f"/api/public/playlist-cover/{playlist['id']}")
    assert restored.status_code == 200


def test_failed_sync_is_retried_and_clears_last_error(env, admin):
    tracks = [_upload(env, f"retry-{i}") for i in range(3)]
    playlist = make_playlist(env.client, "Retry", [track["id"] for track in tracks])
    env.client.put("/api/admin/settings", json={"default_playlist_id": playlist["id"]})
    _start(env)
    conn = db.open_db(env.settings)
    try:
        state: dict = {}
        now = env.clock.now()
        env.engine.sync_ok = False
        assert queue_engine.sync_engine(
            conn, env.settings, env.engine, now=now, sync_state=state
        ) is False
        assert state.get("failed") is True
        assert "signature" not in state
        assert db.get_setting(conn, "_engine_sync_error")
        env.engine.sync_ok = True
        db.set_setting(conn, "engine_last_error", "old failure")
        conn.commit()
        assert queue_engine.sync_engine(
            conn, env.settings, env.engine, now=now, sync_state=state
        ) is True
        assert not db.get_setting(conn, "_engine_sync_error")
        assert db.get_setting(conn, "engine_last_error") == ""
    finally:
        conn.close()


def test_empty_library_start_and_upload_recovery(env, admin):
    env.engine.sync_ok = False  # socket not ready yet, like a just-started unit
    _start(env)
    status = env.client.get("/api/admin/station/status").json()
    assert status["state"] == "no_music"
    assert status["reason"] == "library_empty"
    assert status["status"] == status["state"]
    assert status["unit_active"] is True
    starts = len(
        [call for call in env.runner.calls if call[0] == "sudo" and call[-2] == "start"]
    )
    conn = db.open_db(env.settings)
    try:
        assert not db.get_setting(conn, "_engine_sync_error")
    finally:
        conn.close()

    env.engine.sync_ok = True
    _upload(env, "recovered")
    env.icecast.payload = {
        "available": True,
        "mount_present": True,
        "listeners": 1,
        "bitrate": 128,
        "title": None,
        "error": None,
    }
    env.app.state.scheduler.tick()
    play_next(env)
    env.app.state.scheduler.tick()
    status = env.client.get("/api/admin/station/status").json()
    assert status["state"] == "on_air"
    assert status["status"] == status["state"]
    assert len(
        [call for call in env.runner.calls if call[0] == "sudo" and call[-2] == "start"]
    ) == starts


def test_migration_v1_keeps_data_and_clears_library_cycle(settings):
    schema = db.SCHEMA_PATH.read_text(encoding="utf-8").replace(
        "    system INTEGER NOT NULL DEFAULT 0,\n", ""
    )
    conn = sqlite3.connect(str(settings.db_path))
    try:
        conn.executescript(schema)
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "INSERT INTO playlists(id, name, description, cover_path, shuffle, repeat_mode, "
            "created_at, updated_at) VALUES('user-1', 'User one', 'desc', NULL, 0, 'none', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO tracks(id, sha256, path, ext, title, created_at, updated_at) "
            "VALUES('track-1', 'hash-1', 'hash-1.mp3', '.mp3', 'Track one', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('_cycle:library', ?)",
            ('{"cycle":1,"remaining":[],"started":true,"tail":"track-1"}',),
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('_cycle:playlist:library', '{}')"
        )
        conn.commit()
    finally:
        conn.close()

    db.init_db(settings)
    conn = db.open_db(settings)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        system = conn.execute(
            "SELECT * FROM playlists WHERE id = 'library'"
        ).fetchone()
        user = conn.execute("SELECT * FROM playlists WHERE id = 'user-1'").fetchone()
        track = conn.execute("SELECT * FROM tracks WHERE id = 'track-1'").fetchone()
        stale = conn.execute(
            "SELECT COUNT(*) FROM settings WHERE key IN ('_cycle:library','_cycle:playlist:library')"
        ).fetchone()[0]
        system_column = {
            row["name"] for row in conn.execute("PRAGMA table_info(playlists)")
        }
    finally:
        conn.close()
    assert version == 2
    assert "system" in system_column
    assert system is not None and system["system"] == 1
    assert system["shuffle"] == 1 and system["repeat_mode"] == "all"
    assert user is not None and user["name"] == "User one"
    assert track is not None and track["title"] == "Track one"
    assert stale == 0


def test_station_playback_stalled_reason(env, admin):
    _upload(env, "stall")
    _start(env)
    env.icecast.payload = {
        "available": True,
        "mount_present": True,
        "listeners": 0,
        "bitrate": 128,
        "title": None,
        "error": None,
    }
    env.clock.advance(61)
    status = env.client.get("/api/admin/station/status").json()
    assert status["state"] == "error"
    assert status["reason"] == "playback_stalled"
