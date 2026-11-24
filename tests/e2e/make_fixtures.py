#!/usr/bin/env python3
"""Create temporary test audio for Vanilla Dream Radio verification.

Run with the project virtual environment (needs mutagen + Pillow):
    /opt/vanilla-dream-radio/venv/bin/python tests/e2e/make_fixtures.py --out /tmp/vdr-fixtures

Produces three short MP3 files with Cyrillic and Latin tags and one embedded
cover, used for upload, metadata, playlist, schedule and stream checks. The files
are technical fixtures only and must be deleted from production afterwards.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from mutagen.id3 import APIC, TALB, TCON, TIT2, TPE1, TDRC
from mutagen.mp3 import MP3
from PIL import Image, ImageDraw


def tone(path: Path, seconds: int, freq: int) -> None:
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
            "-ac", "2", "-ar", "44100", "-b:a", "128k",
            str(path),
        ],
        check=True,
    )


def cover(path: Path) -> None:
    img = Image.new("RGB", (600, 600), "#1C1B25")
    draw = ImageDraw.Draw(img)
    for i in range(600):
        draw.line([(0, i), (600, i)], fill=(188 - i // 6, 161 - i // 9, 244 - i // 12))
    img.save(path, "JPEG", quality=88)


def tag(path: Path, title: str, artist: str, album: str, genre: str, year: str, cover_path: Path | None = None) -> None:
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    audio.tags.delall("TIT2")
    audio.tags.delall("TPE1")
    audio.tags.delall("TALB")
    audio.tags.delall("TCON")
    audio.tags.delall("TDRC")
    audio.tags.add(TIT2(encoding=3, text=title))
    audio.tags.add(TPE1(encoding=3, text=artist))
    audio.tags.add(TALB(encoding=3, text=album))
    audio.tags.add(TCON(encoding=3, text=genre))
    audio.tags.add(TDRC(encoding=3, text=year))
    if cover_path is not None:
        audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_path.read_bytes()))
    audio.save()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/tmp/vdr-fixtures")
    parser.add_argument("--seconds", type=int, default=12)
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    first = out / "01-pervyy-test.mp3"
    second = out / "02-second-track.mp3"
    third = out / "03-tretiy.mp3"
    tone(first, args.seconds, 440)
    tone(second, args.seconds, 523)
    tone(third, args.seconds, 659)

    cover_file = out / "cover.jpg"
    cover(cover_file)

    tag(first, "Первый тест", "Ваниль", "Сиреневый эфир", "Ambient", "2026", cover_file)
    tag(second, "Second Track", "Vanilla Dream", "Test Album", "Chill", "2026")
    tag(third, "Третий", "Мечта", "Ночная дорога", "Night Drive", "2025", cover_file)

    for path in (first, second, third):
        audio = MP3(path)
        print(f"{path.name}: {audio.info.length:.1f}s {audio.info.bitrate // 1000} kbps tags={sorted(k.split(':')[0] for k in audio.tags.keys())}")


if __name__ == "__main__":
    main()
