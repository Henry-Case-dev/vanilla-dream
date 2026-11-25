"""PL-01..03: playlist CRUD, ordering, shared tracks, deletion safety."""

from __future__ import annotations

from app import db
from tests.helpers import make_playlist, play_next, upload_and_get, write_mp3


def _upload(env, name: str, title: str, seconds: float = 1.0) -> dict:
    return upload_and_get(
        env.client,
        write_mp3(env.settings.music_dir.parent / f"{name}.mp3", title=title, seconds=seconds),
    )


def test_playlist_crud(env, admin):
    created = env.client.post(
        "/api/admin/playlists",
        json={"name": "Ночная дорога", "description": "Для поздних поездок"},
    )
    assert created.status_code == 201
    playlist = created.json()["playlist"]
    assert playlist["name"] == "Ночная дорога"
    assert playlist["shuffle"] is False
    assert playlist["repeat_mode"] == "none"
    assert playlist["track_count"] == 0
    assert playlist["cover_url"] is None

    renamed = env.client.patch(
        f"/api/admin/playlists/{playlist['id']}",
        json={"name": "Night drive", "shuffle": True, "repeat_mode": "all"},
    ).json()["playlist"]
    assert renamed["name"] == "Night drive"
    assert renamed["shuffle"] is True
    assert renamed["repeat_mode"] == "all"

    invalid = env.client.patch(
        f"/api/admin/playlists/{playlist['id']}", json={"repeat_mode": "sometimes"}
    )
    assert invalid.status_code == 422
    assert "режим повторения" in invalid.json()["detail"]["message"].lower()

    listing = env.client.get("/api/admin/playlists").json()["items"]
    assert listing[0]["id"] == "library" and listing[0]["system"] is True
    assert [item["id"] for item in listing[1:]] == [playlist["id"]]

    deleted = env.client.delete(f"/api/admin/playlists/{playlist['id']}")
    assert deleted.status_code == 200
    assert env.client.get(f"/api/admin/playlists/{playlist['id']}").status_code == 404


def test_tracks_add_order_and_totals(env, admin):
    first = _upload(env, "p1", "Первая", seconds=1.0)
    second = _upload(env, "p2", "Вторая", seconds=2.0)
    third = _upload(env, "p3", "Третья", seconds=1.0)
    playlist = make_playlist(env.client, "Список", [first["id"], second["id"], third["id"]])
    detail = env.client.get(f"/api/admin/playlists/{playlist['id']}").json()["playlist"]
    assert [track["id"] for track in detail["tracks"]] == [first["id"], second["id"], third["id"]]
    assert detail["track_count"] == 3
    assert detail["duration"] > 3.5

    reordered = env.client.put(
        f"/api/admin/playlists/{playlist['id']}/order",
        json={"track_ids": [third["id"], first["id"], second["id"]]},
    )
    assert reordered.status_code == 200
    tracks = env.client.get(
        f"/api/admin/playlists/{playlist['id']}"
    ).json()["playlist"]["tracks"]
    assert [track["id"] for track in tracks] == [third["id"], first["id"], second["id"]]
    assert [track["position"] for track in tracks] == [0, 1, 2]

    bad_order = env.client.put(
        f"/api/admin/playlists/{playlist['id']}/order", json={"track_ids": [first["id"]]}
    )
    assert bad_order.status_code == 422
    assert bad_order.json()["detail"]["code"] == "invalid_order"

    removed = env.client.delete(
        f"/api/admin/playlists/{playlist['id']}/tracks/{second['id']}"
    )
    assert removed.status_code == 200
    assert removed.json()["playlist"]["track_count"] == 2

    duplicate_add = env.client.post(
        f"/api/admin/playlists/{playlist['id']}/tracks",
        json={"track_ids": [first["id"], "missing"]},
    ).json()
    assert duplicate_add["added"] == 0
    assert duplicate_add["skipped"] == 1
    assert duplicate_add["unavailable"] == 1


def test_shared_track_is_not_copied(env, admin):
    track = _upload(env, "shared", "Общая")
    one = make_playlist(env.client, "Один", [track["id"]])
    two = make_playlist(env.client, "Два", [track["id"]])
    assert one["track_count"] == 1 and two["track_count"] == 1
    conn = db.open_db(env.settings)
    try:
        copies = conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE sha256 = (SELECT sha256 FROM tracks WHERE id = ?)",
            (track["id"],),
        ).fetchone()[0]
        refs = conn.execute(
            "SELECT COUNT(*) FROM playlist_tracks WHERE track_id = ?", (track["id"],)
        ).fetchone()[0]
    finally:
        conn.close()
    assert copies == 1
    assert refs == 2


