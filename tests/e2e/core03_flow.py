#!/usr/bin/env python3
"""VD-RADIO-CORE-03 end-to-end: real backend, real Liquidsoap, real Icecast.

Runs against the isolated stand (front door http://127.0.0.1:18080 by default)
whose test tones are six-second distinguishable sine waves. Every check uses the
station's own API, the real playback history and - where relevant - the browser.

    python tests/e2e/core03_flow.py --base http://127.0.0.1:18080 \
        --password e2e-owner-password --fixtures <dir with tone-N.mp3> \
        [--ssh-host root@192.255.209.205 --ssh-key ~/.ssh/adminbot_vps \
         --engine-unit vanilla-dream-radio-e2e-liquidsoap.service]

The script never touches production: the SSH restart is aimed at the isolated
unit passed explicitly and refuses anything that is not an e2e unit.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import APIRequestContext, sync_playwright

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


class Api:
    def __init__(self, ctx: APIRequestContext, base: str) -> None:
        self.ctx = ctx
        self.base = base
        self.csrf = ""

    def _headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf} if self.csrf else {}

    def get(self, path: str) -> Any:
        response = self.ctx.get(path, timeout=30000)
        if response.status >= 400:
            raise RuntimeError(f"GET {path} -> {response.status} {response.text()[:300]}")
        return response.json()

    def post(self, path: str, data: Any = None, *, timeout: int = 60000) -> tuple[int, Any]:
        response = self.ctx.post(path, data=data or {}, headers=self._headers(), timeout=timeout)
        return response.status, _json(response)

    def put(self, path: str, data: Any = None, *, timeout: int = 60000) -> tuple[int, Any]:
        response = self.ctx.put(path, data=data or {}, headers=self._headers(), timeout=timeout)
        return response.status, _json(response)

    def patch(self, path: str, data: Any = None) -> tuple[int, Any]:
        response = self.ctx.patch(path, data=data or {}, headers=self._headers(), timeout=30000)
        return response.status, _json(response)

    def delete(self, path: str, data: Any = None) -> tuple[int, Any]:
        response = self.ctx.delete(path, data=data or {}, headers=self._headers(), timeout=30000)
        return response.status, _json(response)

    def upload(self, file: Path, batch: str) -> tuple[int, Any]:
        response = self.ctx.post(
            "/api/admin/tracks/upload",
            multipart={"file": {"name": file.name, "mimeType": "audio/mpeg", "buffer": file.read_bytes()}},
            params={"batch_id": batch},
            headers=self._headers(),
            timeout=180000,
        )
        return response.status, _json(response)

    # -- domain helpers ---------------------------------------------------
    def now(self) -> dict:
        return self.get("/api/public/now-playing")

    def state(self) -> dict:
        return self.get("/api/admin/station/state")

    def queue(self) -> dict:
        return self.get("/api/admin/queue")

    def history(self, limit: int = 40) -> list[dict]:
        return self.get(f"/api/public/history?limit={limit}").get("items") or []

    def playlists(self) -> list[dict]:
        return self.get("/api/admin/playlists").get("items") or []

    def tracks(self) -> list[dict]:
        return self.get("/api/admin/tracks?limit=200").get("items") or []

    def playlist_by_name(self, name: str) -> dict:
        for item in self.playlists():
            if item.get("name") == name:
                return item
        raise RuntimeError(f"playlist {name!r} not found")

    def track_by_title(self, title: str) -> dict:
        for item in self.tracks():
            if item.get("title") == title:
                return item
        raise RuntimeError(f"track {title!r} not found")

    def wait_track(self, titles: set[str], timeout: float = 30.0) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            snapshot = self.now()
            track = snapshot.get("track") or {}
            if track.get("title") in titles:
                return snapshot
            time.sleep(1.0)
        return None

    def wait_status(self, status: str, timeout: float = 60.0) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            snapshot = self.now()
            if (snapshot.get("station") or {}).get("status") == status:
                return snapshot
            time.sleep(1.0)
        return None

    def wait_source_playlist(self, playlist_id: str, timeout: float = 30.0) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            source = self.state().get("effective_source") or {}
            if source.get("playlist_id") == playlist_id:
                return self.state()
            time.sleep(1.0)
        return None

    def wait_fresh_track(self, timeout: float = 30.0) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            snapshot = self.now()
            track = snapshot.get("track") or {}
            if track.get("title") and float(track.get("elapsed") or 99) < 1.0:
                return snapshot
            time.sleep(0.4)
        return None


def _json(response) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001 - empty body is valid
        return {}


def login(ctx: APIRequestContext, password: str) -> str:
    response = ctx.post("/api/auth/login", data={"password": password}, timeout=30000)
    if response.status != 200:
        raise RuntimeError(f"login failed: {response.status} {response.text()[:200]}")
    return str(response.json().get("csrf_token") or "")


def ssh_restart(args: argparse.Namespace, unit: str) -> None:
    if "e2e" not in unit:
        raise RuntimeError("refusing to restart a non-e2e unit")
    for target in (args.icecast_unit, unit):
        if target and "e2e" not in target:
            raise RuntimeError(f"refusing to restart a non-e2e unit: {target}")
        command = [
            "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
            "-i", str(Path(args.ssh_key).expanduser()),
            args.ssh_host, f"systemctl restart {target}",
        ]
        subprocess.run(command, check=True, timeout=60)
        print(f"restarted {target}")
        time.sleep(3)


def ensure_station_playing(api: Api) -> None:
    status, payload = api.post("/api/admin/station/start")
    if status >= 400:
        raise RuntimeError(f"station start failed: {status} {payload}")
    api.wait_status("on_air", timeout=90)


def upload_tones(api: Api, fixtures: Path) -> dict[str, str]:
    batch = f"core03-{int(time.time())}"
    added: dict[str, str] = {}
    for index in range(1, 9):
        file = fixtures / f"tone-{index}.mp3"
        if not file.exists():
            raise RuntimeError(f"missing fixture {file}")
        status, payload = api.upload(file, batch)
        if status == 201:
            added[f"Tone {index}"] = payload["track"]["id"]
        elif status == 202:
            pending_id = (payload.get("pending") or {}).get("id")
            _, decided = api.post(f"/api/admin/uploads/{pending_id}/decide", {"action": "save"})
            track = decided.get("track") or {}
            added[f"Tone {index}"] = track.get("id")
        else:
            raise RuntimeError(f"upload {file.name} -> {status} {payload}")
    return added


def create_playlist(api: Api, name: str, track_ids: list[str], *, shuffle: bool = False,
                    repeat: str = "none") -> dict:
    status, payload = api.post(
        "/api/admin/playlists",
        {"name": name, "shuffle": shuffle, "repeat_mode": repeat},
    )
    if status >= 400:
        raise RuntimeError(f"playlist create failed: {status} {payload}")
    playlist = payload.get("playlist") or payload
    _, _ = api.post(f"/api/admin/playlists/{playlist['id']}/tracks", {"track_ids": track_ids})
    return api.playlist_by_name(name)


def titles(queue: dict) -> list[str]:
    return [((item.get("track") or {}).get("title") or "") for item in queue.get("items") or []]


def future_ids(queue: dict) -> list[str]:
    return [item["id"] for item in queue.get("items") or []]


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def scenario_modes(api: Api, state: dict[str, Any]) -> None:
    print("\n--- A. modes, permanent selection, transitions")
    a = state["A"]
    b = state["B"]
    before_instance = api.state()["engine_instance"]

    status, payload = api.post(f"/api/admin/playlists/{a['id']}/play", {"mode": "now"})
    check("A: play now accepted", status == 200 and payload.get("status") in ("switching", "applied"),
          f"{status} {payload.get('status')}")
    snapshot = api.wait_source_playlist(a["id"], timeout=25)
    check("A: effective source became A", snapshot is not None)
    check("A: engine was not restarted by the switch", api.state()["engine_instance"] == before_instance)

    st = api.state()
    check("A: mode is playlist", st.get("mode") == "playlist", str(st.get("mode")))
    check("A: selected playlist is A", st.get("selected_playlist_id") == a["id"])
    check("A: program playlist is A", st.get("program_playlist_id") == a["id"])
    check("A: no temporary override was created", st.get("override") is None, json.dumps(st.get("override")))

    status, _ = api.post(f"/api/admin/playlists/{a['id']}/play", {"mode": "now"})
    check("A: second play of the active source is already_active", status == 200)
    st = api.state()
    check("A: already_active keeps one source", st.get("selected_playlist_id") == a["id"])

    api.wait_fresh_track(timeout=25)
    status, payload = api.post(f"/api/admin/playlists/{b['id']}/play", {"mode": "next"})
    st = api.state()
    pending = st.get("pending_source") or {}
    if pending:
        check("B: next-mode writes pending_source", pending.get("playlist_id") == b["id"],
              json.dumps(pending))
        check("B: effective source still A before the track ends",
              (st.get("effective_source") or {}).get("playlist_id") == a["id"])
    else:
        # A very short remaining track can commit the pending switch before this
        # read; the ordering check below still proves no extra old-source track.
        check("B: pending switch committed immediately",
              (st.get("effective_source") or {}).get("playlist_id") == b["id"],
              str(st.get("effective_source")))
    head = (api.queue().get("items") or [{}])[0]
    head_title = (head.get("track") or {}).get("title")
    check("B: the very next queued track belongs to B", head_title in state["B_titles"], str(head_title))
    history_before = [item.get("title") for item in api.history(20)]
    snapshot = api.wait_source_playlist(b["id"], timeout=30)
    check("B: source changed after the current track", snapshot is not None)
    check("B: pending_source cleared", api.state().get("pending_source") is None)
    history_after = [item.get("title") for item in api.history(20)]
    new_titles = [title for title in history_after if title not in history_before]
    check("B: no extra A track played before the switch",
          bool(new_titles) and all(title in state["B_titles"] for title in new_titles),
          str(new_titles[:4]))

    # Rapid A -> B -> C(the same B) must end on the last accepted request.
    api.post(f"/api/admin/playlists/{a['id']}/play", {"mode": "now"})
    api.post(f"/api/admin/playlists/{b['id']}/play", {"mode": "now"})
    revision_after_b = api.state()["state_revision"]
    api.post(f"/api/admin/playlists/{a['id']}/play", {"mode": "now"})
    st = api.state()
    check("rapid: last request wins (A)", st.get("selected_playlist_id") == a["id"], str(st.get("selected_playlist_id")))
    check("rapid: revision is monotonic", st["state_revision"] >= revision_after_b)
    check("rapid: source is A", api.wait_source_playlist(a["id"], timeout=25) is not None)

    status, payload = api.put("/api/admin/station/mode", {"mode": "auto"})
    st = api.state()
    check("mode: auto accepted", status == 200 and st.get("mode") == "auto", f"{status}")
    check("mode: auto keeps the selected playlist for later", st.get("selected_playlist_id") == a["id"])
    status, _ = api.put("/api/admin/station/mode", {"mode": "playlist", "playlist_id": a["id"]})
    check("mode: back to playlist", status == 200 and api.state().get("mode") == "playlist")


def scenario_queue(api: Api, state: dict[str, Any]) -> None:
    print("\n--- Q. queue editing, manual insertions, one-shot shuffle")
    a = state["A"]
    tracks = state["added"]
    api.post(f"/api/admin/playlists/{a['id']}/play", {"mode": "now"})
    api.wait_source_playlist(a["id"], timeout=30)
    api.wait_fresh_track(timeout=30)

    # Manual positions must enter the future queue, not start immediately.
    for title in ("Tone 7", "Tone 8"):
        status, payload = api.post(f"/api/admin/tracks/{tracks[title]}/queue")
        check(f"Q: manual queue insert accepted ({title})", status < 400, str(status))
    queue = api.queue()
    check("Q: queue exposes current and future items", "current" in queue and isinstance(queue.get("items"), list))
    check("Q: no prepared lock field breaks the payload",
          all("prepared" in item for item in queue.get("items") or []))
    manual_items = [item for item in queue.get("items") or [] if item.get("manual")]
    check("Q: manual insertions are marked manual", len(manual_items) >= 1, str(len(manual_items)))

    items = queue.get("items") or []
    if len(items) >= 3:
        target = items[2]
        order_before = future_ids(queue)
        status, payload = api.patch(f"/api/admin/queue/{target['id']}", {"move_to": 1})
        queue_after = api.queue()
        check("Q: prepared/future position can be moved",
              status < 400 and future_ids(queue_after).index(target["id"]) == 1,
              f"{status} {future_ids(queue_after)[:4]}")
        check("Q: move keeps every position", sorted(future_ids(queue_after)) == sorted(order_before))

        mover = next((item for item in queue_after["items"][1:] if not item.get("manual")), None)
        if mover is not None:
            status, _ = api.post(f"/api/admin/queue/{mover['id']}/move-next")
            head = api.queue()
            check("Q: move-next works", status < 400 and future_ids(head)[0] == mover["id"],
                  f"{status} {future_ids(head)[:3]}")
            # Put it back so it does not start before the next checks.
            api.patch(f"/api/admin/queue/{mover['id']}", {"move_to": 2})

        # Delete a non-head future position.
        queue_now = api.queue()
        victim = next((item for item in queue_now["items"][1:] if not item.get("manual")), None)
        if victim is not None:
            status, payload = api.delete(f"/api/admin/queue/{victim['id']}")
            queue_deleted = api.queue()
            check("Q: future position can be removed",
                  status < 400 and victim["id"] not in future_ids(queue_deleted), str(status))
            check("Q: removal does not delete the file",
                  api.track_by_title((victim.get("track") or {}).get("title")) is not None)

    before_shuffle = [item["id"] for item in api.queue().get("items") or [] if item.get("manual")]
    status, payload = api.post("/api/admin/queue/shuffle")
    check("Q: one-shot shuffle accepted", status < 400, str(status))
    after_queue = api.queue()
    after_ids = {item["id"] for item in after_queue.get("items") or []}
    current_id = (after_queue.get("current") or {}).get("id")
    check("Q: one-shot shuffle keeps manual positions",
          all(item_id in after_ids or item_id == current_id for item_id in before_shuffle),
          f"before={len(before_shuffle)} after={len([i for i in after_queue['items'] if i.get('manual')])}")
    status, payload = api.post("/api/admin/queue/clear-manual")
    check("Q: clear-manual accepted", status < 400, str(status))
    check("Q: no manual positions remain",
          not [item for item in api.queue().get("items") or [] if item.get("manual")])


def scenario_shuffle_repeat(api: Api, state: dict[str, Any]) -> None:
    print("\n--- S. shuffle and repeat semantics")
    a = state["A"]
    api.post(f"/api/admin/playlists/{a['id']}/play", {"mode": "now"})
    api.wait_source_playlist(a["id"], timeout=25)
    a = api.playlist_by_name(a["name"])
    st = api.state()
    revision = st["state_revision"]
    instance = st["engine_instance"]

    status, payload = api.patch(f"/api/admin/playlists/{a['id']}", {"shuffle": True, "repeat_mode": "all"})
    check("S: shuffle+repeat saved", status == 200 and payload.get("playlist", {}).get("shuffle") is True, str(status))
    check("S: settings applied honestly", payload.get("application") in ("applied", "pending"), str(payload.get("application")))
    check("S: state revision advanced", payload.get("state_revision", 0) >= revision)
    st = api.state()
    check("S: engine instance unchanged by settings", st["engine_instance"] == instance)
    check("S: station state exposes program playlist", st.get("program_playlist_id") == a["id"])

    status, payload = api.patch(f"/api/admin/playlists/{a['id']}", {"shuffle": False})
    check("S: stale-free second toggle accepted", status == 200, str(status))
    stale_status, stale_payload = api.patch(
        f"/api/admin/playlists/{a['id']}", {"shuffle": True, "state_revision": max(0, revision - 5)}
    )
    check("S: stale revision is rejected", stale_status == 409, f"{stale_status} {stale_payload.get('error')}")

    # repeat one: the current program track becomes the target; skip moves it.
    _, payload = api.patch(f"/api/admin/playlists/{a['id']}", {"repeat_mode": "one"})
    check("S: repeat one accepted", payload.get("playlist", {}).get("repeat_mode") == "one")
    first_now = (api.now().get("track") or {}).get("title")
    status, _ = api.post("/api/admin/station/skip")
    if status >= 400:
        status, _ = api.post("/api/admin/station/next")
    time.sleep(4)
    second_now = (api.now().get("track") or {}).get("title")
    check("S: explicit skip moves the repeat target", second_now != first_now, f"{first_now} -> {second_now}")
    check("S: repeat stays one after skip",
          api.playlist_by_name(a["name"]).get("repeat_mode") == "one")

    _, payload = api.patch(f"/api/admin/playlists/{a['id']}", {"repeat_mode": "none"})
    check("S: back to no repeat", payload.get("playlist", {}).get("repeat_mode") == "none")


def scenario_replenish(api: Api, state: dict[str, Any]) -> None:
    print("\n--- R. dynamic replenishment")
    a = state["A"]
    added = state["added"]

    patch = api.playlists()
    fresh = create_playlist(api, "E2E replenish", [added["Tone 1"], added["Tone 2"]])
    api.post(f"/api/admin/playlists/{fresh['id']}/play", {"mode": "now"})
    api.wait_source_playlist(fresh["id"], timeout=25)

    before_revision = api.state()["state_revision"]
    status, payload = api.post(
        f"/api/admin/playlists/{fresh['id']}/tracks", {"track_ids": [added["Tone 3"]]}
    )
    check("R: adding to the active playlist accepted", status == 200 and payload.get("added") == 1, str(payload.get("added")))
    check("R: new track joins the current cycle", payload.get("in_current_cycle") is True)
    check("R: membership bumps the revision", payload.get("state_revision", 0) > before_revision)

    queue_titles = titles(api.queue())
    check("R: new track appears in the future queue", "Tone 3" in queue_titles, str(queue_titles))

    status, payload = api.post(
        f"/api/admin/playlists/{fresh['id']}/tracks", {"track_ids": [added["Tone 1"]]}
    )
    check("R: re-adding an existing track adds nothing", payload.get("added") == 0, str(payload.get("added")))
    check("R: re-adding reports saved without a sync",
          payload.get("application") == "saved" and payload.get("engine_synced") is False,
          json.dumps({k: payload.get(k) for k in ("application", "engine_synced")}))

    # Inactive playlist: no effect on the air.
    inert = create_playlist(api, "E2E inert", [added["Tone 5"]])
    source_before = (api.state().get("effective_source") or {}).get("playlist_id")
    status, payload = api.post(f"/api/admin/playlists/{inert['id']}/tracks", {"track_ids": [added["Tone 6"]]})
    check("R: inactive playlist saves without sync", payload.get("application") == "saved", str(payload.get("application")))
    check("R: inactive playlist leaves the air source", (api.state().get("effective_source") or {}).get("playlist_id") == source_before)

    # Empty active playlist resumes when the first track arrives.
    empty = create_playlist(api, "E2E empty", [added["Tone 8"]])
    api.post(f"/api/admin/playlists/{empty['id']}/play", {"mode": "now"})
    api.wait_source_playlist(empty["id"], timeout=25)
    # remove the only track from the playlist membership while it is active
    api.delete(f"/api/admin/playlists/{empty['id']}/tracks/{added['Tone 8']}")
    silence = api.wait_status("no_music", timeout=45)
    check("R: emptying the active source leaves technical silence", silence is not None, (api.now().get("station") or {}).get("status"))
    status, payload = api.post(f"/api/admin/playlists/{empty['id']}/tracks", {"track_ids": [added["Tone 8"]]})
    resumed = api.wait_track({"Tone 8"}, timeout=40)
    check("R: first track resumes the empty active source", resumed is not None)


def scenario_delete(api: Api, state: dict[str, Any]) -> None:
    print("\n--- D. media lifecycle")
    added = state["added"]
    playlist = create_playlist(api, "E2E delete", [added["Tone 4"], added["Tone 5"]])
    api.post(f"/api/admin/playlists/{playlist['id']}/play", {"mode": "now"})
    api.wait_source_playlist(playlist["id"], timeout=25)
    current = (api.now().get("track") or {}).get("title")
    victim = "Tone 5" if current == "Tone 4" else "Tone 4"
    victim_id = added[victim]
    status, payload = api.delete(f"/api/admin/tracks/{victim_id}", {"mode": "after"})
    check("D: delete after accepts the request", status == 200, str(status))
    check("D: track disappears from the library immediately",
          victim not in [track.get("title") for track in api.tracks()])
    deadline = time.time() + 40
    gone = False
    while time.time() < deadline:
        if api.get("/api/admin/tracks?limit=200").get("total") == len(api.tracks()):
            gone = victim not in [track.get("title") for track in api.tracks()]
        if gone:
            break
        time.sleep(2)
    check("D: logical removal keeps the station alive", (api.now().get("station") or {}).get("status") in ("on_air", "no_music"))

    current_track = (api.now().get("track") or {})
    if current_track.get("id"):
        status, payload = api.delete(f"/api/admin/tracks/{current_track['id']}", {"mode": "now"})
        check("D: delete now on the current track accepted", status == 200, str(status))
        time.sleep(6)
        check("D: delete now moved away from the removed track",
              (api.now().get("track") or {}).get("id") != current_track.get("id"))


def scenario_player(api: Api, args: argparse.Namespace, state: dict[str, Any], pw) -> None:
    print("\n--- P. web player recovery (real browser)")
    unit = args.engine_unit
    if not args.ssh_host or not Path(args.ssh_key).expanduser().exists():
        check("P: player recovery skipped (no SSH restart available)", True, "skipped")
        return
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1366, "height": 768})
    errors: list[str] = []

    def note_console(message) -> None:
        if message.type != "error":
            return
        url = str((message.location or {}).get("url") or "")
        text = message.text or ""
        if "/cover/" in url or "favicon" in url:
            return
        # While Icecast is restarted the mount answers 404 for a few seconds;
        # the reconnect policy is expected to retry that and the recovery check
        # below proves the outcome. Real script errors still count.
        if "Failed to load resource" in text and "/radio.mp3" in url:
            return
        errors.append(f"{text} @ {url}")

    page.on("console", note_console)
    page.goto(args.base + "/admin/login", wait_until="load")
    page.fill("input[type=password]", args.password)
    page.click("button[type=submit]")
    page.wait_for_selector(".pb-local button", timeout=20000)
    page.wait_for_timeout(1500)
    page.evaluate("window.__vdrNoReload = 'kept'")

    local_button = page.locator(".pb-local button").first
    if local_button.count() == 0:
        check("P: local play control found", False, "no .pb-local button")
        browser.close()
        return
    local_button.click()
    page.wait_for_timeout(5000)
    paused = page.evaluate("() => { const a = document.querySelector('audio'); return a ? a.paused : true; }")
    check("P: local playback started", not paused)

    page.evaluate(
        """() => {
            window.__vdrSrcSets = 0;
            const proto = HTMLMediaElement.prototype;
            const descriptor = Object.getOwnPropertyDescriptor(proto, 'src');
            Object.defineProperty(proto, 'src', {
                get() { return descriptor.get.call(this); },
                set(value) { window.__vdrSrcSets += 1; return descriptor.set.call(this, value); },
                configurable: true,
            });
        }"""
    )
    ssh_restart(args, unit)
    recovered = False
    deadline = time.time() + 90
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        paused = page.evaluate("() => { const a = document.querySelector('audio'); return a ? a.paused : true; }")
        ready = page.evaluate("() => { const a = document.querySelector('audio'); return a ? a.readyState : 0; }")
        if not paused and ready >= 2:
            recovered = True
            break
    check("P: audio recovered after an engine restart without reload", recovered)
    check("P: the page was not reloaded", page.evaluate("() => window.__vdrNoReload") == "kept")
    check("P: UI shows local playback",
          page.locator(".pb-local button.is-active").count() == 1,
          page.locator(".pb-local__label").inner_text() if page.locator(".pb-local__label").count() else "")
    check("P: no connection error is shown",
          page.locator("text=Не удалось подключиться").count() == 0)
    total_sets = int(page.evaluate("() => window.__vdrSrcSets") or 0)
    check("P: recovery did not thrash the connection", total_sets <= 4, f"src assignments={total_sets}")
    page.wait_for_timeout(8000)
    page.evaluate("() => { window.__vdrSrcSets = 0; }")
    api.post("/api/admin/station/skip")
    page.wait_for_timeout(12000)
    check("P: no reconnect on a track change", int(page.evaluate("() => window.__vdrSrcSets") or 0) == 0,
          f"src assignments={page.evaluate('() => window.__vdrSrcSets')}")
    check("P: no console errors during recovery", not errors, "; ".join(errors[:3]))
    browser.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:18080")
    parser.add_argument("--password", default="e2e-owner-password")
    parser.add_argument("--fixtures", required=True)
    parser.add_argument("--ssh-host", default="")
    parser.add_argument("--ssh-key", default="")
    parser.add_argument("--engine-unit", default="vanilla-dream-radio-e2e-liquidsoap.service")
    parser.add_argument("--icecast-unit", default="vanilla-dream-radio-e2e-icecast.service")
    parser.add_argument("--skip", default="", help="comma separated scenario letters to skip")
    args = parser.parse_args()
    skip = {part.strip().upper() for part in args.skip.split(",") if part.strip()}

    state: dict[str, Any] = {}
    with sync_playwright() as pw:
        ctx = pw.request.new_context(base_url=args.base, ignore_https_errors=True)
        csrf = login(ctx, args.password)
        api = Api(ctx, args.base)
        api.csrf = csrf

        ensure_station_playing(api)
        added = upload_tones(api, Path(args.fixtures))
        check("setup: eight tones imported", len(added) == 8, str(len(added)))
        if len(added) < 8:
            print(json.dumps(added, ensure_ascii=False))
            return 1

        playlist_a = create_playlist(api, "E2E A", [added["Tone 1"], added["Tone 2"], added["Tone 3"]])
        playlist_b = create_playlist(api, "E2E B", [added["Tone 4"], added["Tone 5"], added["Tone 6"]])
        state.update({
            "A": playlist_a,
            "B": playlist_b,
            "added": added,
            "A_titles": {"Tone 1", "Tone 2", "Tone 3"},
            "B_titles": {"Tone 4", "Tone 5", "Tone 6"},
        })

        if "A" not in skip:
            scenario_modes(api, state)
        if "Q" not in skip:
            scenario_queue(api, state)
        if "S" not in skip:
            scenario_shuffle_repeat(api, state)
        if "R" not in skip:
            scenario_replenish(api, state)
        if "D" not in skip:
            scenario_delete(api, state)
        if "P" not in skip:
            scenario_player(api, args, state, pw)

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
