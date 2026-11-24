"""Settings validation/engine include files, history, links, internal health."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import api_admin, api_internal, db
from app.config import SUPPORTED_BITRATES
from app.util import ApiError
from tests.helpers import make_playlist, play_next, upload_and_get, write_mp3


def _upload(env, name: str) -> dict:
    return upload_and_get(env.client, write_mp3(env.settings.music_dir.parent / f"{name}.mp3", title=name))


def test_settings_get_shape(env, admin):
    response = env.client.get("/api/admin/settings")
    assert response.status_code == 200
    body = response.json()
    settings = body["settings"]
    assert settings["station_name"] == "Vanilla Dream Radio"
    assert settings["bitrate"] == "128"
    assert settings["timezone"] == "Asia/Magadan"
    assert settings["queue_depth"] == "6"
    assert settings["desired_state"] == "stopped"
    assert not any(key.startswith("_") for key in settings)
    assert body["supported"]["bitrates"] == list(SUPPORTED_BITRATES)


def test_settings_validation(env, admin):
    invalid_bitrate = env.client.put("/api/admin/settings", json={"bitrate": 100})
    assert invalid_bitrate.status_code == 422
    distance = invalid_bitrate.json()["detail"]
    assert distance["code"] == "invalid_bitrate"
    assert "Доступны" in distance["message"]
    for value in SUPPORTED_BITRATES:
        assert str(value) in distance["message"]

    assert env.client.put("/api/admin/settings", json={"timezone": "Mars/Olympus"}).status_code == 422
    assert env.client.put("/api/admin/settings", json={"queue_depth": 0}).status_code == 422
    assert env.client.put("/api/admin/settings", json={"max_upload_mb": 0}).status_code == 422
    assert env.client.put("/api/admin/settings", json={"default_playlist_id": "missing"}).status_code == 404
    assert env.client.put("/api/admin/settings", json={"unknown": 1}).status_code == 422
    readonly = env.client.put("/api/admin/settings", json={"admin_password_set": "1"})
    assert readonly.status_code == 422
    assert readonly.json()["detail"]["code"] == "readonly_setting"
    bad_url = env.client.put("/api/admin/settings", json={"public_base_url": "ftp://nope"})
    assert bad_url.status_code == 422


def test_encoder_and_station_files_are_written(env, admin):
    response = env.client.put(
        "/api/admin/settings",
        json={
            "bitrate": 192,
            "station_name": 'Dream "FM"',
            "station_description": "Ночь \\ утро",
            "station_genre": "Ambient",
        },
    )
    assert response.status_code == 200, response.text
    assert env.settings.encoder_file.read_text(encoding="utf-8") == "encoder_bitrate = 192\n"
    station_text = env.settings.station_file.read_text(encoding="utf-8")
    assert 'station_name = "Dream \\"FM\\""' in station_text
    assert 'station_description = "Ночь \\\\ утро"' in station_text
    assert 'station_genre = "Ambient"' in station_text
    assert ":=" not in station_text

    # The restart only happens while the station is meant to be playing.
    assert not [call for call in env.runner.calls if call[-2] == "restart"]
    _upload(env, "cfg")
    env.client.post("/api/admin/station/start")
    restarted = env.client.put("/api/admin/settings", json={"bitrate": 160})
    assert restarted.status_code == 200
    body = restarted.json()
    assert body["engine_restarted"] is True
    assert "bitrate" in body["engine_fields_changed"]
    assert [call[-2] for call in env.runner.calls].count("restart") == 1
    assert env.settings.encoder_file.read_text(encoding="utf-8") == "encoder_bitrate = 160\n"


def test_unchanged_settings_do_not_rewrite_or_restart(env, admin, monkeypatch):
    _upload(env, "unchanged-cfg")
    env.client.post("/api/admin/station/start")
    changed = env.client.put("/api/admin/settings", json={"bitrate": 160})
    assert changed.status_code == 200
    assert changed.json()["engine_fields_changed"] == ["bitrate"]
    restarts = [call for call in env.runner.calls if call[-2] == "restart"]
    assert len(restarts) == 1

    writes: list[str] = []
    original = api_admin.atomic_write_text

    def spy(path, text, *args, **kwargs):
        writes.append(str(path))
        return original(path, text, *args, **kwargs)

    monkeypatch.setattr(api_admin, "atomic_write_text", spy)
    same = env.client.put(
        "/api/admin/settings",
        json={"bitrate": 160, "station_name": "Vanilla Dream Radio"},
    )
    assert same.status_code == 200, same.text
    body = same.json()
    assert body["engine_fields_changed"] == []
    assert body["engine_restarted"] is False
    assert writes == []
    assert len([call for call in env.runner.calls if call[-2] == "restart"]) == 1
    assert env.settings.encoder_file.read_text(encoding="utf-8") == "encoder_bitrate = 160\n"


def test_settings_desired_state_routes_to_station(env, admin):
    _upload(env, "desired")
    played = env.client.put("/api/admin/settings", json={"desired_state": "playing"})
    assert played.status_code == 200
    assert played.json()["station"]["desired"] == "playing"
    conn = db.open_db(env.settings)
    try:
        assert db.get_setting(conn, "desired_state") == "playing"
    finally:
        conn.close()
    stopped = env.client.put("/api/admin/settings", json={"desired_state": "stopped"})
    assert stopped.status_code == 200
    assert stopped.json()["station"]["state"] == "off"


def test_admin_history_and_pagination(env, admin):
    tracks = [_upload(env, f"page{i}") for i in range(3)]
    playlist = make_playlist(env.client, "Paging", [track["id"] for track in tracks])
    env.client.put(
        "/api/admin/settings", json={"default_playlist_id": playlist["id"]}
    )
    env.client.post("/api/admin/station/start")
    play_next(env)
    play_next(env)
    history = env.client.get("/api/admin/history", params={"limit": 1}).json()
    assert len(history["items"]) == 1
    assert history["items"][0]["title"] == "page1"

    page = env.client.get(
        "/api/admin/tracks", params={"limit": 2, "offset": 2, "sort": "created", "order": "asc"}
    ).json()
    assert page["total"] == 3
    assert len(page["items"]) <= 2


def test_unknown_route_is_russian_json(env):
    response = env.client.get("/api/admin/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"]["message"] == "Не найдено."

    response = env.client.get("/api/public/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "http_error"
    assert response.json()["detail"]["message"] == "Не найдено."


def test_internal_health_only_localhost(env):
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    assert api_internal.internal_health(request) == {"status": "ok"}
    remote = SimpleNamespace(client=SimpleNamespace(host="203.0.113.5"))
    with pytest.raises(ApiError) as excinfo:
        api_internal.internal_health(remote)
    assert excinfo.value.status_code == 404
    assert excinfo.value.message == "Не найдено."


def test_internal_track_start_requires_token(env):
    response = env.client.post(
        "/api/internal/engine/track-start", data={"vd": "x", "ts": "1"}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "forbidden"