def test_delete_playlist_keeps_audio_and_cleans_references(env, admin):
    track = _upload(env, "keep", "Сохранить")
    playlist = make_playlist(env.client, "Удаляемый", [track["id"]])
    conn = db.open_db(env.settings)
    try:
        row = conn.execute("SELECT path FROM tracks WHERE id = ?", (track["id"],)).fetchone()
    finally:
        conn.close()
    audio = env.settings.music_dir / row["path"]

    env.client.put(
        "/api/admin/settings", json={"default_playlist_id": playlist["id"]}
    )
    env.client.post(f"/api/admin/playlists/{playlist['id']}/rotation")

    assert env.client.delete(f"/api/admin/playlists/{playlist['id']}").status_code == 200
    assert audio.exists()
    assert env.client.get(f"/api/admin/tracks").json()["total"] == 1

    conn = db.open_db(env.settings)
    try:
        assert conn.execute("SELECT value FROM settings WHERE key='default_playlist_id'").fetchone()[0] == ""
        assert conn.execute(
            "SELECT COUNT(*) FROM rotation_items WHERE playlist_id = ?", (playlist["id"],)
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_add_tracks_counts_existing_relation_before_file_check(env, admin):
    offline = _upload(env, "relation-offline", "Relation offline")
    readable = _upload(env, "relation-readable", "Relation readable")
    offline_playlist = make_playlist(env.client, "Offline relation", [offline["id"]])
    readable_playlist = make_playlist(env.client, "Readable relation", [readable["id"]])

    conn = db.open_db(env.settings)
    try:
        path = conn.execute(
            "SELECT path FROM tracks WHERE id = ?", (offline["id"],)
        ).fetchone()["path"]
    finally:
        conn.close()
    (env.settings.music_dir / path).unlink()

    offline_result = env.client.post(
        f"/api/admin/playlists/{offline_playlist['id']}/tracks",
        json={"track_ids": [offline["id"], offline["id"]]},
    ).json()
    assert offline_result["added"] == 0
    assert offline_result["skipped"] == 1
    assert offline_result["unavailable"] == 0

    readable_result = env.client.post(
        f"/api/admin/playlists/{readable_playlist['id']}/tracks",
        json={"track_ids": [readable["id"], readable["id"]]},
    ).json()
    assert readable_result["added"] == 0
    assert readable_result["skipped"] == 1
    assert readable_result["unavailable"] == 0


def test_playlist_play_sets_override_and_plays(env, admin):
    track = _upload(env, "play", "Запуск")
    playlist = make_playlist(env.client, "Эфир", [track["id"]])
    response = env.client.post(f"/api/admin/playlists/{playlist['id']}/play", json={"mode": "now"})
    assert response.status_code == 200
    body = response.json()
    assert body["started"] is True
    assert body["playlist"]["id"] == playlist["id"]
    assert body["station"]["desired"] == "playing"
    queue = env.client.get("/api/admin/queue").json()["items"]
    assert queue and queue[0]["track"]["id"] == track["id"]
    started = play_next(env)
    assert started["track"]["id"] == track["id"]

    conn = db.open_db(env.settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM overrides WHERE active = 1").fetchone()[0] == 1
    finally:
        conn.close()

    cleared = env.client.delete("/api/admin/station/override")
    assert cleared.status_code == 200
    conn = db.open_db(env.settings)
    try:
        active = conn.execute("SELECT COUNT(*) FROM overrides WHERE active = 1").fetchone()[0]
    finally:
        conn.close()
    assert active == 0


def test_playlist_cover_uses_first_track_cover(env, admin):
    plain = _upload(env, "plain", "Без обложки")
    with_cover = upload_and_get(
        env.client,
        write_mp3(env.settings.music_dir.parent / "covered.mp3", title="С обложкой", cover=True),
    )
    playlist = make_playlist(env.client, "Обложки", [plain["id"], with_cover["id"]])
    playlist = env.client.get(f"/api/admin/playlists/{playlist['id']}").json()["playlist"]
    assert playlist["cover_kind"] == "auto"
    assert playlist["cover_url"].split("?")[0].endswith(
        f"/api/public/playlist-cover/{playlist['id']}"
    )
    response = env.client.get(f"/api/public/playlist-cover/{playlist['id']}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
