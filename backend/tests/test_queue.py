"""QUEUE-01..09: engine sync, shuffle/repeat cycles, manual queue, persistence."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app import db, queue_engine
from app.main import create_app
from tests.helpers import make_playlist, play_next, upload_and_get, write_mp3


def _upload(env, name: str, title: str | None = None) -> dict:
    return upload_and_get(
        env.client,
        write_mp3(env.settings.music_dir.parent / f"{name}.mp3", title=title or name),
    )


def _use_playlist(env, playlist_id: str, depth: int | None = None, start: bool = False) -> None:
    payload = {"default_playlist_id": playlist_id}
    if depth is not None:
        payload["queue_depth"] = depth
    response = env.client.put("/api/admin/settings", json=payload)
    assert response.status_code == 200, response.text
    if start:
        started = env.client.post("/api/admin/station/start")
        assert started.status_code == 200, started.text
    env.app.state.scheduler.tick()


def _history(env) -> list:
    conn = db.open_db(env.settings)
    try:
        return conn.execute("SELECT * FROM history ORDER BY id ASC").fetchall()
    finally:
        conn.close()


def test_engine_sync_file_and_prepared_prefix(env, admin):
    tracks = [_upload(env, f"sync{i}", f"Sync {i}") for i in range(8)]
    playlist = make_playlist(env.client, "Sync", [track["id"] for track in tracks])
    _use_playlist(env, playlist["id"], start=True)

    queue = env.client.get("/api/admin/queue").json()
    assert len(queue["items"]) == 6
    prepared = [item for item in queue["items"] if item["prepared"]]
    assert len(prepared) == queue_engine.ENGINE_PREFETCH
    assert env.engine.sync_calls >= 1

    content = env.settings.engine_queue_file.read_text(encoding="utf-8")
    lines = [line for line in content.splitlines() if line]
    assert len(lines) == queue_engine.ENGINE_PREFETCH
    prepared_ids = [item["id"] for item in prepared]
    link_paths = [Path(line) for line in lines]
    assert [path.stem for path in link_paths] == prepared_ids
    for path in link_paths:
        assert path.is_absolute()
        assert path.parent == env.settings.queue_link_dir
        assert path.is_symlink()
        assert path.resolve().exists()
        assert path.suffix == ".mp3"
        assert path.resolve().parent == env.settings.music_dir

    snapshot_ids = [item["id"] for item in queue["items"]]
    assert [item["id"] for item in prepared] == snapshot_ids[: queue_engine.ENGINE_PREFETCH]
    for item in queue["items"]:
        assert item["estimated_start"]
        datetime.fromisoformat(item["estimated_start"])


def test_sync_failure_keeps_pending_and_warns(env, admin):
    tracks = [_upload(env, f"fail{i}", f"Fail {i}") for i in range(4)]
    playlist = make_playlist(env.client, "Fail", [track["id"] for track in tracks])
    env.engine.sync_ok = False
    _use_playlist(env, playlist["id"], start=True)
    queue = env.client.get("/api/admin/queue").json()
    assert queue["items"]
    assert not any(item["prepared"] for item in queue["items"])
    conn = db.open_db(env.settings)
    try:
        warnings = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'engine' AND level = 'warning'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert warnings >= 1

    env.engine.sync_ok = True
    env.app.state.scheduler.tick()
    queue = env.client.get("/api/admin/queue").json()
    assert any(item["prepared"] for item in queue["items"])


def test_queue_depth_and_no_duplicates_in_snapshot(env, admin):
    tracks = [_upload(env, f"uniq{i}", f"Unique {i}") for i in range(3)]
    playlist = make_playlist(env.client, "Small", [track["id"] for track in tracks])
    _use_playlist(env, playlist["id"], depth=6)
    ids = [item["track"]["id"] for item in env.client.get("/api/admin/queue").json()["items"]]
    assert len(ids) == 3
    assert len(set(ids)) == 3


def test_shuffle_cycle_plays_every_track_once(env, admin):
    tracks = [_upload(env, f"sh{i}", f"Shuffle {i}") for i in range(8)]
    playlist = make_playlist(
        env.client, "Shuffled", [track["id"] for track in tracks], shuffle=True, repeat_mode="all"
    )
    _use_playlist(env, playlist["id"], depth=8)

    queued = [item["track"]["id"] for item in env.client.get("/api/admin/queue").json()["items"]]
    assert len(queued) == len(set(queued)) == 8

    played = []
    for _ in range(8):
        position = play_next(env)
        assert position is not None
        played.append(position["track"]["id"])
    assert sorted(played) == sorted(track["id"] for track in tracks)
    assert len(set(played)) == 8

    history = _history(env)
    assert all(":cycle:1" in row["source_ref"] for row in history)

    ninth = play_next(env)
    assert ninth is not None
    assert ninth["track"]["id"] != played[-1]
    assert ":cycle:2" in _history(env)[-1]["source_ref"]


def test_repeat_none_does_not_restart_playlist(env, admin):
    tracks = [_upload(env, f"none{i}", f"None {i}") for i in range(3)]
    playlist = make_playlist(env.client, "Once", [track["id"] for track in tracks])
    _use_playlist(env, playlist["id"], depth=3)
    played = [play_next(env)["track"]["id"] for _ in range(3)]
    assert sorted(played) == sorted(track["id"] for track in tracks)
    refs = [row["source_ref"] for row in _history(env)]
    assert all(":cycle:1" in ref for ref in refs)
    # A repeat-none playlist pass ends; the next source is the library fallback,
    # never a new cycle of the same playlist.
    future = env.client.get("/api/admin/queue").json()["items"]
    playlist_key = f"playlist:{playlist['id']}"
    for item in future:
        if item["source"]["kind"] == "playlist":
            assert playlist_key not in (item["source"].get("playlist_id") or "")


def test_repeat_all_starts_new_cycle(env, admin):
    tracks = [_upload(env, f"all{i}", f"All {i}") for i in range(3)]
    playlist = make_playlist(
        env.client, "Loop", [track["id"] for track in tracks], repeat_mode="all"
    )
    _use_playlist(env, playlist["id"], depth=3)
    for _ in range(3):
        assert play_next(env) is not None
    queue = env.client.get("/api/admin/queue").json()
    assert queue["items"], "repeat all must refill the queue"
    refs = []
    conn = db.open_db(env.settings)
    try:
        refs = [
            row[0]
            for row in conn.execute(
                "SELECT source_ref FROM queue_positions WHERE state IN ('pending','prepared')"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert refs
    assert all(":cycle:1" not in ref for ref in refs)
    assert any(":cycle:2" in ref or ":cycle:3" in ref for ref in refs)


def test_repeat_one_replays_same_track(env, admin):
    track = _upload(env, "one", "Одна песня")
    playlist = make_playlist(
        env.client, "One", [track["id"]], repeat_mode="one"
    )
    _use_playlist(env, playlist["id"], depth=3)
    queue = env.client.get("/api/admin/queue").json()["items"]
    assert len(queue) == 3
    assert {item["track"]["id"] for item in queue} == {track["id"]}
    played = [play_next(env)["track"]["id"] for _ in range(3)]
    assert set(played) == {track["id"]}


def test_manual_queue_controls(env, admin):
    first = _upload(env, "m1", "M1")
    second = _upload(env, "m2", "M2")
    manual = _upload(env, "m3", "M3")
    playlist = make_playlist(env.client, "Manual", [first["id"], second["id"]])
    _use_playlist(env, playlist["id"], depth=3)

    response = env.client.post(f"/api/admin/tracks/{manual['id']}/play-next")
    assert response.status_code == 200
    items = response.json()["items"]
    assert items[0]["track"]["id"] == manual["id"]
    assert items[0]["manual"] is True

    env.client.post(f"/api/admin/tracks/{manual['id']}/queue")
    tail = env.client.get("/api/admin/queue").json()["items"]
    assert tail[-1]["track"]["id"] == manual["id"]
    assert tail[-1]["manual"] is True

    cleared = env.client.post("/api/admin/queue/clear-manual").json()
    assert cleared["removed"] == 2
    assert not any(item["manual"] for item in cleared["items"])

    env.client.post("/api/admin/tracks/{0}/play-now".format(manual["id"]))
    assert env.engine.skip_calls == 0  # station is stopped: no engine skip
    assert env.client.get("/api/admin/queue").json()["items"][0]["track"]["id"] == manual["id"]


def test_play_now_skips_when_station_playing(env, admin):
    track = _upload(env, "pnow", "Сейчас")
    _upload(env, "other", "Другой")
    env.client.post("/api/admin/station/start")
    conn = db.open_db(env.settings)
    try:
        assert env.station.desired(conn) == "playing"
    finally:
        conn.close()
    env.client.post(f"/api/admin/tracks/{track['id']}/play-now")
    assert env.engine.skip_calls == 1


def test_prepared_positions_are_locked(env, admin):
    tracks = [_upload(env, f"lock{i}", f"Lock {i}") for i in range(6)]
    playlist = make_playlist(env.client, "Lock", [track["id"] for track in tracks])
    _use_playlist(env, playlist["id"], start=True)
    items = env.client.get("/api/admin/queue").json()["items"]
    prepared = [item for item in items if item["prepared"]]
    assert prepared

    blocked = env.client.patch(
        f"/api/admin/queue/{prepared[0]['id']}", json={"move_to": 3}
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "position_locked"
    assert env.client.delete(f"/api/admin/queue/{prepared[0]['id']}").status_code == 409

    pending = [item for item in items if not item["prepared"]]
    moved = env.client.patch(f"/api/admin/queue/{pending[-1]['id']}", json={"move_to": 0})
    assert moved.status_code == 200
    after = moved.json()["items"]
    assert after[0]["id"] == pending[-1]["id"]
    assert after[1]["prepared"] is True
    assert after[1]["id"] == prepared[0]["id"]

    deleted = env.client.delete(f"/api/admin/queue/{pending[0]['id']}")
    assert deleted.status_code == 200
    assert pending[0]["id"] not in [item["id"] for item in deleted.json()["items"]]


def test_shuffle_future_keeps_prepared_and_moves_pending(env, admin, monkeypatch):
    tracks = [_upload(env, f"fsh{i}", f"Future {i}") for i in range(6)]
    playlist = make_playlist(env.client, "Future", [track["id"] for track in tracks])
    _use_playlist(env, playlist["id"], start=True)
    items = env.client.get("/api/admin/queue").json()["items"]
    prepared_ids = [item["id"] for item in items if item["prepared"]]
    pending_ids = [item["id"] for item in items if not item["prepared"]]
    assert len(pending_ids) >= 2

    class ReversingRandom:
        def shuffle(self, values):
            values.reverse()

        def choice(self, values):
            return values[0]

        def randrange(self, start, stop=None):
            return start

    monkeypatch.setattr(queue_engine.random, "SystemRandom", ReversingRandom)
    shuffled = env.client.post("/api/admin/queue/shuffle").json()
    assert shuffled["shuffled"] == len(pending_ids)
    new_ids = [item["id"] for item in shuffled["items"]]
    assert new_ids[: len(prepared_ids)] == prepared_ids
    assert new_ids[len(prepared_ids):] == list(reversed(pending_ids))


def test_source_change_keeps_manual_items(env, admin):
    first = _upload(env, "src1", "Src1")
    second = _upload(env, "src2", "Src2")
    manual = _upload(env, "srcm", "Manual source")
    playlist_a = make_playlist(env.client, "A", [first["id"]])
    playlist_b = make_playlist(env.client, "B", [second["id"]])
    _use_playlist(env, playlist_a["id"], depth=2)
    env.client.post(f"/api/admin/tracks/{manual['id']}/play-next")

    env.client.post(f"/api/admin/playlists/{playlist_b['id']}/play")
    env.app.state.scheduler.tick()
    items = env.client.get("/api/admin/queue").json()["items"]
    assert any(item["track"]["id"] == manual["id"] and item["manual"] for item in items)
    assert any(item["track"]["id"] == second["id"] for item in items)
    assert not any(
        item["track"]["id"] == first["id"] and not item["manual"] for item in items
    )


def test_track_start_is_idempotent_and_unknown_is_safe(env, admin):
    track = _upload(env, "idem", "Idempotent")
    playlist = make_playlist(env.client, "Idem", [track["id"]])
    _use_playlist(env, playlist["id"], depth=2)
    position = env.client.get("/api/admin/queue").json()["items"][0]
    url = "/api/internal/engine/track-start"
    headers = {"X-Internal-Token": env.internal_token}
    first = env.client.post(url, data={"vd": position["id"], "ts": "1700000000.5"}, headers=headers)
    second = env.client.post(url, data={"vd": position["id"], "ts": "1700000003.5"}, headers=headers)
    assert first.status_code == second.status_code == 200
    history = _history(env)
    assert len(history) == 1
    assert history[0]["position_id"] == position["id"]

    unknown = env.client.post(url, data={"vd": "nope", "ts": "1700000004"}, headers=headers)
    assert unknown.status_code == 200
    assert unknown.json()["ok"] is True
    conn = db.open_db(env.settings)
    try:
        warnings = conn.execute(
            "SELECT COUNT(*) FROM events WHERE message LIKE '%неизвестной позиции%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert warnings == 1

    forbidden = env.client.post(url, data={"vd": position["id"]}, headers={"X-Internal-Token": "bad"})
    assert forbidden.status_code == 403


def test_queue_survives_restart(env, admin):
    tracks = [_upload(env, f"keep{i}", f"Keep {i}") for i in range(5)]
    playlist = make_playlist(env.client, "Persist", [track["id"] for track in tracks])
    _use_playlist(env, playlist["id"])
    started = play_next(env)
    before = env.client.get("/api/admin/queue").json()
    before_ids = [item["id"] for item in before["items"]]
    assert before_ids

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
        client2.headers.update({"X-CSRF-Token": login.json()["csrf_token"]})
        after = client2.get("/api/admin/queue").json()
        after_ids = [item["id"] for item in after["items"]]
        assert before["current"]["position_id"] == after["current"]["position_id"]
        assert after_ids[: len(before_ids)] == before_ids
        assert started["id"] == after["current"]["position_id"]


def test_engine_track_start_accepts_symlink_path(env, admin):
    tracks = [_upload(env, f"link{i}", f"Link {i}") for i in range(3)]
    playlist = make_playlist(env.client, "Links", [track["id"] for track in tracks])
    _use_playlist(env, playlist["id"], start=True)
    lines = [
        line
        for line in env.settings.engine_queue_file.read_text(encoding="utf-8").splitlines()
        if line
    ]
    link = lines[0]
    position_id = Path(link).stem
    response = env.client.post(
        "/api/internal/engine/track-start",
        data={"f": link, "ts": f"{env.clock.now().timestamp():.3f}"},
        headers={"X-Internal-Token": env.internal_token},
    )
    assert response.status_code == 200
    history = _history(env)
    assert history[-1]["position_id"] == position_id

    outside = env.client.post(
        "/api/internal/engine/track-start",
        data={"f": "/etc/passwd", "ts": "1"},
        headers={"X-Internal-Token": env.internal_token},
    )
    assert outside.status_code == 422
    assert outside.json()["detail"]["code"] == "invalid_engine_path"

    unknown = env.client.post(
        "/api/internal/engine/track-start",
        data={"f": str(env.settings.queue_link_dir / "unknown.mp3"), "ts": "1"},
        headers={"X-Internal-Token": env.internal_token},
    )
    assert unknown.status_code == 200


def test_missing_audio_is_dropped_from_engine_queue(env, admin):
    tracks = [_upload(env, f"lost{i}", f"Lost {i}") for i in range(3)]
    playlist = make_playlist(env.client, "Lost", [track["id"] for track in tracks])
    _use_playlist(env, playlist["id"], start=True)
    conn = db.open_db(env.settings)
    try:
        row = conn.execute(
            "SELECT path FROM tracks WHERE id = ?", (tracks[0]["id"],)
        ).fetchone()
    finally:
        conn.close()
    (env.settings.music_dir / row["path"]).unlink()
    env.app.state.scheduler.tick()
    items = env.client.get("/api/admin/queue").json()["items"]
    assert tracks[0]["id"] not in [item["track"]["id"] for item in items]
    conn = db.open_db(env.settings)
    try:
        warnings = conn.execute(
            "SELECT COUNT(*) FROM events WHERE message LIKE '%недоступен%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert warnings >= 1


def test_library_is_the_last_resort_source(env, admin):
    tracks = [
        upload_and_get(env.client, write_mp3(env.settings.music_dir.parent / f"lib{i}.mp3", title=f"Lib {i}"))
        for i in range(3)
    ]
    env.app.state.scheduler.tick()
    items = env.client.get("/api/admin/queue").json()["items"]
    assert items
    assert all(item["source"]["kind"] == "library" for item in items)
    ids = [item["track"]["id"] for item in items]
    assert sorted(ids) == sorted(track["id"] for track in tracks)
    assert len(ids) == len(set(ids))


def test_stale_links_are_removed_after_track_start(env, admin):
    tracks = [_upload(env, f"stale{i}", f"Stale {i}") for i in range(5)]
    playlist = make_playlist(env.client, "Stale", [track["id"] for track in tracks])
    _use_playlist(env, playlist["id"], start=True)
    first_link = env.settings.queue_link_dir / (
        env.client.get("/api/admin/queue").json()["items"][0]["id"] + ".mp3"
    )
    assert first_link.is_symlink()
    play_next(env)
    # The link of the playing position is kept until the engine moves on.
    assert first_link.exists()
    play_next(env)
    assert not first_link.exists()


def test_skip_endpoint_uses_engine(env, admin):
    _upload(env, "skip", "Skip")
    env.client.post("/api/admin/station/start")
    response = env.client.post("/api/admin/station/skip")
    assert response.status_code == 200
    assert env.engine.skip_calls == 1
