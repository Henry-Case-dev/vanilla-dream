#!/usr/bin/env python3
"""Production API flow check for Vanilla Dream Radio.

Runs against a live installation (localhost by default) with the admin password
read from a file, and drives the real product: login, upload, playlist, schedule,
queue, station start, track change, and manual stop.

    python3 tests/e2e/api_flow.py --password-file /root/vdr-password --fixtures /tmp/vdr-fixtures

Uses only the Python standard library.  The uploaded fixtures stay in the
library; delete them from the panel (or with --cleanup) after the checks.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = "http://127.0.0.1:8100"

RESULTS: list[tuple[str, bool, str]] = []


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""

    def request(self, method: str, path: str, body: bytes | None = None, ctype: str | None = None, timeout: float = 30.0):
        req = urllib.request.Request(self.base + path, data=body, method=method)
        if ctype:
            req.add_header("Content-Type", ctype)
        if self.csrf and method not in ("GET", "HEAD"):
            req.add_header("X-CSRF-Token", self.csrf)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, raw
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def json(self, method: str, path: str, payload=None, timeout: float = 30.0):
        body = json.dumps(payload).encode() if payload is not None else None
        status, raw = self.request(method, path, body, "application/json" if body else None, timeout)
        try:
            data = json.loads(raw or b"null")
        except json.JSONDecodeError:
            data = raw.decode("utf-8", "replace")
        return status, data

    def upload(self, path: str) -> tuple[int, dict]:
        boundary = "----vdr" + uuid.uuid4().hex
        name = os.path.basename(path)
        ctype = mimetypes.guess_type(name)[0] or "audio/mpeg"
        with open(path, "rb") as fh:
            content = fh.read()
        parts = [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'.encode(),
            f"Content-Type: {ctype}\r\n\r\n".encode(),
            content,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        status, raw = self.request("POST", "/api/admin/tracks/upload", b"".join(parts), f"multipart/form-data; boundary={boundary}", timeout=120)
        try:
            return status, json.loads(raw or b"null")
        except json.JSONDecodeError:
            return status, {"raw": raw.decode("utf-8", "replace")[:200]}


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + str(detail)) if detail else ''}")
    return ok


def wait_for(predicate, timeout: float, interval: float = 1.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


def items_of(data) -> list:
    if isinstance(data, dict):
        for key in ("items", "playlists", "tracks", "rules", "data"):
            if isinstance(data.get(key), list):
                return data[key]
    return data if isinstance(data, list) else []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--fixtures", default="/tmp/vdr-fixtures")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--skip-stop", action="store_true")
    args = parser.parse_args()

    password = open(args.password_file, encoding="utf-8").read().strip()
    files = sorted(
        os.path.join(args.fixtures, f)
        for f in os.listdir(args.fixtures)
        if f.lower().endswith(".mp3")
    )
    if len(files) < 3:
        print(f"need three fixture mp3 files in {args.fixtures}", file=sys.stderr)
        return 2

    c = Client(args.base)

    status, now = c.json("GET", "/api/public/now-playing")
    check("public now-playing", status == 200 and isinstance(now, dict) and "station" in now, status)

    status, login = c.json("POST", "/api/auth/login", {"password": "definitely-wrong"})
    check("login rejects wrong password", status in (400, 401, 403, 429), status)

    status, login = c.json("POST", "/api/auth/login", {"password": password})
    ok = status == 200 and isinstance(login, dict) and login.get("csrf_token")
    check("admin login", ok, status)
    if not ok:
        print("cannot continue without login", file=sys.stderr)
        return 1
    c.csrf = login["csrf_token"]

    status, me = c.json("GET", "/api/auth/me")
    check("session me", status == 200 and me.get("authenticated") is True)

    status, data = c.request("POST", "/api/admin/playlists", b'{"name":"x"}', "application/json")
    no_csrf = Client(args.base)
    no_csrf.jar = c.jar
    no_csrf.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(c.jar))
    s2, _ = no_csrf.request("POST", "/api/admin/playlists", b'{"name":"x"}', "application/json")
    check("csrf required for mutations", s2 in (400, 403), s2)

    upload_ids: list[str] = []
    for path in files:
        status, data = c.upload(path)
        track = data if isinstance(data, dict) else {}
        track_id = track.get("id") or (track.get("track") or {}).get("id")
        detail = track.get("detail") or {}
        existing = detail.get("existing_track_id") if isinstance(detail, dict) else None
        ok = status in (200, 201) and bool(track_id)
        if status == 409 and existing:
            track_id = existing
            ok = True
        check(f"upload {os.path.basename(path)}", ok, status)
        if track_id:
            upload_ids.append(track_id)

    status, dup = c.upload(files[0])
    dup_detail = (dup or {}).get("detail") or {}
    check("duplicate upload rejected", status == 409 and bool(dup_detail.get("existing_track_id")), status)

    status, library = c.json("GET", "/api/admin/tracks?limit=100")
    lib_items = items_of(library)
    check("library lists uploads", status == 200 and len(lib_items) >= len(upload_ids), f"{len(lib_items)} tracks")

    name = f"Проверка {uuid.uuid4().hex[:6]}"
    status, pl = c.json("POST", "/api/admin/playlists", {"name": name, "description": "e2e"})
    playlist_id = None
    if isinstance(pl, dict):
        playlist_id = pl.get("id") or (pl.get("playlist") or {}).get("id")
    check("create playlist", status in (200, 201) and bool(playlist_id), status)

    if playlist_id:
        status, _ = c.json("POST", f"/api/admin/playlists/{playlist_id}/tracks", {"track_ids": upload_ids})
        check("add tracks to playlist", status in (200, 204), status)
        status, detail = c.json("GET", f"/api/admin/playlists/{playlist_id}")
        tracks = items_of(detail) if isinstance(detail, dict) else []
        if not tracks and isinstance(detail, dict):
            tracks = items_of(detail.get("playlist", {}))
        check("playlist contains tracks", len(tracks) >= len(upload_ids), f"{len(tracks)}")
        status, _ = c.json("PATCH", f"/api/admin/playlists/{playlist_id}", {"shuffle": True, "repeat_mode": "all"})
        check("playlist shuffle/repeat", status in (200, 204), status)

    status, rule = c.json("POST", "/api/admin/schedule", {
        "name": "e2e all day",
        "playlist_id": playlist_id,
        "use_rotation": False,
        "days": [0, 1, 2, 3, 4, 5, 6],
        "start": "00:00",
        "end": "23:59",
        "enabled": True,
        "priority": 5,
        "transition": "after_track",
    })
    rule_id = None
    if isinstance(rule, dict):
        rule_id = rule.get("id") or (rule.get("rule") or {}).get("id")
    check("create schedule rule", status in (200, 201) and bool(rule_id), status)

    status, _ = c.json("PUT", "/api/admin/settings", {"schedule_enabled": 1, "default_playlist_id": playlist_id or ""})
    check("enable schedule", status in (200, 204), status)

    status, queue = c.json("GET", "/api/admin/queue")
    check("queue endpoint", status == 200 and isinstance(queue, dict))

    status, _ = c.json("POST", "/api/admin/station/start")
    check("station start accepted", status in (200, 202), status)

    def on_air():
        s, np = c.json("GET", "/api/public/now-playing")
        st = ((np or {}).get("station") or {}).get("status")
        return st == "on_air" and (np.get("track") or {}).get("id")

    result = wait_for(on_air, 60)
    check("station reaches on_air with a track", bool(result))

    time.sleep(6)
    s, np = c.json("GET", "/api/public/now-playing")
    listeners = (np.get("listeners") or {})
    check("listeners reported from icecast", listeners.get("available") is True, listeners)

    title_before = ((np.get("track") or {}).get("title"))
    status, _ = c.json("POST", "/api/admin/station/skip")
    check("skip accepted", status in (200, 202), status)

    def changed():
        s2, np2 = c.json("GET", "/api/public/now-playing")
        t = (np2.get("track") or {})
        return t.get("title") != title_before and t.get("id")

    check("track changes after skip", bool(wait_for(changed, 40)), title_before)

    status, history = c.json("GET", "/api/public/history?limit=10")
    check("history recorded", status == 200 and len(items_of(history)) >= 1)

    status, links = c.json("GET", "/api/admin/links")
    check("links endpoint", status == 200 and isinstance(links, (dict, list)))

    status, storage = c.json("GET", "/api/admin/storage")
    check("storage endpoint", status == 200 and isinstance(storage, dict))

    status, services = c.json("GET", "/api/admin/services")
    check("services endpoint", status == 200 and isinstance(services, (dict, list)))

    if not args.skip_stop:
        status, _ = c.json("POST", "/api/admin/station/stop")
        check("station stop accepted", status in (200, 202), status)

        def stopped():
            s2, np2 = c.json("GET", "/api/public/now-playing")
            return ((np2 or {}).get("station") or {}).get("status") == "off"

        check("station reports off", bool(wait_for(stopped, 40)))

    if args.cleanup:
        for track_id in upload_ids:
            c.json("DELETE", f"/api/admin/tracks/{track_id}")
        if playlist_id:
            c.json("DELETE", f"/api/admin/playlists/{playlist_id}")
        if rule_id:
            c.json("DELETE", f"/api/admin/schedule/{rule_id}")
        status, lib = c.json("GET", "/api/admin/tracks?limit=100")
        check("cleanup removed uploads", len(items_of(lib)) == 0, f"{len(items_of(lib))} left")

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
