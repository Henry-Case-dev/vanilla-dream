"""Test helpers: silent MP3 generation, uploads, queue stepping."""

from __future__ import annotations

import io
from pathlib import Path

from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1

MP3_FRAME_LENGTH = 417
SAMPLES_PER_FRAME = 1152
SAMPLE_RATE = 44100


def silent_mp3_bytes(seconds: float = 1.0) -> bytes:
    frames = max(1, int(round(seconds * SAMPLE_RATE / SAMPLES_PER_FRAME)))
    header = bytes([0xFF, 0xFB, 0x90, 0x00])
    payload = header + b"\x00" * (MP3_FRAME_LENGTH - 4)
    return payload * frames


def jpeg_bytes(size: tuple[int, int] = (32, 32), color=(188, 161, 244)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    image = Image.new("RGB", size, color)
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def write_mp3(
    path: Path,
    *,
    seconds: float = 1.0,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    genre: str | None = None,
    year: str | None = None,
    cover: bool = False,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(silent_mp3_bytes(seconds))
    tags = ID3()
    if title is not None:
        tags.add(TIT2(encoding=3, text=[title]))
    if artist is not None:
        tags.add(TPE1(encoding=3, text=[artist]))
    if album is not None:
        tags.add(TALB(encoding=3, text=[album]))
    if genre is not None:
        tags.add(TCON(encoding=3, text=[genre]))
    if year is not None:
        tags.add(TDRC(encoding=3, text=[year]))
    if cover:
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="cover", data=jpeg_bytes()))
    tags.save(path, v2_version=3)
    return path


def upload_bytes(client, filename: str, data: bytes, *, expect: int | None = 201):
    response = client.post(
        "/api/admin/tracks/upload",
        files={"file": (filename, data, "audio/mpeg")},
    )
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def upload_mp3(client, path: Path, filename: str | None = None, *, expect: int | None = 201):
    return upload_bytes(client, filename or path.name, path.read_bytes(), expect=expect)


def upload_and_get(client, path: Path, filename: str | None = None) -> dict:
    response = upload_mp3(client, path, filename)
    return response.json()["track"]


def play_next(env) -> dict | None:
    """Start the first future position through the real internal endpoint."""

    queue = env.client.get("/api/admin/queue").json()
    if not queue["items"]:
        env.app.state.scheduler.tick()
        queue = env.client.get("/api/admin/queue").json()
    if not queue["items"]:
        return None
    position = queue["items"][0]
    timestamp = env.clock.now().timestamp()
    link_path = Path(env.settings.queue_link_dir) / f"{position['id']}.mp3"
    response = env.client.post(
        "/api/internal/engine/track-start",
        data={"f": str(link_path), "ts": f"{timestamp:.3f}"},
        headers={"X-Internal-Token": env.internal_token},
    )
    assert response.status_code == 200, response.text
    env.clock.advance(float(position["track"].get("duration") or 1.0) + 0.5)
    env.app.state.scheduler.tick()
    return position


def track_ids_in_queue(env) -> list[str]:
    return [item["track"]["id"] for item in env.client.get("/api/admin/queue").json()["items"]]


def make_playlist(client, name: str, track_ids: list[str], **kwargs) -> dict:
    response = client.post("/api/admin/playlists", json={"name": name, **kwargs})
    assert response.status_code == 201, response.text
    playlist = response.json()["playlist"]
    if track_ids:
        added = client.post(
            f"/api/admin/playlists/{playlist['id']}/tracks", json={"track_ids": track_ids}
        )
        assert added.status_code == 200, added.text
        playlist = added.json()["playlist"]
    return playlist
