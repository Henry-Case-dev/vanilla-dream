"""NOW-01..03: public now-playing shape, SSE, history, link files, covers."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import db
from app.api_public import _event_source
from app.main import create_app
from tests.helpers import make_playlist, play_next, upload_and_get, write_mp3


def _setup_playing(env, admin, *, listeners: int = 7):
    first = upload_and_get(
        env.client,
        write_mp3(env.settings.music_dir.parent / "now1.mp3", title="Первый", artist="Ваниль", cover=True),
    )
    second = upload_and_get(
        env.client,
        write_mp3(env.settings.music_dir.parent / "now2.mp3", title="Второй", artist="Мечта"),
    )
    playlist = make_playlist(env.client, "Эфир", [first["id"], second["id"]])
    env.client.put("/api/admin/settings", json={"default_playlist_id": playlist["id"]})
    env.client.post("/api/admin/station/start")
    env.icecast.payload = {
        "available": True,
        "mount_present": True,
        "listeners": listeners,
        "bitrate": 128,
        "title": "Ваниль - Первый",
        "error": None,
    }
    position = play_next(env)
    env.app.state.scheduler.tick()
    return first, second, playlist, position


def test_now_playing_shape(env, admin):
    first, second, playlist, position = _setup_playing(env, admin)
    conn = db.open_db(env.settings)
    try:
        row = conn.execute("SELECT started_at FROM playback_state WHERE singleton = 1").fetchone()
    finally:
        conn.close()
    from app.util import parse_iso

    started = parse_iso(row["started_at"])
    env.clock.set(started + timedelta(seconds=0.4))
    payload = env.client.get("/api/public/now-playing").json()

    station = payload["station"]
    base = env.settings.public_base_url
    assert station["name"] == "Vanilla Dream Radio"
    assert station["description"]
    assert station["status"] == "on_air"
    assert station["status_label"] == "В эфире"
    assert station["desired"] == "playing"
    assert station["stream_url"] == f"{base}/radio.mp3"
    assert station["m3u_url"] == f"{base}/radio.m3u"
    assert station["pls_url"] == f"{base}/radio.pls"
    assert station["bitrate"] == 128

    track = payload["track"]
    assert track["id"] == first["id"]
    assert track["title"] == "Первый"
    assert track["artist"] == "Ваниль"
    assert track["duration"] > 0
    assert 0.0 < track["progress"] <= 1.0
    assert 0.0 <= track["elapsed"] <= track["duration"]
    assert track["cover_url"].endswith(f"/api/public/cover/{first['id']}")
    assert track["started_at"]

    assert payload["source"]["kind"] == "playlist"
    assert payload["source"]["playlist_id"] == playlist["id"]
    assert payload["source"]["name"] == "Эфир"
    assert payload["listeners"] == {"available": True, "count": 7}
    assert payload["stats_available"] is True
    assert payload["next_track"]["id"] == second["id"]
    assert payload["next_schedule_change"] is None


def test_now_playing_is_public(env, admin):
    _setup_playing(env, admin)
    env.client.cookies.clear()
    public = env.client.get("/api/public/now-playing")
    assert public.status_code == 200
    assert env.client.get("/api/admin/settings").status_code == 401


def test_stats_unavailable_is_not_zero(env, admin):
    first = upload_and_get(
        env.client, write_mp3(env.settings.music_dir.parent / "nostats.mp3", title="No stats")
    )
    playlist = make_playlist(env.client, "Stats", [first["id"]])
    env.client.put("/api/admin/settings", json={"default_playlist_id": playlist["id"]})
    env.client.post("/api/admin/station/start")
    env.icecast.payload = {
        "available": False,
        "mount_present": False,
        "listeners": None,
        "bitrate": None,
        "title": None,
        "error": "stats_unavailable",
    }
    env.app.state.scheduler.tick()
    payload = env.client.get("/api/public/now-playing").json()
    assert payload["listeners"] == {"available": False, "count": None}
    assert payload["stats_available"] is False
    assert payload["station"]["bitrate"] is None
    assert payload["station"]["status_label"] != "В эфире"


def test_stopped_station_payload(env):
    payload = env.client.get("/api/public/now-playing").json()
    assert payload["station"]["status"] == "off"
    assert payload["station"]["status_label"] == "Остановлено"
    assert payload["track"] is None
    assert payload["source"]["kind"] == "none"
    assert payload["next_track"] is None
    assert payload["listeners"]["available"] is True
    assert payload["listeners"]["count"] == 0


def test_stopped_station_hides_last_track(env, admin):
    _setup_playing(env, admin)
    stopped = env.client.post("/api/admin/station/stop")
    assert stopped.status_code == 200
    env.app.state.scheduler.tick()
    payload = env.client.get("/api/public/now-playing").json()
    assert payload["station"]["desired"] == "stopped"
    assert payload["station"]["status"] == "off"
    assert payload["track"] is None
    assert payload["next_track"] is None
    assert payload["source"]["kind"] == "none"
    history = env.client.get("/api/public/history").json()["items"]
    assert history and history[0]["ended_at"] is not None


def test_history_endpoint(env, admin):
    first, second, playlist, position = _setup_playing(env, admin)
    second_position = play_next(env)
    history = env.client.get("/api/public/history", params={"limit": 10}).json()
    assert len(history["items"]) == 2
    assert history["items"][0]["track_id"] == second_position["track"]["id"]
    assert history["items"][1]["track_id"] == first["id"]
    assert history["items"][0]["title"] == "Второй"
    assert history["items"][0]["source"]["playlist_id"] == playlist["id"]
    assert history["items"][0]["ended_at"] is None
    assert history["items"][1]["ended_at"] is not None

    limited = env.client.get("/api/public/history", params={"limit": 1}).json()
    assert len(limited["items"]) == 1
    assert limited["items"][0]["track_id"] == second_position["track"]["id"]


def test_player_link_files(env, admin):
    _setup_playing(env, admin)
    base = env.settings.public_base_url
    m3u = env.client.get("/radio.m3u")
    assert m3u.status_code == 200
    assert m3u.headers["content-type"].startswith("audio/x-mpegurl")
    assert m3u.text.startswith("#EXTM3U")
    assert f"{base}/radio.mp3" in m3u.text

    pls = env.client.get("/radio.pls")
    assert pls.status_code == 200
    assert pls.headers["content-type"].startswith("audio/x-scpls")
    assert "NumberOfEntries=1" in pls.text
    assert f"File1={base}/radio.mp3" in pls.text
    assert "Version=2" in pls.text

    env.client.cookies.clear()
    assert env.client.get("/radio.m3u").status_code == 200
    assert env.client.get("/radio.pls").status_code == 200


def test_sse_first_event(env, admin):
    _setup_playing(env, admin)
    with env.client.stream("GET", "/api/public/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("data: "):
                payload = json.loads(line[len("data: "):])
                assert "station" in payload
                assert payload["station"]["status"] in {
                    "on_air",
                    "starting",
                    "reconnecting",
                    "no_music",
                    "off",
                    "error",
                }
                break
        else:  # pragma: no cover
            raise AssertionError("SSE stream produced no data event")


class _FakeSseRequest:
    def __init__(self, app, disconnected: bool = False):
        self.app = app
        self._disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self._disconnected


def _sse_app():
    state = SimpleNamespace(
        now_playing_changed=asyncio.Event(), shutdown_event=asyncio.Event()
    )
    return SimpleNamespace(state=state)


async def _collect_sse(app, request, *, shutdown_event=None, snapshot=None):
    chunks = []
    async for chunk in _event_source(
        request, shutdown_event=shutdown_event, snapshot=snapshot
    ):
        chunks.append(chunk)
    return chunks


def test_sse_stops_when_shutdown_event_is_set():
    app = _sse_app()
    app.state.shutdown_event.set()
    request = _FakeSseRequest(app)
    chunks = asyncio.run(
        _collect_sse(
            app,
            request,
            shutdown_event=app.state.shutdown_event,
            snapshot=lambda: {"station": {"status": "off"}},
        )
    )
    assert chunks == []


def test_sse_stops_on_disconnect():
    app = _sse_app()
    request = _FakeSseRequest(app, disconnected=True)
    chunks = asyncio.run(
        _collect_sse(
            app,
            request,
            shutdown_event=app.state.shutdown_event,
            snapshot=lambda: {"station": {"status": "off"}},
        )
    )
    assert chunks == []


def test_sse_returns_after_shutdown_during_stream():
    app = _sse_app()

    def snapshot():
        app.state.shutdown_event.set()
        return {"station": {"status": "off"}}

    request = _FakeSseRequest(app)
    chunks = asyncio.run(
        _collect_sse(
            app,
            request,
            shutdown_event=app.state.shutdown_event,
            snapshot=snapshot,
        )
    )
    assert len(chunks) == 1
    assert chunks[0].startswith("data: ")
    assert json.loads(chunks[0][len("data: ") :])["station"]["status"] == "off"


def test_lifespan_sets_shutdown_event(env):
    app = create_app(
        settings=env.settings,
        clock=env.clock,
        station=env.station,
        engine=env.engine,
        icecast=env.icecast,
    )
    assert not app.state.shutdown_event.is_set()
    with TestClient(app):
        assert not app.state.shutdown_event.is_set()
    assert app.state.shutdown_event.is_set()


def test_public_cover_404_for_missing(env):
    response = env.client.get("/api/public/cover/missing")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "cover_not_found"
    playlist_cover = env.client.get("/api/public/playlist-cover/missing")
    assert playlist_cover.status_code == 404


def test_public_base_url_setting_changes_links(env, admin):
    response = env.client.put(
        "/api/admin/settings", json={"public_base_url": "https://radio.example.test/"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["settings"]["public_base_url"] == "https://radio.example.test"
    payload = env.client.get("/api/public/now-playing").json()
    assert payload["station"]["stream_url"] == "https://radio.example.test/radio.mp3"
    assert "https://radio.example.test/radio.mp3" in env.client.get("/radio.m3u").text
    links = env.client.get("/api/admin/links").json()["links"]
    by_key = {item["key"]: item for item in links}
    assert by_key["mp3"]["url"] == "https://radio.example.test/radio.mp3"
    assert "world_radio" not in by_key


def test_cover_cache_headers(env, admin):
    first, _second, _playlist, _position = _setup_playing(env, admin)
    cover = env.client.get(f"/api/public/cover/{first['id']}")
    assert cover.status_code == 200
    assert "max-age" in cover.headers.get("cache-control", "")
