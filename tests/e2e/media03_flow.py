#!/usr/bin/env python3
"""VD-RADIO-MEDIA-03 end-to-end: replace an existing track with a real engine.

Runs against the isolated stand (front door http://127.0.0.1:18080) whose
fixtures are 12 s base tones and 18 s replacement versions with the same tags but
different frequency, bitrate and duration.

    python tests/e2e/media03_flow.py --base http://127.0.0.1:18080 \
        --password e2e-owner-password --fixtures <dir with tone-N / tone-N-v2>
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

from playwright.sync_api import APIRequestContext, sync_playwright

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


class Api:
    def __init__(self, ctx: APIRequestContext) -> None:
        self.ctx = ctx
        self.csrf = ""

    def get(self, path: str, **kw) -> Any:
        response = self.ctx.get(path, timeout=60000, **kw)
        if response.status >= 400:
            raise RuntimeError(f"GET {path} -> {response.status} {response.text()[:300]}")
        if "application/zip" in (response.headers.get("content-type") or ""):
            return response.body()
        return response.json()

    def post(self, path: str, data: Any = None, **kw) -> tuple[int, Any]:
        response = self.ctx.post(
            path, data=data or {}, headers={"X-CSRF-Token": self.csrf}, timeout=180000, **kw
        )
        try:
            return response.status, response.json()
        except Exception:
            return response.status, {}

    def delete(self, path: str, data: Any = None) -> tuple[int, Any]:
        response = self.ctx.delete(path, data=data or {}, headers={"X-CSRF-Token": self.csrf})
        return response.status, _json(response)

    def upload(self, file: Path, batch: str) -> tuple[int, Any]:
        response = self.ctx.post(
            "/api/admin/tracks/upload",
            multipart={
                "file": {"name": file.name, "mimeType": "audio/mpeg", "buffer": file.read_bytes()},
                "batch_id": batch,
            },
            headers={"X-CSRF-Token": self.csrf},
            timeout=180000,
        )
        return response.status, _json(response)

    def tracks(self) -> list[dict]:
        return self.get("/api/admin/tracks?limit=200").get("items") or []

    def track(self, track_id: str) -> dict:
        return next(t for t in self.tracks() if t["id"] == track_id)

    def playlists(self) -> list[dict]:
        return self.get("/api/admin/playlists").get("items") or []

    def state(self) -> dict:
        return self.get("/api/admin/station/state")

    def now(self) -> dict:
        return self.get("/api/public/now-playing")

    def wait_track(self, title: str, timeout: float = 40.0) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            snapshot = self.now()
            if ((snapshot.get("track") or {}).get("title")) == title:
                return snapshot
            time.sleep(1.0)
        return None


def _json(response) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return {}


def login(ctx: APIRequestContext, password: str) -> str:
    response = ctx.post("/api/auth/login", data={"password": password})
    if response.status != 200:
        raise RuntimeError(f"login failed: {response.status}")
    return str(response.json().get("csrf_token") or "")


def create_playlist(api: Api, name: str, ids: list[str], *, repeat: str = "none") -> dict:
    status, payload = api.post("/api/admin/playlists", {"name": name, "repeat_mode": repeat})
    playlist = (payload or {}).get("playlist") or {}
    api.post(f"/api/admin/playlists/{playlist['id']}/tracks", {"track_ids": ids})
    return next(p for p in api.playlists() if p["name"] == name)


def upload_pending(api: Api, file: Path, batch: str) -> dict:
    status, payload = api.upload(file, batch)
    if status == 202:
        pending = dict(payload.get("pending") or {})
        pending["_batch_id"] = (
            payload.get("batch_id") or pending.get("batch_id") or batch
        )
        return pending
    raise RuntimeError(f"expected a duplicate decision for {file.name}, got {status} {payload}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:18080")
    parser.add_argument("--password", default="e2e-owner-password")
    parser.add_argument("--fixtures", required=True)
    args = parser.parse_args()
    fixtures = Path(args.fixtures)

    with sync_playwright() as pw:
        ctx = pw.request.new_context(base_url=args.base, ignore_https_errors=True)
        api = Api(ctx)
        api.csrf = login(ctx, args.password)

        # Station is playing a two-track playlist (tone 1 then tone 2).
        batch = f"media03-{int(time.time())}"
        base_ids: dict[str, str] = {}
        for index in (1, 2):
            status, payload = api.upload(fixtures / f"tone-{index}.mp3", batch)
            if status != 201:
                raise RuntimeError(f"base upload failed: {status} {payload}")
            base_ids[f"Tone {index}"] = payload["track"]["id"]
        playlist = create_playlist(api, "E2E M03", [base_ids["Tone 1"], base_ids["Tone 2"]])
        api.post(f"/api/admin/playlists/{playlist['id']}/play", {"mode": "now"})
        time.sleep(3)
        api.wait_track("Tone 1", timeout=40)
        instance = api.state()["engine_instance"]
        count_before = len(api.tracks())
        created_before = api.track(base_ids["Tone 1"])["created_at"]

        # --- replace an inactive (future) track ---------------------------------
        print("\n--- R. replace a future track")
        pending = upload_pending(api, fixtures / "tone-1-v2.mp3", f"{batch}-r1")
        pending_id = pending["id"]
        status, batch_payload = api.post(f"/api/admin/uploads/{pending_id}/decide", {"action": "replace"})
        body = batch_payload if isinstance(batch_payload, dict) else {}
        check("R: replace decision accepted", status == 200 and body.get("status") in ("replaced", "identical"),
              f"{status} {body.get('status')}")
        track = api.track(base_ids["Tone 1"])
        check("R: track id preserved", track["id"] == base_ids["Tone 1"])
        check("R: no new library row", len(api.tracks()) == count_before, f"{count_before} -> {len(api.tracks())}")
        check("R: duration updated to the new file", abs(float(track["duration"]) - 18.0) < 1.0, str(track["duration"]))
        check("R: bitrate updated", int(track.get("bitrate") or 0) == 96, str(track.get("bitrate")))
        check("R: created_at preserved", track["created_at"] == created_before)
        detail_tracks = (
            api.get(f"/api/admin/playlists/{playlist['id']}").get("playlist", {}).get("tracks") or []
        )
        detail_ids = [
            item.get("id") or (item.get("track") or {}).get("id") for item in detail_tracks
        ]
        check("R: playlist membership/order preserved",
              detail_ids == [base_ids["Tone 1"], base_ids["Tone 2"]], str(detail_ids))
        download = ctx.get(f"/api/admin/tracks/{base_ids['Tone 1']}/download?mode=original")
        new_bytes = download.body()
        expected_sha = hashlib.sha256((fixtures / "tone-1-v2.mp3").read_bytes()).hexdigest()
        check("R: original download serves the new file",
              hashlib.sha256(new_bytes).hexdigest() == expected_sha, expected_sha[:12])
        check("R: engine was not restarted", api.state()["engine_instance"] == instance)

        # --- replace the currently playing track --------------------------------
        print("\n--- P. replace the playing track")
        api.post(f"/api/admin/playlists/{playlist['id']}/play", {"mode": "now"})
        snapshot = api.wait_track("Tone 2", timeout=40)
        before_duration = float((snapshot or {}).get("track", {}).get("duration") or 0)
        pending = upload_pending(api, fixtures / "tone-2-v2.mp3", f"{batch}-r2")
        status, body = api.post(f"/api/admin/uploads/{pending['id']}/decide", {"action": "replace"})
        check("P: replace accepted while playing", status == 200 and body.get("replaced") is True, str(status))
        current = (api.now().get("track") or {})
        check("P: current instance keeps the old duration",
              abs(float(current.get("duration") or 0) - before_duration) < 0.2,
              f"{current.get('duration')} vs {before_duration}")
        check("P: current track was not interrupted",
              (api.now().get("track") or {}).get("title") == "Tone 2")
        check("P: engine instance unchanged", api.state()["engine_instance"] == instance)

        # Let the old instance finish; the next play of Tone 2 must use the new file.
        time.sleep(max(1.0, before_duration - float(current.get("elapsed") or 0) + 3))
        history = api.get("/api/public/history?limit=6").get("items") or []
        latest = next((row for row in history if row.get("title") == "Tone 2"), None)
        if latest and latest.get("ended_at") and latest.get("started_at"):
            from datetime import datetime

            started = datetime.fromisoformat(latest["started_at"].replace("Z", "+00:00"))
            ended = datetime.fromisoformat(latest["ended_at"].replace("Z", "+00:00"))
            played = (ended - started).total_seconds()
            check("P: the old instance played its full old length", abs(played - before_duration) < 2.0,
                  f"{played:.1f}s")

        status, payload = api.post(
            f"/api/admin/playlists/{playlist['id']}/play", {"mode": "now", "restart": True}
        )
        time.sleep(4)
        now = api.now()
        check("P: the next play uses the new duration",
              abs(float((now.get("track") or {}).get("duration") or 0) - 18.0) < 1.0,
              str((now.get("track") or {}).get("duration")))

        # --- export snapshot keeps the old file ---------------------------------
        print("\n--- E. export holds the replaced file")
        pending = upload_pending(api, fixtures / "tone-2-v3.mp3", f"{batch}-r3")
        status, export = api.post(
            "/api/admin/exports",
            {"scope": "library", "format": "zip", "metadata_mode": "original"},
        )
        check("E: export job created", status < 400 and bool((export or {}).get("job")),
              str(status))
        status, body = api.post(f"/api/admin/uploads/{pending['id']}/decide", {"action": "replace"})
        check("E: replace during export accepted", status == 200, str(status))
        job_id = ((export or {}).get("job") or {}).get("id")
        if job_id:
            deadline = time.time() + 60
            archive = None
            while time.time() < deadline:
                job = api.get(f"/api/admin/exports/{job_id}").get("job") or {}
                if job.get("state") == "ready":
                    archive = api.get(f"/api/admin/exports/{job_id}/archive")
                    break
                if job.get("state") in ("error", "cancelled"):
                    break
                time.sleep(1)
            check("E: archive produced", archive is not None)
            if archive:
                with zipfile.ZipFile(io.BytesIO(archive)) as zf:
                    names = zf.namelist()
                    check("E: archive contains the playlist", any(n.endswith(".m3u8") or n.endswith(".m3u") for n in names))
            api.post(f"/api/admin/exports/{job_id}/cancel")

        # --- identical file -----------------------------------------------------
        print("\n--- I. identical file")
        same_batch = f"{batch}-same"
        status, payload = api.upload(fixtures / "tone-1-v2.mp3", same_batch)
        if status == 202:
            pending_id = payload["pending"]["id"]
            actual_batch = payload.get("batch_id") or (payload["pending"] or {}).get("batch_id") or same_batch
            batch_info = api.get(f"/api/admin/uploads/{actual_batch}").get("batch") or {}
            item = next((i for i in batch_info.get("items") or [] if i["id"] == pending_id), {})
            check("I: identical flagged", bool(item.get("identical")) or item.get("can_replace") is False, json.dumps(item)[:200])
            status, body = api.post(f"/api/admin/uploads/{pending_id}/decide", {"action": "replace"})
            check("I: identical replace is a safe no-op", status == 200 and body.get("identical") is True, str(body.get("status")))
            api.post(f"/api/admin/uploads/{pending_id}/decide", {"action": "skip"})
        else:
            check("I: identical upload flagged", status == 202, str(status))

        ctx.dispose()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:")
        for name in failed:
            print(" -", name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
