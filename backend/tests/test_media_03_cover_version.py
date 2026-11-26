"""F1: versioned public cover URLs refresh after a replace or edit."""

from __future__ import annotations

from urllib.parse import urlsplit

from app import db
from app.models import cover_version, track_payload
from tests.helpers import make_playlist, play_next, write_mp3


def _post(env, path):
    return env.client.post(
        "/api/admin/tracks/upload",
        files={"file": (path.name, path.read_bytes(), "audio/mpeg")},
    )


def _added(env, path) -> dict:
    response = _post(env, path)
    assert response.status_code == 201, response.text
    return response.json()["track"]


def _pending(env, path) -> dict:
    response = _post(env, path)
    assert response.status_code == 202, response.text
    return response.json()["pending"]


def _decide(env, pending_id: str, action: str = "replace", track_id: str | None = None):
    body = {"action": action}
    if track_id:
        body["track_id"] = track_id
    return env.client.post(f"/api/admin/uploads/{pending_id}/decide", json=body)


def _add(env, name: str, **kwargs) -> dict:
    return _added(env, write_mp3(env.settings.music_dir.parent / f"{name}.mp3", **kwargs))


def _replacement(env, name: str, **kwargs):
    return write_mp3(env.settings.music_dir.parent / f"{name}.mp3", **kwargs)


def _cover_url(env, track_id: str) -> str:
    items = env.client.get("/api/admin/tracks", params={"q": ""}).json()["items"]
    return next(item["cover_url"] for item in items if item["id"] == track_id)


def _get_image(env, url: str):
    parts = urlsplit(url)
    return env.client.get(f"{parts.path}?{parts.query}" if parts.query else parts.path)


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


def test_cover_url_version_changes_after_replace_and_resolves(env, admin):
    track = _add(
        env, "f1-base", title="Обложка", artist="Автор", cover=True, seconds=1.0
    )
    before = _cover_url(env, track["id"])
    assert urlsplit(before).path.startswith("/api/public/cover/")
    assert "?v=" in before

    # The clock is frozen: the cover file token alone must bust the URL.
    pending = _pending(
        env,
        _replacement(
            env, "f1-new", title="Обложка", artist="Автор", cover=True, seconds=2.0
        ),
    )
    assert _decide(env, pending["id"]).json()["status"] == "replaced"

    after = _cover_url(env, track["id"])
    assert after != before
    assert urlsplit(after).path == urlsplit(before).path
    assert "?v=" in after

    response = _get_image(env, after)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("image/jpeg")
    conn = db.open_db(env.settings)
    try:
        row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track["id"],)).fetchone()
    finally:
        conn.close()
    assert response.content == (env.settings.cover_dir / row["cover_path"]).read_bytes()


def test_cover_url_version_changes_after_metadata_edit(env, admin):
    track = _add(env, "f1-edit", title="Правка", artist="Автор", cover=True)
    before = _cover_url(env, track["id"])

    env.clock.advance(1)
    patched = env.client.patch(
        f"/api/admin/tracks/{track['id']}", json={"title": "Правка 2"}
    )
    assert patched.status_code == 200, patched.text

    after = _cover_url(env, track["id"])
    assert after != before
    assert urlsplit(after).path == urlsplit(before).path
    assert "?v=" in after
    assert _get_image(env, after).status_code == 200


def test_public_now_playing_and_history_cover_urls_refresh(env, admin):
    current = _add(
        env, "f1-live", title="В эфире", artist="Автор", cover=True, seconds=1.0
    )
    following = _add(env, "f1-next", title="Следующий", artist="Автор", cover=True)
    playlist = make_playlist(env.client, "F1", [current["id"], following["id"]])
    _use_playlist(env, playlist["id"])
    _start(env)
    play_next(env)

    now_before = env.client.get("/api/public/now-playing").json()
    assert now_before["track"]["id"] == current["id"]
    live_before = now_before["track"]["cover_url"]
    assert urlsplit(live_before).path == f"/api/public/cover/{current['id']}"
    assert "?v=" in live_before
    history_before = env.client.get("/api/public/history").json()["items"][0]
    assert history_before["cover_url"] == live_before

    env.clock.advance(1)
    pending = _pending(
        env,
        _replacement(
            env, "f1-live-new", title="В эфире", artist="Автор", cover=True, seconds=3.0
        ),
    )
    assert _decide(env, pending["id"]).json()["status"] == "replaced"

    now_after = env.client.get("/api/public/now-playing").json()
    assert now_after["track"]["id"] == current["id"]
    live_after = now_after["track"]["cover_url"]
    assert live_after != live_before
    assert "?v=" in live_after
    assert urlsplit(live_after).path == urlsplit(live_before).path
    history_after = env.client.get("/api/public/history").json()["items"][0]
    assert history_after["cover_url"] == live_after
    assert _get_image(env, live_after).status_code == 200


def test_cover_url_without_timestamps_has_no_version(env, admin):
    row = {
        "id": "legacy-cover",
        "title": "Legacy",
        "cover_path": "legacy-cover.jpg",
        "custom_cover_path": None,
        "updated_at": None,
        "replaced_at": None,
    }
    assert cover_version(row) is None
    payload = track_payload(row, env.settings)
    assert payload["cover_url"] == (
        f"{env.settings.public_base_url}/api/public/cover/legacy-cover"
    )
    assert "?" not in payload["cover_url"]

    same_stamp = "2026-01-01T00:00:00.000Z"
    first = cover_version(
        {"updated_at": same_stamp, "cover_path": "one.jpg", "custom_cover_path": None}
    )
    second = cover_version(
        {"updated_at": same_stamp, "cover_path": "two.jpg", "custom_cover_path": None}
    )
    assert first and second and first != second
