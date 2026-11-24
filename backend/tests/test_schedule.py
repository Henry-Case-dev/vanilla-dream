"""SCHED-01..07: station timezone, overnight windows, priority, conflicts, overrides."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app import db, scheduler
from tests.helpers import make_playlist, play_next, upload_and_get, write_mp3


def _upload(env, name: str) -> dict:
    return upload_and_get(env.client, write_mp3(env.settings.music_dir.parent / f"{name}.mp3", title=name))


def _rule(days, start, end, priority=0, enabled=1, name="Rule", rotation=1, playlist_id=None):
    return {
        "id": uuid.uuid4().hex,
        "name": name,
        "playlist_id": playlist_id,
        "rotation": rotation,
        "days": scheduler.encode_days(days),
        "start_min": start,
        "end_min": end,
        "enabled": enabled,
        "priority": priority,
        "transition": "after_track",
    }


def _insert_rule(conn, rule):
    conn.execute(
        "INSERT INTO schedule_rules(id, name, playlist_id, rotation, days, start_min, end_min, "
        "enabled, priority, transition, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rule["id"],
            rule["name"],
            rule["playlist_id"],
            rule["rotation"],
            rule["days"],
            rule["start_min"],
            rule["end_min"],
            rule["enabled"],
            rule["priority"],
            rule["transition"],
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    conn.commit()


def _local(zone_name: str, text: str) -> datetime:
    from zoneinfo import ZoneInfo

    local = datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(zone_name))
    return local


def test_overnight_window_uses_start_day(env):
    rule = _rule([0], 22 * 60, 8 * 60)  # Monday 22:00 -> Tuesday 08:00
    assert scheduler.rule_matches(rule, _local("Asia/Magadan", "2026-11-23T23:00")) is True
    assert scheduler.rule_matches(rule, _local("Asia/Magadan", "2026-11-24T07:59")) is True
    assert scheduler.rule_matches(rule, _local("Asia/Magadan", "2026-11-24T08:00")) is False
    assert scheduler.rule_matches(rule, _local("Asia/Magadan", "2026-11-24T21:00")) is False
    assert scheduler.rule_matches(rule, _local("Asia/Magadan", "2026-11-22T23:00")) is False
    # A non-overnight rule is limited to its own day.
    daytime = _rule([0], 9 * 60, 17 * 60)
    assert scheduler.rule_matches(daytime, _local("Asia/Magadan", "2026-11-23T12:00")) is True
    assert scheduler.rule_matches(daytime, _local("Asia/Magadan", "2026-11-24T12:00")) is False


def test_dst_offsets_are_interpreted_in_station_zone(env):
    conn = db.open_db(env.settings)
    try:
        db.set_setting(conn, "timezone", "Europe/Berlin")
        early = _rule([6], 1 * 60 + 30, 2 * 60, name="Early")
        late = _rule([6], 3 * 60 + 30, 4 * 60, name="Late")
        _insert_rule(conn, early)
        _insert_rule(conn, late)
        before = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)
        changes = scheduler.upcoming_changes(conn, before, limit=10)
        starts = {}
        for change in changes:
            if change["event"] == "start":
                starts.setdefault(change["rule_name"], change["at"])
        # 2026-03-29 is the European DST switch: 01:30 CET is 00:30 UTC,
        # 03:30 CEST is 01:30 UTC (one UTC hour later, two wall-clock hours later).
        assert starts["Early"] == datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc)
        assert starts["Late"] == datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)
    finally:
        conn.close()


def test_priority_and_tie_breakers(env):
    conn = db.open_db(env.settings)
    try:
        db.set_setting(conn, "timezone", "UTC")
        low = _rule([0], 9 * 60, 12 * 60, priority=1, name="Low")
        high = _rule([0], 10 * 60, 11 * 60, priority=9, name="High")
        _insert_rule(conn, low)
        _insert_rule(conn, high)
        moment = datetime(2026, 11, 23, 10, 30, tzinfo=timezone.utc)
        assert scheduler.active_rule(conn, moment)["name"] == "High"

        conn.execute("DELETE FROM schedule_rules")
        early = _rule([0], 9 * 60, 12 * 60, priority=5, name="Early")
        late = _rule([0], 10 * 60, 12 * 60, priority=5, name="Late")
        _insert_rule(conn, early)
        _insert_rule(conn, late)
        assert scheduler.active_rule(conn, moment)["name"] == "Early"

        conn.execute("DELETE FROM schedule_rules")
        first = _rule([0], 10 * 60, 12 * 60, priority=5, name="First")
        first["id"] = "aaa"
        second = _rule([0], 10 * 60, 12 * 60, priority=5, name="Second")
        second["id"] = "bbb"
        _insert_rule(conn, first)
        _insert_rule(conn, second)
        assert scheduler.active_rule(conn, moment)["id"] == "aaa"

        disabled = _rule([0], 9 * 60, 12 * 60, priority=99, enabled=0, name="Off")
        _insert_rule(conn, disabled)
        assert scheduler.active_rule(conn, moment)["id"] == "aaa"

        db.set_setting(conn, "schedule_enabled", "0")
        conn.commit()
        assert scheduler.active_rule(conn, moment) is None
    finally:
        conn.close()


def test_conflicts_detection(env):
    conn = db.open_db(env.settings)
    try:
        db.set_setting(conn, "timezone", "UTC")
        conn.execute("DELETE FROM schedule_rules")
        night = _rule([0], 22 * 60, 8 * 60, priority=2, name="Night")
        morning = _rule([0, 1], 7 * 60, 9 * 60, priority=1, name="Morning")
        _insert_rule(conn, night)
        _insert_rule(conn, morning)
        conflicts = scheduler.conflicts(conn)
        assert len(conflicts) == 1
        conflict = conflicts[0]
        assert {conflict["a_name"], conflict["b_name"]} == {"Night", "Morning"}
        assert 1 in conflict["days"]  # Tuesday morning overlaps the overnight tail
        assert conflict["winner_id"] == night["id"]
        assert "пересекаются" in conflict["message"]

        conn.execute("DELETE FROM schedule_rules")
        _insert_rule(conn, _rule([2], 9 * 60, 10 * 60, name="A"))
        _insert_rule(conn, _rule([3], 9 * 60, 10 * 60, name="B"))
        assert scheduler.conflicts(conn) == []
    finally:
        conn.close()


def test_next_change_and_upcoming(env):
    conn = db.open_db(env.settings)
    try:
        db.set_setting(conn, "timezone", "UTC")
        _insert_rule(conn, _rule([0], 10 * 60, 12 * 60, name="Morning block"))
        now = datetime(2026, 11, 23, 9, 30, tzinfo=timezone.utc)
        at, name = scheduler.next_schedule_change(conn, now)
        assert at == datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
        assert name == "Morning block"
        upcoming = scheduler.upcoming_changes(conn, now, limit=3)
        assert upcoming[0]["event"] == "start"
        assert upcoming[1]["event"] == "end"
    finally:
        conn.close()


def test_schedule_api_validation(env, admin):
    track = _upload(env, "apl")
    playlist = make_playlist(env.client, "API", [track["id"]])
    base = {
        "name": "Test",
        "playlist_id": playlist["id"],
        "days": [0, 1],
        "start": "10:00",
        "end": "12:00",
    }
    assert env.client.post("/api/admin/schedule", json={**base, "days": []}).status_code == 422
    assert env.client.post("/api/admin/schedule", json={**base, "days": [7]}).status_code == 422
    assert env.client.post("/api/admin/schedule", json={**base, "start": "25:00"}).status_code == 422
    assert env.client.post("/api/admin/schedule", json={**base, "start": "10:00", "end": "10:00"}).status_code == 422
    assert (
        env.client.post("/api/admin/schedule", json={**base, "transition": "teleport"}).status_code
        == 422
    )
    created = env.client.post("/api/admin/schedule", json=base)
    assert created.status_code == 201
    rule = created.json()["rule"]
    assert rule["days"] == [0, 1]
    patched = env.client.patch(
        f"/api/admin/schedule/{rule['id']}", json={"enabled": False, "priority": 7}
    )
    assert patched.status_code == 200
    assert patched.json()["rule"]["enabled"] is False
    assert patched.json()["rule"]["priority"] == 7
    listing = env.client.get("/api/admin/schedule").json()
    assert listing["schedule_enabled"] is True
    assert listing["timezone"] == "Asia/Magadan"
    disabled = env.client.put("/api/admin/settings", json={"schedule_enabled": False})
    assert disabled.status_code == 200
    assert env.client.get("/api/admin/schedule").json()["schedule_enabled"] is False
    env.client.put("/api/admin/settings", json={"schedule_enabled": True})
    assert env.client.delete(f"/api/admin/schedule/{rule['id']}").status_code == 200
    assert env.client.get("/api/admin/schedule").json()["rules"] == []


def test_override_one_track_returns_to_default(env, admin):
    a_track = _upload(env, "ova")
    b_track = _upload(env, "ovb")
    playlist_a = make_playlist(env.client, "Default", [a_track["id"]])
    playlist_b = make_playlist(env.client, "Override", [b_track["id"]])
    env.client.put("/api/admin/settings", json={"default_playlist_id": playlist_a["id"]})
    env.app.state.scheduler.tick()

    response = env.client.post(
        "/api/admin/station/override",
        json={"kind": "playlist", "scope": "one_track", "playlist_id": playlist_b["id"]},
    )
    assert response.status_code == 200
    override = response.json()["override"]
    assert override["scope"] == "one_track"
    assert response.json()["queue"]["items"][0]["track"]["id"] == b_track["id"]

    started = play_next(env)
    assert started["track"]["id"] == b_track["id"]
    conn = db.open_db(env.settings)
    try:
        active = conn.execute("SELECT COUNT(*) FROM overrides WHERE active = 1").fetchone()[0]
    finally:
        conn.close()
    assert active == 0
    env.app.state.scheduler.tick()
    items = env.client.get("/api/admin/queue").json()["items"]
    assert items[0]["track"]["id"] == a_track["id"]


def test_override_until_time_and_schedule_change(env, admin):
    default_track = _upload(env, "ovd")
    override_track = _upload(env, "ovt")
    default_playlist = make_playlist(env.client, "Default", [default_track["id"]])
    override_playlist = make_playlist(env.client, "Timed", [override_track["id"]])
    env.client.put("/api/admin/settings", json={"default_playlist_id": default_playlist["id"]})

    until = (env.clock.now() + timedelta(minutes=30)).isoformat()
    timed = env.client.post(
        "/api/admin/station/override",
        json={
            "kind": "playlist",
            "scope": "until_time",
            "playlist_id": override_playlist["id"],
            "until": until,
        },
    )
    assert timed.status_code == 200, timed.text
    assert timed.json()["override"]["expires_at"]
    env.app.state.scheduler.tick()
    assert env.client.get("/api/admin/queue").json()["items"][0]["track"]["id"] == override_track["id"]

    env.clock.advance(31 * 60)
    env.app.state.scheduler.tick()
    conn = db.open_db(env.settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM overrides WHERE active = 1").fetchone()[0] == 0
    finally:
        conn.close()
    assert env.client.get("/api/admin/queue").json()["items"][0]["track"]["id"] == default_track["id"]

    # until_schedule_change expires at the next rule boundary.
    conn = db.open_db(env.settings)
    try:
        _insert_rule(conn, _rule([0], 19 * 60, 23 * 60, name="Evening"))
    finally:
        conn.close()
    boundary = env.client.post(
        "/api/admin/station/override",
        json={
            "kind": "playlist",
            "scope": "until_schedule_change",
            "playlist_id": override_playlist["id"],
        },
    )
    assert boundary.status_code == 200, boundary.text
    expires = boundary.json()["override"]["expires_at"]
    assert expires.startswith("2026-11-23T12:00:00")
    env.clock.advance(3 * 3600)
    env.app.state.scheduler.tick()
    conn = db.open_db(env.settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM overrides WHERE active = 1").fetchone()[0] == 0
    finally:
        conn.close()


def test_override_indefinite_and_clear(env, admin):
    a_track = _upload(env, "indA")
    b_track = _upload(env, "indB")
    playlist_a = make_playlist(env.client, "Default", [a_track["id"]])
    playlist_b = make_playlist(env.client, "Forever", [b_track["id"]])
    env.client.put("/api/admin/settings", json={"default_playlist_id": playlist_a["id"]})
    created = env.client.post(
        "/api/admin/station/override",
        json={"kind": "playlist", "scope": "indefinite", "playlist_id": playlist_b["id"]},
    )
    assert created.status_code == 200
    assert created.json()["override"]["expires_at"] is None
    for _ in range(3):
        assert play_next(env)["track"]["id"] == b_track["id"]

    cleared = env.client.delete("/api/admin/station/override")
    assert cleared.status_code == 200
    env.app.state.scheduler.tick()
    assert env.client.get("/api/admin/queue").json()["items"][0]["track"]["id"] == a_track["id"]


def test_expired_override_is_not_restored(env, admin):
    track = _upload(env, "exp")
    playlist = make_playlist(env.client, "Expired", [track["id"]])
    conn = db.open_db(env.settings)
    try:
        conn.execute(
            "INSERT INTO overrides(id, kind, playlist_id, scope, expires_at, active, created_at, note) "
            "VALUES('old', 'playlist', ?, 'until_time', ?, 1, '2026-01-01T00:00:00Z', NULL)",
            (playlist["id"], "2026-01-01T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()
    env.app.state.scheduler.tick()
    conn = db.open_db(env.settings)
    try:
        row = conn.execute("SELECT active FROM overrides WHERE id = 'old'").fetchone()
    finally:
        conn.close()
    assert row["active"] == 0
    env.client.get("/api/admin/queue")


def test_override_validation(env, admin):
    track = _upload(env, "val")
    playlist = make_playlist(env.client, "Val", [track["id"]])
    bad_kind = env.client.post(
        "/api/admin/station/override", json={"kind": "chaos", "scope": "indefinite"}
    )
    assert bad_kind.status_code == 422
    bad_scope = env.client.post(
        "/api/admin/station/override",
        json={"kind": "playlist", "scope": "forever", "playlist_id": playlist["id"]},
    )
    assert bad_scope.status_code == 422
    no_playlist = env.client.post(
        "/api/admin/station/override", json={"kind": "playlist", "scope": "indefinite"}
    )
    assert no_playlist.status_code == 422
    missing_playlist = env.client.post(
        "/api/admin/station/override",
        json={"kind": "playlist", "scope": "indefinite", "playlist_id": "missing"},
    )
    assert missing_playlist.status_code == 404
    past = env.client.post(
        "/api/admin/station/override",
        json={
            "kind": "playlist",
            "scope": "until_time",
            "playlist_id": playlist["id"],
            "until": "2020-01-01T00:00:00Z",
        },
    )
    assert past.status_code == 422


def test_immediate_transition_skips_current_track(env, admin):
    track = _upload(env, "immediate")
    playlist = make_playlist(env.client, "Immediate", [track["id"]])
    env.client.put("/api/admin/settings", json={"default_playlist_id": playlist["id"]})
    env.client.post("/api/admin/station/start")

    env.client.post(
        "/api/admin/schedule",
        json={
            "name": "Switch now",
            "playlist_id": playlist["id"],
            "days": [0, 1, 2, 3, 4, 5, 6],
            "start": "00:00",
            "end": "23:59",
            "priority": 10,
            "transition": "immediate",
        },
    )
    env.app.state.scheduler.tick()
    assert env.engine.skip_calls == 1
    env.app.state.scheduler.tick()
    assert env.engine.skip_calls == 1

    # A same-priority after_track rule taking over must not force a skip.
    env.client.post(
        "/api/admin/schedule",
        json={
            "name": "Later",
            "playlist_id": playlist["id"],
            "days": [0, 1, 2, 3, 4, 5, 6],
            "start": "00:00",
            "end": "23:59",
            "priority": 20,
            "transition": "after_track",
        },
    )
    env.app.state.scheduler.tick()
    assert env.engine.skip_calls == 1


def test_rule_without_source_is_rejected(env, admin):
    response = env.client.post(
        "/api/admin/schedule",
        json={
            "name": "e2e all day",
            "playlist_id": None,
            "use_rotation": False,
            "days": [0, 1, 2, 3, 4, 5, 6],
            "start": "00:00",
            "end": "23:59",
            "enabled": True,
            "priority": 5,
            "transition": "after_track",
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_rule_source"
    assert "плейлист" in detail["message"]
    assert env.client.get("/api/admin/schedule").json()["rules"] == []


def test_empty_scheduled_playlist_falls_back_to_library(env, admin):
    track = _upload(env, "fallback-a")
    _upload(env, "fallback-b")
    playlist = make_playlist(env.client, "Will empty", [track["id"]])
    rule = env.client.post(
        "/api/admin/schedule",
        json={
            "name": "All day",
            "playlist_id": playlist["id"],
            "use_rotation": False,
            "days": [0, 1, 2, 3, 4, 5, 6],
            "start": "00:00",
            "end": "23:59",
            "enabled": True,
            "priority": 10,
            "transition": "after_track",
        },
    )
    assert rule.status_code == 201, rule.text
    removed = env.client.delete(
        f"/api/admin/playlists/{playlist['id']}/tracks/{track['id']}"
    )
    assert removed.status_code == 200

    env.client.post("/api/admin/station/start")
    env.icecast.payload = {
        "available": True,
        "mount_present": True,
        "listeners": 1,
        "bitrate": 128,
        "title": None,
        "error": None,
    }
    started = play_next(env)
    assert started is not None
    env.app.state.scheduler.tick()
    payload = env.client.get("/api/public/now-playing").json()
    assert payload["track"] is not None
    assert payload["station"]["status"] != "no_music"
    items = env.client.get("/api/admin/queue").json()["items"]
    assert any(item["source"]["kind"] == "library" for item in items)
    assert not any(item["source"]["kind"] == "schedule" for item in items)

    conn = db.open_db(env.settings)
    try:
        warnings = conn.execute(
            "SELECT COUNT(*) FROM events WHERE message LIKE '%Правило расписания пропущено%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert warnings >= 1


def test_rule_without_rotation_sources_falls_back(env, admin):
    _upload(env, "rot-fallback-a")
    _upload(env, "rot-fallback-b")
    rule = env.client.post(
        "/api/admin/schedule",
        json={
            "name": "Rotation empty",
            "playlist_id": None,
            "use_rotation": True,
            "days": [0, 1, 2, 3, 4, 5, 6],
            "start": "00:00",
            "end": "23:59",
            "enabled": True,
            "priority": 9,
        },
    )
    assert rule.status_code == 201, rule.text
    env.app.state.scheduler.tick()
    items = env.client.get("/api/admin/queue").json()["items"]
    assert items
    assert all(item["source"]["kind"] == "library" for item in items)
    conn = db.open_db(env.settings)
    try:
        warnings = conn.execute(
            "SELECT COUNT(*) FROM events WHERE message LIKE '%Правило расписания пропущено%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert warnings >= 1


def test_stopped_station_is_not_started_by_schedule(env, admin):
    track = _upload(env, "stop")
    playlist = make_playlist(env.client, "Stop", [track["id"]])
    env.client.post(
        "/api/admin/schedule",
        json={
            "name": "Always",
            "playlist_id": playlist["id"],
            "days": [0, 1, 2, 3, 4, 5, 6],
            "start": "00:00",
            "end": "23:59",
            "priority": 10,
        },
    )
    env.client.post("/api/admin/station/stop")
    starts_before = [call for call in env.runner.calls if call[-2] == "start"]
    env.app.state.scheduler.tick()
    starts_after = [call for call in env.runner.calls if call[-2] == "start"]
    assert len(starts_after) == len(starts_before)
    status = env.client.get("/api/admin/station/status").json()
    assert status["desired"] == "stopped"
    assert status["state"] == "off"
