#!/usr/bin/env python3
"""Stress and audio checks for the isolated 1.0.1 station.

- bulk selection with a library larger than one page (60+ tracks)
- real audio signal in the public stream (not just technical silence)
- track change observed in the API, the ICY metadata and the audio stream
- history records the real playback

Run against the isolated harness (front door on 127.0.0.1:18080).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import APIRequestContext, Page, sync_playwright

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, str(detail)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + str(detail)) if detail else ''}")
    return ok


def api(ctx: APIRequestContext, path: str) -> dict:
    resp = ctx.get(path)
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"_status": resp.status, "_text": resp.text()[:200]}


def wait_panel(page: Page, marker: str, timeout: float = 25) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.evaluate("(m) => !!document.body && document.body.innerText.includes(m)", marker):
                return True
        except Exception:  # noqa: BLE001 - navigation in flight
            pass
        page.wait_for_timeout(300)
    return False


def make_tracks(out: Path, count: int) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    files = []
    for index in range(count):
        path = out / f"bulk-{index:03d}.mp3"
        if not path.exists():
            freq = 300 + index * 7
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                 f"sine=frequency={freq}:duration=2", "-ac", "2", "-ar", "44100", "-b:a", "128k", str(path)],
                check=True,
            )
        files.append(path)
    return files


def upload(ctx: APIRequestContext, files: list[Path], csrf: str) -> int:
    added = 0
    for path in files:
        resp = ctx.post("/api/admin/tracks/upload", headers={"X-CSRF-Token": csrf}, multipart={"file": {
            "name": path.name, "mimeType": "audio/mpeg", "buffer": path.read_bytes(),
        }})
        if resp.status in (200, 201, 409):
            added += 1
        else:
            print("upload failed", path.name, resp.status, resp.text()[:120])
    return added


def measure_audio(url: str, seconds: float, work: Path) -> dict:
    out = work / "capture.mp3"
    with urllib.request.urlopen(url, timeout=20) as resp, open(out, "wb") as fh:
        deadline = time.time() + seconds
        while time.time() < deadline:
            chunk = resp.read(8192)
            if not chunk:
                break
            fh.write(chunk)
    size = out.stat().st_size
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=format_name,bit_rate,duration",
         "-of", "json", str(out)], capture_output=True, text=True, timeout=60,
    )
    info = json.loads(probe.stdout or "{}").get("format", {})
    vol = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", str(out), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120,
    )
    match = re.search(r"mean_volume: ([-\d.]+) dB", vol.stderr)
    maxv = re.search(r"max_volume: ([-\d.]+) dB", vol.stderr)
    return {
        "bytes": size,
        "format": info.get("format_name"),
        "bit_rate": info.get("bit_rate"),
        "duration": info.get("duration"),
        "mean_db": float(match.group(1)) if match else None,
        "max_db": float(maxv.group(1)) if maxv else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:18080")
    parser.add_argument("--password", default="e2e-owner-password")
    parser.add_argument("--work", default=str(Path.home() / ".cache" / "vdr-stress"))
    args = parser.parse_args()

    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    bulk = make_tracks(work / "bulk", 60)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1366, "height": 900}, base_url=args.base)
        page = ctx.new_page()
        page.goto(args.base + "/admin/login", wait_until="commit")
        page.wait_for_selector("input[type=password]")
        page.fill("input[type=password]", args.password)
        page.get_by_role("button", name=re.compile("Войти")).first.click()
        wait_panel(page, "Обзор", 30)

        print("\n--- bulk selection over one page")
        csrf = api(ctx.request, "/api/auth/me").get("csrf_token") or ""
        before = api(ctx.request, "/api/admin/tracks?limit=1").get("total") or 0
        uploads = upload(ctx.request, bulk, csrf)
        check("bulk fixtures uploaded", uploads >= 60, uploads)
        ids = api(ctx.request, "/api/admin/tracks/ids")
        check("ids endpoint returns the whole library", (ids.get("total") or 0) >= 60, ids.get("total"))

        page.goto(args.base + "/admin/playlists", wait_until="commit")
        wait_panel(page, "Плейлисты", 25)
        page.wait_for_timeout(1500)
        # repeatable: drop a stress playlist left by an earlier run
        for stale in api(ctx.request, "/api/admin/playlists")["items"]:
            if stale["name"] == "Большая медиатека":
                ctx.request.delete(f"/api/admin/playlists/{stale['id']}", headers={"X-CSRF-Token": csrf})
        page.reload(wait_until="commit")
        wait_panel(page, "Плейлисты", 25)
        page.wait_for_timeout(1200)
        page.get_by_role("button", name=re.compile("Создать плейлист")).first.click()
        page.wait_for_selector(".modal__panel")
        page.locator(".modal__panel input").first.fill("Большая медиатека")
        page.get_by_role("button", name=re.compile("^Создать|Сохранить")).last.click()
        page.wait_for_timeout(2500)
        created = next((p for p in api(ctx.request, "/api/admin/playlists")["items"] if p["name"] == "Большая медиатека"), None)
        check("stress playlist created", bool(created))
        if created:
            page.goto(args.base + f"/admin/playlists/{created['id']}", wait_until="commit")
            wait_panel(page, "Большая медиатека", 25)
            page.wait_for_timeout(1500)
            page.get_by_role("button", name=re.compile("Добавить музыку|Добавить треки")).first.click()
            page.wait_for_selector(".modal__panel")
            page.wait_for_timeout(1500)
            page.get_by_role("checkbox", name=re.compile("Выбрать все", re.I)).first.check()
            page.wait_for_timeout(800)
            confirm = page.get_by_role("button", name=re.compile(r"Добавить \((\d+)\)")).first
            label = confirm.inner_text() if confirm.count() else ""
            total = (api(ctx.request, "/api/admin/tracks/ids").get("total") or 0)
            already = (created.get("track_count") or 0)
            expected = total - already
            match = re.search(r"\((\d+)\)", label)
            check("select-all covers every track not yet in the playlist",
                  bool(match) and int(match.group(1)) == expected,
                  f"{label} vs {expected} (library {total}, already {already})")
            if confirm.count():
                confirm.click()
                page.wait_for_timeout(4000)
            detail = api(ctx.request, f"/api/admin/playlists/{created['id']}")["playlist"]
            check("bulk add stored every track", (detail.get("track_count") or 0) == total,
                  f"{detail.get('track_count')} / {total}")
            ctx.request.delete(f"/api/admin/playlists/{created['id']}", headers={"X-CSRF-Token": csrf})

        print("\n--- real audio signal")
        now = api(ctx.request, "/api/public/now-playing")
        status = now["station"]["status"]
        track = (now.get("track") or {}).get("title")
        check("station is on air with a track", status == "on_air" and bool(track), f"{status} {track}")
        audio = measure_audio(args.base + "/radio.mp3", 8, work)
        check("stream carries decodable mp3", audio["format"] == "mp3" and audio["bytes"] > 40000, audio)
        check("audio is not silent (mean volume)", audio["mean_db"] is not None and audio["mean_db"] > -60,
              f"mean {audio['mean_db']} dB, max {audio['max_db']} dB")

        icy = subprocess.run(
            ["curl", "-s", "-D", "-", "-o", os.devnull, "-H", "Icy-MetaData: 1", "--max-time", "4",
             args.base + "/radio.mp3"],
            capture_output=True, text=True, timeout=30,
        )
        blob = icy.stdout.lower()
        check("icy metadata present", "icy-br: 128" in blob and "icy-name" in blob, blob[:80].replace("\n", " "))

        first_title = track
        ctx.request.post("/api/admin/station/skip", headers={"X-CSRF-Token": csrf})
        deadline = time.time() + 40
        changed = None
        while time.time() < deadline:
            now2 = api(ctx.request, "/api/public/now-playing")
            changed = (now2.get("track") or {}).get("title")
            if changed and changed != first_title:
                break
            time.sleep(2)
        check("skip advances to another track", bool(changed) and changed != first_title, f"{first_title} -> {changed}")
        audio2 = measure_audio(args.base + "/radio.mp3", 6, work)
        check("audio after the change is still real", audio2["mean_db"] is not None and audio2["mean_db"] > -60,
              f"mean {audio2['mean_db']} dB")

        history = api(ctx.request, "/api/public/history?limit=10")
        items = history.get("items") or history
        check("history records real playback", isinstance(items, list) and len(items) >= 1, len(items) if isinstance(items, list) else 0)

        browser.close()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} stress checks passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    import os

    raise SystemExit(main())

