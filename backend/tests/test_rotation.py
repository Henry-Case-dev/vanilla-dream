"""ROT-01..03: rotation modes, counters, editor, empty playlists, schedule priority."""

from __future__ import annotations

from app import db, queue_engine
from tests.helpers import make_playlist, play_next, upload_and_get, write_mp3


def _upload(env, name: str) -> dict:
    return upload_and_get(env.client, write_mp3(env.settings.music_dir.parent / f"{name}.mp3", title=name))


def _put_rotation(env, mode: str, items: list[dict]) -> dict:
    response = env.client.put("/api/admin/rotation", json={"mode": mode, "items": items})
    assert response.status_code == 200, response.text
    return response.json()["rotation"]


def _sources(env) -> list[dict]:
    return [item["source"] for item in env.client.get("/api/admin/queue").json()["items"]]


def test_sequential_rotation_cycles_playlists(env, admin):
    a_tracks = [_upload(env, "ra1"), _upload(env, "ra2")]
    b_tracks = [_upload(env, "rb1"), _upload(env, "rb2")]
    playlist_a = make_playlist(env.client, "Night drive", [t["id"] for t in a_tracks])
    playlist_b = make_playlist(env.client, "Chill", [t["id"] for t in b_tracks])
    rotation = _put_rotation(
        env,
        "sequential",
        [
            {"playlist_id": playlist_a["id"], "position": 0},
            {"playlist_id": playlist_b["id"], "position": 1},
        ],
    )
    assert rotation["mode"] == "sequential"
    env.app.state.scheduler.tick()

    items = env.client.get("/api/admin/queue").json()["items"]
    assert [item["track"]["id"] for item in items][:4] == [
        a_tracks[0]["id"],
        a_tracks[1]["id"],
        b_tracks[0]["id"],
        b_tracks[1]["id"],
    ]
    assert [item["source"]["playlist_id"] for item in items][:4] == [
        playlist_a["id"],
        playlist_a["id"],
        playlist_b["id"],
        playlist_b["id"],
    ]
    assert items[0]["source"]["kind"] == "rotation"

    started = []
    for _ in range(4):
        position = play_next(env)
        started.append((position["track"]["id"], position["source"]["playlist_id"]))
    assert [source for _track, source in started] == [
        playlist_a["id"],
        playlist_a["id"],
        playlist_b["id"],
        playlist_b["id"],
    ]


