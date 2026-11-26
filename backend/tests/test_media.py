"""MEDIA-01..09 / AUTH-03: ingest, dedupe, covers, metadata, streaming, deletion."""

from __future__ import annotations

import hashlib

from app import db
from tests.helpers import (
    jpeg_bytes,
    make_playlist,
    play_next,
    silent_mp3_bytes,
    upload_and_get,
    upload_bytes,
    upload_mp3,
    write_mp3,
)


def _music_files(env):
    return sorted(path.name for path in env.settings.music_dir.glob("*") if path.is_file())


def test_upload_extracts_tags_and_cover(env, admin):
    path = write_mp3(
        env.settings.music_dir.parent / "fixtures" / "Первый трек.mp3",
        title="Первый тест",
        artist="Ваниль",
        album="Сиреневый",
        genre="Dream",
        year="2026",
        cover=True,
    )
    track = upload_and_get(env.client, path)
    assert track["title"] == "Первый тест"
    assert track["artist"] == "Ваниль"
    assert track["album"] == "Сиреневый"
    assert track["genre"] == "Dream"
    assert track["year"] == 2026
    assert 0.5 < track["duration"] < 2.0
    assert track["bitrate"] == 128
    assert track["size"] > 0
    assert track["cover_url"].split("?")[0].endswith(f"/api/public/cover/{track['id']}")

    stored = db.open_db(env.settings)
    try:
        row = stored.execute("SELECT * FROM tracks WHERE id = ?", (track["id"],)).fetchone()
    finally:
        stored.close()
    assert row["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert (env.settings.music_dir / row["path"]).exists()
    assert "fixtures" in str(path) and "Первый трек.mp3" not in _music_files(env)
    assert (env.settings.cover_dir / f"{track['id']}.jpg").exists()

    cover = env.client.get(f"/api/public/cover/{track['id']}")
    assert cover.status_code == 200
    assert cover.headers["content-type"].startswith("image/jpeg")
    assert len(cover.content) > 100


def test_unicode_and_filename_fallback(env, admin):
    path = write_mp3(env.settings.music_dir.parent / "Без тегов.mp3")
    track = upload_and_get(env.client, path)
    assert track["title"] == "Без тегов"
    assert track["cover_url"] is None

    named = write_mp3(
        env.settings.music_dir.parent / "Ёлка — Письмо.mp3",
        title="Ёлка — Письмо",
        artist="Ёлка",
    )
    second = upload_and_get(env.client, named)
    assert second["title"] == "Ёлка — Письмо"
    assert second["artist"] == "Ёлка"


def test_duplicate_returns_pending_decision(env, admin):
    path = write_mp3(env.settings.music_dir.parent / "dup.mp3", title="Дубль")
    first = upload_and_get(env.client, path)
    response = upload_mp3(env.client, path, expect=None)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending"
    pending = body["pending"]
    assert pending["reason"] == "content"
    assert pending["match_track_id"] == first["id"]
    assert pending["matches"][0]["track"]["id"] == first["id"]
    assert env.client.get("/api/admin/tracks").json()["total"] == 1
    assert (env.settings.music_dir / ".pending").exists()
    assert any((env.settings.music_dir / ".pending").iterdir())


def test_corrupt_and_unsupported_files_rejected(env, admin):
    corrupt = upload_bytes(env.client, "broken.mp3", b"this is not audio at all", expect=None)
    assert corrupt.status_code == 422
    assert corrupt.json()["detail"]["code"] == "corrupt_audio"
    assert "повреждён" in corrupt.json()["detail"]["message"]

    bad_ext = upload_bytes(env.client, "track.txt", b"hello", expect=None)
    assert bad_ext.status_code == 415
    assert bad_ext.json()["detail"]["code"] == "unsupported_format"

    empty = upload_bytes(env.client, "empty.mp3", b"", expect=None)
    assert empty.status_code == 422
    assert empty.json()["detail"]["code"] == "empty_file"


def test_path_traversal_filename_is_not_used(env, admin):
    data = silent_mp3_bytes()
    response = upload_bytes(env.client, "../../../evil.mp3", data)
    track = response.json()["track"]
    assert track["title"] == "evil"
    root_files = [path.name for path in env.settings.music_dir.parent.glob("evil*")]
    assert root_files == []
    row = db.open_db(env.settings)
    try:
        stored = row.execute("SELECT path FROM tracks WHERE id = ?", (track["id"],)).fetchone()
    finally:
        row.close()
    assert (env.settings.music_dir / stored["path"]).resolve().is_relative_to(
        env.settings.music_dir.resolve()
    )
    files = _music_files(env)
    assert len(files) == 1
    assert files[0] == f"{track['id']}.mp3"


def test_metadata_edit_is_used_everywhere(env, admin):
    path = write_mp3(env.settings.music_dir.parent / "edit.mp3", title="Старое", artist="Кто-то")
    track = upload_and_get(env.client, path)
    updated = env.client.patch(
        f"/api/admin/tracks/{track['id']}",
        json={"title": "Новое имя", "artist": "Другой", "album": "Альбом", "genre": "Ambient", "year": 2024},
    )
    assert updated.status_code == 200, updated.text
    payload = updated.json()["track"]
    assert payload["title"] == "Новое имя"
    assert payload["genre"] == "Ambient"
    assert payload["year"] == 2024

    invalid_year = env.client.patch(f"/api/admin/tracks/{track['id']}", json={"year": "abcd"})
    assert invalid_year.status_code == 422
    assert invalid_year.json()["detail"]["code"] == "invalid_field"

    empty_title = env.client.patch(f"/api/admin/tracks/{track['id']}", json={"title": "   "})
    assert empty_title.status_code == 422

    env.client.post("/api/admin/station/start")
    play_next(env)
    now_playing = env.client.get("/api/public/now-playing").json()
    assert now_playing["track"]["title"] == "Новое имя"
    assert now_playing["track"]["artist"] == "Другой"


def test_stream_supports_range(env, admin):
    path = write_mp3(env.settings.music_dir.parent / "range.mp3", title="Range")
    track = upload_and_get(env.client, path)
    full = env.client.get(f"/api/admin/tracks/{track['id']}/stream")
    assert full.status_code == 200
    assert full.headers["content-type"].startswith("audio/mpeg")
    assert len(full.content) == track["size"]

    partial = env.client.get(
        f"/api/admin/tracks/{track['id']}/stream", headers={"Range": "bytes=0-99"}
    )
    assert partial.status_code == 206
    assert len(partial.content) == 100
    assert partial.headers["content-range"].startswith("bytes 0-99/")


def test_delete_cleans_relations_and_files(env, admin):
    first = upload_and_get(
        env.client, write_mp3(env.settings.music_dir.parent / "a.mp3", title="A")
    )
    second = upload_and_get(
        env.client, write_mp3(env.settings.music_dir.parent / "b.mp3", title="B")
    )
    playlist = make_playlist(env.client, "Смесь", [first["id"], second["id"]])
    assert playlist["track_count"] == 2

    response = env.client.delete(f"/api/admin/tracks/{first['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is True
    assert body["pending"] is False
    assert body["playlists_count"] == 1

    conn = db.open_db(env.settings)
    try:
        row = conn.execute("SELECT path, cover_path FROM tracks WHERE id = ?", (first["id"],)).fetchone()
        assert row is None
        refs = conn.execute(
            "SELECT COUNT(*) FROM playlist_tracks WHERE track_id = ?", (first["id"],)
        ).fetchone()[0]
        assert refs == 0
    finally:
        conn.close()


def test_delete_current_track_is_deferred(env, admin):
    first = upload_and_get(
        env.client, write_mp3(env.settings.music_dir.parent / "c.mp3", title="C")
    )
    second = upload_and_get(
        env.client, write_mp3(env.settings.music_dir.parent / "d.mp3", title="D")
    )
    playlist = make_playlist(env.client, "Очередь", [first["id"], second["id"]])
    env.client.post(f"/api/admin/playlists/{playlist['id']}/play")
    started = play_next(env)
    assert started["track"]["id"] == first["id"]
    conn = db.open_db(env.settings)
    try:
        row = conn.execute("SELECT path FROM tracks WHERE id = ?", (first["id"],)).fetchone()
    finally:
        conn.close()
    file_path = env.settings.music_dir / row["path"]
    assert file_path.exists()

    response = env.client.delete(f"/api/admin/tracks/{first['id']}")
    body = response.json()
    assert body["deleted"] is False
    assert body["pending"] is True
    assert body["playlists_count"] == 1
    assert body["current"] is True
    assert file_path.exists()

    play_next(env)
    assert not file_path.exists()
    conn = db.open_db(env.settings)
    try:
        assert conn.execute("SELECT 1 FROM tracks WHERE id = ?", (first["id"],)).fetchone() is None
    finally:
        conn.close()


def test_bulk_delete(env, admin):
    tracks = [
        upload_and_get(env.client, write_mp3(env.settings.music_dir.parent / f"bulk{i}.mp3", title=f"B{i}"))
        for i in range(3)
    ]
    response = env.client.post(
        "/api/admin/tracks/bulk-delete",
        json={"ids": [tracks[0]["id"], tracks[1]["id"], "missing"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert sorted(body["deleted"]) == sorted([tracks[0]["id"], tracks[1]["id"]])
    assert body["not_found"] == ["missing"]
    listing = env.client.get("/api/admin/tracks").json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == tracks[2]["id"]


def test_library_listing_search_sort(env, admin):
    for index, title in enumerate(["Альфа", "Бета", "Гамма"]):
        upload_and_get(
            env.client,
            write_mp3(env.settings.music_dir.parent / f"s{index}.mp3", title=title, artist="Исполнитель"),
        )
    listing = env.client.get("/api/admin/tracks", params={"q": "бета"}).json()
    assert listing["total"] == 1
    assert listing["items"][0]["title"] == "Бета"
    assert listing["library"]["count"] == 3
    assert listing["library"]["bytes"] > 0

    ordered = env.client.get("/api/admin/tracks", params={"sort": "title", "order": "asc"}).json()
    assert [item["title"] for item in ordered["items"]] == ["Альфа", "Бета", "Гамма"]

    bad_sort = env.client.get("/api/admin/tracks", params={"sort": "drop table"})
    assert bad_sort.status_code == 422
    assert "Недопустимая сортировка" in bad_sort.json()["detail"]["message"]


def test_large_upload_is_streamed_in_chunks(env, admin):
    path = write_mp3(
        env.settings.music_dir.parent / "large.mp3", title="Большой файл", seconds=150
    )
    assert path.stat().st_size > 1024 * 1024
    track = upload_and_get(env.client, path)
    assert track["size"] == path.stat().st_size
    assert track["duration"] > 100


def test_free_space_guard_rejects_upload(env, admin):
    import dataclasses

    env.app.state.settings = dataclasses.replace(
        env.app.state.settings, min_free_bytes=10**18
    )
    response = upload_bytes(
        env.client, "nospace.mp3", silent_mp3_bytes(), expect=None
    )
    assert response.status_code == 507
    assert response.json()["detail"]["code"] == "not_enough_space"
    assert "свободного места" in response.json()["detail"]["message"]


def test_cover_reencode_resizes_large_image(env, admin):
    path = write_mp3(
        env.settings.music_dir.parent / "bigcover.mp3",
        title="Big cover",
        cover=True,
    )
    from mutagen.id3 import APIC, ID3

    tags = ID3(path)
    tags.delall("APIC")
    big = jpeg_bytes((1600, 1200))
    tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=big))
    tags.save(path, v2_version=3)
    track = upload_and_get(env.client, path)
    from PIL import Image

    image = Image.open(env.settings.cover_dir / f"{track['id']}.jpg")
    assert max(image.size) <= 800
