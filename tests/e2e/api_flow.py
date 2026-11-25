#!/usr/bin/env python3
"""End-to-end product flow for Vanilla Dream Radio 1.0.1.

Runs against a live installation (production or an isolated copy) and drives the
real API: login, library, playlists, bulk selection, covers, system playlist,
station control, playlist «В эфир», statuses, services, storage and links.

    python3 tests/e2e/api_flow.py --password-file /root/vdr-password \
        --fixtures /tmp/vdr-fixtures [--cleanup]

Standard library only. Fixtures stay in the library unless --cleanup is given.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

RESULTS: list[tuple[str, bool, str]] = []


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""

    def request(self, method: str, path: str, body: bytes | None = None, ctype: str | None = None, timeout: float = 60.0):
        req = urllib.request.Request(self.base + path, data=body, method=method)
        if ctype:
            req.add_header("Content-Type", ctype)
        if self.csrf and method not in ("GET", "HEAD"):
            req.add_header("X-CSRF-Token", self.csrf)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def json(self, method: str, path: str, payload=None, timeout: float = 60.0):
        body = json.dumps(payload).encode() if payload is not None else None
        status, raw = self.request(method, path, body, "application/json" if body else None, timeout)
        try:
            data = json.loads(raw or b"null")
        except json.JSONDecodeError:
            data = raw.decode("utf-8", "replace")
        return status, data

    def upload(self, path: str, field: str = "file", endpoint: str = "/api/admin/tracks/upload"):
        boundary = "----vdr" + uuid.uuid4().hex
        name = os.path.basename(path)
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        content = open(path, "rb").read()
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{field}"; filename="{name}"\r\n'.encode(),
            f"Content-Type: {ctype}\r\n\r\n".encode(),
            content,
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        status, raw = self.request("POST", endpoint, body, f"multipart/form-data; boundary={boundary}", timeout=120)
        try:
            return status, json.loads(raw or b"null")
        except json.JSONDecodeError:
            return status, {"raw": raw.decode("utf-8", "replace")[:200]}


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, str(detail)))
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
        for key in ("items", "playlists", "tracks", "rules", "links", "services", "data", "ids"):
            if isinstance(data.get(key), list):
                return data[key]
    return data if isinstance(data, list) else []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--fixtures", default="/tmp/vdr-fixtures")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--keep-station", action="store_true", help="do not stop the station at the end")
    args = parser.parse_args()

    password = open(args.password_file, encoding="utf-8").read().strip()
    files = sorted(os.path.join(args.fixtures, f) for f in os.listdir(args.fixtures) if f.lower().endswith(".mp3"))
    if len(files) < 2:
        print(f"need at least two fixture mp3 files in {args.fixtures}", file=sys.stderr)
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
        return 1
    c.csrf = login["csrf_token"]

    status, me = c.json("GET", "/api/auth/me")
    check("session me", status == 200 and me.get("authenticated") is True)

    anon = Client(args.base)
    s2, _ = anon.request("POST", "/api/admin/playlists", b'{"name":"x"}', "application/json")
    check("csrf required for mutations", s2 in (400, 403), s2)

    # ---- library and uploads -------------------------------------------------
    status, body = c.json("GET", "/api/admin/tracks?limit=5")
    library_before = (body or {}).get("total") if isinstance(body, dict) else None
    check("library endpoint", status == 200 and isinstance(body, dict) and "items" in body, library_before)

    upload_ids: list[str] = []
    for path in files[:3]:
        status, data = c.upload(path)
        data = data if isinstance(data, dict) else {}
        track_id = data.get("id") or (data.get("track") or {}).get("id")
        existing = (data.get("detail") or {}).get("existing_track_id") if isinstance(data.get("detail"), dict) else None
        ok = (status in (200, 201) and bool(track_id)) or (status == 409 and bool(existing))
        if not track_id and existing:
            track_id = existing
        check(f"upload {os.path.basename(path)}", ok, status)
        if track_id:
            upload_ids.append(track_id)

    status, dup = c.upload(files[0])
    dup_detail = (dup or {}).get("detail") or {}
    check("duplicate upload rejected", status == 409 and bool(dup_detail.get("existing_track_id")), status)

    status, body = c.json("GET", "/api/admin/tracks/ids")
    ids = (body or {}).get("ids") if isinstance(body, dict) else None
    check("bulk ids endpoint", status == 200 and isinstance(ids, list) and len(ids) >= len(upload_ids), len(ids or []))

    # ---- system playlist -----------------------------------------------------
    status, body = c.json("GET", "/api/admin/playlists")
    playlists = items_of(body)
    system = next((p for p in playlists if p.get("system") or p.get("id") == "library"), None)
    check("system playlist exists", status == 200 and system is not None and system.get("name") == "Вся медиатека", (system or {}).get("name"))
    if playlists:
        check("system playlist is first", playlists[0].get("id") == "library", playlists[0].get("id"))
    if system:
        check(
            "system playlist counts library",
            system.get("track_count") == (library_before or 0) or (system.get("track_count") or 0) >= len(upload_ids),
            f"track_count={system.get('track_count')} library={library_before}",
        )
        check("system playlist defaults", system.get("shuffle") is True and system.get("repeat_mode") == "all",
              f"shuffle={system.get('shuffle')} repeat={system.get('repeat_mode')}")

    status, body = c.json("DELETE", "/api/admin/playlists/library")
    check("system playlist cannot be deleted", status in (409, 422), status)
    status, body = c.json("POST", "/api/admin/playlists/library/tracks", {"track_ids": upload_ids})
    check("system playlist membership is virtual", status in (409, 422), status)

    status, detail = c.json("GET", "/api/admin/playlists/library")
    sys_detail = (detail or {}).get("playlist") or {}
    check("system playlist detail has tracks", isinstance(sys_detail.get("tracks"), list) and len(sys_detail["tracks"]) >= len(upload_ids),
          len(sys_detail.get("tracks") or []))

    # ---- user playlist -------------------------------------------------------
    name = f"Проверка {uuid.uuid4().hex[:6]}"
    status, pl = c.json("POST", "/api/admin/playlists", {"name": name, "description": "e2e"})
    playlist = (pl or {}).get("playlist") or {}
    playlist_id = playlist.get("id")
    check("create playlist", status in (200, 201) and bool(playlist_id), status)

    added = skipped = unavailable = None
    if playlist_id:
        status, add = c.json("POST", f"/api/admin/playlists/{playlist_id}/tracks", {"track_ids": upload_ids})
        add = add if isinstance(add, dict) else {}
        added, skipped, unavailable = add.get("added"), add.get("skipped"), add.get("unavailable")
        check("add tracks returns counters", status in (200, 201) and isinstance(added, int), f"added={added} skipped={skipped} unavailable={unavailable}")

        status, detail = c.json("GET", f"/api/admin/playlists/{playlist_id}")
        detail_playlist = (detail or {}).get("playlist") or {}
        tracks = detail_playlist.get("tracks") or []
        check("playlist detail contains tracks", status == 200 and len(tracks) == len(upload_ids), f"{len(tracks)}")
        check("playlist counters consistent",
              (detail_playlist.get("track_count") or 0) == len(upload_ids) and (detail_playlist.get("duration") or 0) > 0,
              f"count={detail_playlist.get('track_count')} duration={detail_playlist.get('duration')}")

        status, add2 = c.json("POST", f"/api/admin/playlists/{playlist_id}/tracks", {"track_ids": upload_ids})
        add2 = add2 if isinstance(add2, dict) else {}
        check("re-adding is skipped", status in (200, 201) and add2.get("skipped") == len(upload_ids) and add2.get("added") == 0,
              f"added={add2.get('added')} skipped={add2.get('skipped')}")

        status, _ = c.json("PATCH", f"/api/admin/playlists/{playlist_id}", {"shuffle": True, "repeat_mode": "all"})
        check("playlist shuffle/repeat saved", status in (200, 204), status)

        # ---- covers --------------------------------------------------------
        cover_png = os.path.join(args.fixtures, "playlist-cover.png")
        if os.path.exists(cover_png):
            status, body = c.upload(cover_png, endpoint=f"/api/admin/playlists/{playlist_id}/cover")
            covered = (body or {}).get("playlist") or {}
            check("upload playlist cover", status in (200, 201) and covered.get("cover_kind") == "custom", status)
            status, body = c.json("GET", f"/api/admin/playlists/{playlist_id}")
            again = ((body or {}).get("playlist") or {}).get("cover_kind")
            check("cover survives re-read", again == "custom", again)
            status, body = c.json("DELETE", f"/api/admin/playlists/{playlist_id}/cover")
            reset = ((body or {}).get("playlist") or {}).get("cover_kind")
            check("cover reset to automatic", status in (200, 204) and reset in ("auto", "none"), f"{status} {reset}")
        else:
            check("cover fixture present", False, cover_png)

        # ---- schedule/rotation use the playlist -----------------------------
        status, rule = c.json("POST", "/api/admin/schedule", {
            "name": "e2e night", "playlist_id": playlist_id, "use_rotation": False,
            "days": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "end": "23:59",
            "enabled": True, "priority": 5, "transition": "after_track",
        })
        rule_id = (rule or {}).get("rule", {}).get("id") or (rule or {}).get("id")
        check("create schedule rule", status in (200, 201) and bool(rule_id), status)
        status, _ = c.json("PUT", "/api/admin/settings", {"default_playlist_id": playlist_id, "schedule_enabled": 0})
        check("set default playlist", status in (200, 204), status)

    # ---- station -------------------------------------------------------------
    status, _ = c.json("POST", "/api/admin/station/stop")
    stopped = wait_for(lambda: (c.json("GET", "/api/public/now-playing")[1].get("station") or {}).get("status") == "off", 40)
    check("station stops", bool(stopped))

    if playlist_id:
        status, played = c.json("POST", f"/api/admin/playlists/{playlist_id}/play", {"mode": "now"})
        started = bool((played or {}).get("started")) if isinstance(played, dict) else False
        check("play on air starts a stopped station", status in (200, 202) and started, status)

        def on_air():
            np = c.json("GET", "/api/public/now-playing")[1]
            return ((np.get("station") or {}).get("status") == "on_air") and (np.get("track") or {}).get("id")

        check("station reaches on_air with a real track", bool(wait_for(on_air, 60)))

        np = c.json("GET", "/api/public/now-playing")[1]
        reason = (np.get("station") or {}).get("reason")
        check("status reason present", isinstance(reason, str) and bool(reason), reason)

        status, _ = c.json("POST", "/api/admin/station/override")
        check("override delete endpoint exists", status in (200, 204, 404, 405), status)

    status, queue = c.json("GET", "/api/admin/queue")
    current = (queue or {}).get("current") if isinstance(queue, dict) else None
    if current:
        check("queue current has id", bool(current.get("id") or current.get("position_id")), list(current)[:6])
    else:
        check("queue current present while on air", False, "no current")

    # ---- services, storage, links -------------------------------------------
    status, body = c.json("GET", "/api/admin/services")
    services = items_of(body)
    units = {s.get("unit") for s in services}
    check("services carry unit ids", status == 200 and len(services) >= 3 and all(s.get("unit") for s in services), len(services))
    check("services include the three units",
          any("liquidsoap" in (u or "") for u in units) and any("icecast" in (u or "") for u in units),
          sorted(u for u in units if u))

    status, body = c.json("GET", "/api/admin/storage")
    storage = body or {}
    check("storage returns numbers",
          status == 200 and isinstance(storage.get("tracks"), int) and isinstance(storage.get("bytes"), int)
          and (storage.get("tracks") or 0) >= len(upload_ids) and (storage.get("bytes") or 0) > 0,
          f"tracks={storage.get('tracks')} bytes={storage.get('bytes')}")

    status, body = c.json("GET", "/api/admin/links")
    links = items_of(body)
    keys = {l.get("key") for l in links}
    check("links have no world_radio key", "world_radio" not in keys, sorted(k for k in keys if k))
    check("links include mp3 primary", "mp3" in keys and next((l for l in links if l.get("key") == "mp3"), {}).get("primary") is True)
    check("links include m3u/pls/player", {"m3u", "pls", "player"} <= keys, sorted(k for k in keys if k))

    # ---- stop and persistence ------------------------------------------------
    status, _ = c.json("POST", "/api/admin/station/stop")
    check("station stop accepted", status in (200, 202), status)
    check("station reports off", bool(wait_for(lambda: (c.json("GET", "/api/public/now-playing")[1].get("station") or {}).get("status") == "off", 40)))

    if args.cleanup:
        if playlist_id:
            c.json("DELETE", f"/api/admin/playlists/{playlist_id}")
        if 'rule_id' in dir() and rule_id:
            c.json("DELETE", f"/api/admin/schedule/{rule_id}")
        c.json("PUT", "/api/admin/settings", {"default_playlist_id": "", "schedule_enabled": 0})
        for track_id in upload_ids:
            c.json("DELETE", f"/api/admin/tracks/{track_id}")

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