def test_by_count_rotation_respects_track_counts(env, admin):
    a_tracks = [_upload(env, f"bca{i}") for i in range(3)]
    b_tracks = [_upload(env, "bcb0")]
    playlist_a = make_playlist(env.client, "Five from A", [t["id"] for t in a_tracks])
    playlist_b = make_playlist(env.client, "One from B", [t["id"] for t in b_tracks])
    _put_rotation(
        env,
        "by_count",
        [
            {"playlist_id": playlist_a["id"], "position": 0, "track_count": 2},
            {"playlist_id": playlist_b["id"], "position": 1, "track_count": 1},
        ],
    )
    env.app.state.scheduler.tick()

    first_three = env.client.get("/api/admin/queue").json()["items"][:3]
    assert [item["source"]["playlist_id"] for item in first_three] == [
        playlist_a["id"],
        playlist_a["id"],
        playlist_b["id"],
    ]
    started = [play_next(env)["source"]["playlist_id"] for _ in range(3)]
    assert started == [playlist_a["id"], playlist_a["id"], playlist_b["id"]]

    conn = db.open_db(env.settings)
    try:
        rows = conn.execute(
            "SELECT id, item_id, played FROM rotation_state ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert any(row["id"].startswith("cursor:") for row in rows)
    assert any(row["id"].startswith("played:") for row in rows)


def test_random_rotation_avoids_immediate_repeat(env, admin):
    playlist_a = make_playlist(env.client, "Random A", [_upload(env, "rndA")["id"]])
    playlist_b = make_playlist(env.client, "Random B", [_upload(env, "rndB")["id"]])
    _put_rotation(
        env,
        "random",
        [
            {"playlist_id": playlist_a["id"], "position": 0},
            {"playlist_id": playlist_b["id"], "position": 1},
        ],
    )
    conn = db.open_db(env.settings)
    try:
        rotation = queue_engine.get_rotation(conn)
        current = None
        sequence = []
        for _ in range(20):
            item = queue_engine._advance_rotation(conn, rotation, current)
            assert item is not None
            current = str(item["id"])
            sequence.append(current)
    finally:
        conn.close()
    assert len(set(sequence)) == 2
    assert all(first != second for first, second in zip(sequence, sequence[1:]))


def test_rotation_editor_validation_and_preview(env, admin):
    playlist = make_playlist(env.client, "Editor", [_upload(env, "editor")["id"]])
    rotation = _put_rotation(
        env,
        "by_count",
        [{"playlist_id": playlist["id"], "position": 0, "track_count": 3, "enabled": True}],
    )
    assert rotation["items"][0]["track_count"] == 3
    assert rotation["items"][0]["enabled"] is True
    assert rotation["items"][0]["playlist_name"] == "Editor"

    preview = env.client.get("/api/admin/rotation/next-preview").json()
    assert preview["mode"] == "by_count"
    assert preview["current"]["playlist_id"] == playlist["id"]

    bad_mode = env.client.put("/api/admin/rotation", json={"mode": "chaos", "items": []})
    assert bad_mode.status_code == 422
    duplicate = env.client.put(
        "/api/admin/rotation",
        json={
            "mode": "sequential",
            "items": [
                {"playlist_id": playlist["id"], "position": 0},
                {"playlist_id": playlist["id"], "position": 1},
            ],
        },
    )
    assert duplicate.status_code == 422
    missing = env.client.put(
        "/api/admin/rotation",
        json={"mode": "sequential", "items": [{"playlist_id": "missing", "position": 0}]},
    )
    assert missing.status_code == 404


def test_empty_and_disabled_rotation_items_are_skipped(env, admin):
    empty = env.client.post("/api/admin/playlists", json={"name": "Empty"}).json()["playlist"]
    good_track = _upload(env, "good")
    good = make_playlist(env.client, "Good", [good_track["id"]])
    disabled_track = _upload(env, "disabled")
    disabled = make_playlist(env.client, "Disabled", [disabled_track["id"]])
    _put_rotation(
        env,
        "sequential",
        [
            {"playlist_id": empty["id"], "position": 0},
            {"playlist_id": disabled["id"], "position": 1, "enabled": False},
            {"playlist_id": good["id"], "position": 2},
        ],
    )
    env.app.state.scheduler.tick()
    items = env.client.get("/api/admin/queue").json()["items"]
    assert items
    assert all(item["track"]["id"] == good_track["id"] for item in items)

    conn = db.open_db(env.settings)
    try:
        warnings = conn.execute(
            "SELECT COUNT(*) FROM events WHERE level = 'warning' AND message LIKE '%Пустой плейлист%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert warnings >= 1


def test_rotation_ignores_playlist_repeat_all(env, admin):
    a_tracks = [_upload(env, f"rpa{i}") for i in range(2)]
    b_track = _upload(env, "rpb")
    playlist_a = make_playlist(
        env.client, "Repeat A", [t["id"] for t in a_tracks], repeat_mode="all"
    )
    playlist_b = make_playlist(env.client, "Next B", [b_track["id"]])
    _put_rotation(
        env,
        "sequential",
        [
            {"playlist_id": playlist_a["id"], "position": 0},
            {"playlist_id": playlist_b["id"], "position": 1},
        ],
    )
    env.app.state.scheduler.tick()
    sources = [item["source"]["playlist_id"] for item in env.client.get("/api/admin/queue").json()["items"]]
    assert sources[:3] == [playlist_a["id"], playlist_a["id"], playlist_b["id"]]


def test_schedule_takes_priority_over_rotation(env, admin):
    rotation_track = _upload(env, "rotprio")
    schedule_track = _upload(env, "schedprio")
    rotation_playlist = make_playlist(env.client, "Rotation", [rotation_track["id"]])
    schedule_playlist = make_playlist(env.client, "Scheduled", [schedule_track["id"]])
    _put_rotation(
        env,
        "sequential",
        [{"playlist_id": rotation_playlist["id"], "position": 0}],
    )
    # Fake clock: 2026-11-23 09:00 UTC == Monday 20:00 in Asia/Magadan.
    rule = env.client.post(
        "/api/admin/schedule",
        json={
            "name": "Evening",
            "playlist_id": schedule_playlist["id"],
            "days": [0],
            "start": "19:00",
            "end": "23:00",
            "enabled": True,
            "priority": 5,
            "transition": "after_track",
        },
    )
    assert rule.status_code == 201, rule.text
    env.app.state.scheduler.tick()
    items = env.client.get("/api/admin/queue").json()["items"]
    assert items
    first = items[0]
    assert first["source"]["kind"] == "schedule"
    assert first["source"]["playlist_id"] == schedule_playlist["id"]
    assert first["track"]["id"] == schedule_track["id"]
    # repeat none: after the scheduled playlist pass the rotation fallback may
    # follow, but it never precedes the active schedule rule.
    assert items[0]["source"]["kind"] != "rotation"
