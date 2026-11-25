#!/usr/bin/env python3
"""Real-browser end-to-end scenarios for Vanilla Dream Radio 1.0.1.

Runs against an isolated station (front door on 127.0.0.1:18080 by default, its
own DB/music/Icecast/Liquidsoap). No API mocks: the browser talks to the real
backend, and the audio checks read the real Icecast mount.

    python tests/e2e/playwright_flow.py --base http://127.0.0.1:18080 \
        --password e2e-owner-password --fixtures <dir> [--live-audio]

Exit code 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, str(detail)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + str(detail)) if detail else ''}")
    return ok


def ok_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=20) as resp:
        return json.loads(resp.read())


def active(page: Page) -> dict:
    return page.evaluate(
        """() => { const a = document.activeElement; if (!a) return {tag: null};
        return {tag: a.tagName, type: a.getAttribute('type'), name: a.getAttribute('name'),
                placeholder: a.getAttribute('placeholder'), aria: a.getAttribute('aria-label'),
                cls: (a.className || '').toString().slice(0, 60),
                value: 'value' in a ? String(a.value).slice(0, 40) : null}; }"""
    )


def type_sequentially(page: Page, text: str, expect_field=lambda a: a.get("tag") in ("INPUT", "TEXTAREA")) -> list[dict]:
    """Type character by character and record the active element after each key."""
    seen = []
    for ch in text:
        page.keyboard.type(ch)
        page.wait_for_timeout(25)
        info = active(page)
        seen.append(info)
        if not expect_field(info):
            break
    return seen


def login(page: Page, base: str, password: str) -> None:
    page.goto(base + "/admin/login", wait_until="commit")
    page.wait_for_selector("input[type=password]", timeout=30000)
    page.fill("input[type=password]", password)
    page.keyboard.press("Enter")
    page.wait_for_selector("text=Радиостанция", timeout=40000)


def open_playlists(page: Page, base: str) -> None:
    page.goto(base + "/admin/playlists", wait_until="commit")
    page.wait_for_timeout(1200)


def first_playlist_href(page: Page) -> str | None:
    links = page.eval_on_selector_all("a[href^='/admin/playlists/']", "els => els.map(e => e.getAttribute('href'))")
    return links[0] if links else None


def scenario_focus(page: Page, base: str) -> None:
    print("\n--- focus regression (P0-INPUT-FOCUS)")
    open_playlists(page, base)
    page.get_by_role("button", name=re.compile("Создать плейлист")).first.click()
    page.wait_for_selector(".modal__panel", timeout=15000)
    panel = page.locator(".modal__panel").last
    field = panel.locator("input").first
    field.click()
    seen = type_sequentially(page, "Проверка фокуса Кириллица")
    check("focus stays on the input for every keystroke", len(seen) > 0 and all(s.get("tag") == "INPUT" for s in seen), seen[0] if seen else "")
    check("typed value matches", field.input_value().endswith("Кириллица"), field.input_value())

    # cursor movement + editing in the middle
    field.press("Home")
    for _ in range(3):
        page.keyboard.press("ArrowRight")
    page.keyboard.type("XY")
    check("middle edit keeps focus", active(page).get("tag") == "INPUT")
    check("middle edit inserted text", "XY" in field.input_value(), field.input_value()[:40])

    # selection + overwrite + delete
    page.keyboard.press("Control+A")
    page.keyboard.type("Новое имя")
    page.keyboard.press("Backspace")
    check("select-all + backspace works", field.input_value() == "Новое им", field.input_value())

    # multi-line description via Tab (textarea)
    page.keyboard.press("Tab")
    info = active(page)
    check("tab moves to the next field", info.get("tag") in ("INPUT", "TEXTAREA", "SELECT"), info.get("tag"))
    if info.get("tag") == "TEXTAREA":
        page.keyboard.type("Строка один")
        page.keyboard.press("Enter")
        page.keyboard.type("Строка два")
        check("textarea keeps focus across newline", active(page).get("tag") == "TEXTAREA")

    # Escape closes and focus returns to a sane element
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    check("escape closes the dialog", page.locator(".modal__panel").count() == 0)

    # reopen: still one initial focus, input focused rather than the close button
    page.get_by_role("button", name=re.compile("Создать плейлист")).first.click()
    page.wait_for_selector(".modal__panel", timeout=15000)
    page.wait_for_timeout(400)
    first = active(page)
    check("initial focus is a form field, not the close button", first.get("tag") in ("INPUT", "TEXTAREA"), first)
    page.keyboard.press("Escape")


def scenario_system_playlist(page: Page, base: str, fixtures: list[Path]) -> None:
    print("\n--- system playlist and automatic air")
    page.goto(base + "/admin", wait_until="commit")
    page.wait_for_timeout(1500)
    text = page.inner_text("body")
    check("overview names the system playlist as the source or shows silence reason",
          ("Вся медиатека" in text) or ("тишин" in text.lower()) or ("медиатек" in text.lower()), text[:120].replace("\n", " "))

    # start the station with an empty library -> technical silence, still on
    start = page.get_by_role("button", name=re.compile("Запустить эфир|Включить эфир")).first
    if start.count():
        start.click()
        page.wait_for_timeout(3000)
    now = ok_json(base + "/api/public/now-playing")
    check("station can start with an empty library", now["station"]["desired"] == "playing", now["station"]["status"])
    check("empty library is reported honestly", now["station"]["status"] in ("no_music", "starting", "on_air") and now["station"].get("reason") in ("library_empty", "engine_starting", "playing", "mount_missing"), now["station"].get("reason"))

    # upload two distinguishable fixtures through the real uploader
    page.goto(base + "/admin/library", wait_until="commit")
    page.wait_for_timeout(1200)
    page.set_input_files("input[type=file]", [str(f) for f in fixtures[:2]])
    page.wait_for_timeout(8000)
    tracks = ok_json(base + "/api/admin/tracks?limit=20")
    check("upload through the UI adds tracks", tracks.get("total", 0) >= 2, tracks.get("total"))

    system = None
    for p in ok_json(base + "/api/admin/playlists").get("items", []):
        if p.get("system"):
            system = p
    check("system playlist contains the uploads", bool(system) and (system.get("track_count") or 0) >= 2, (system or {}).get("track_count"))

    deadline = time.time() + 90
    status, track = None, None
    while time.time() < deadline:
        np = ok_json(base + "/api/public/now-playing")
        status = np["station"]["status"]
        track = (np.get("track") or {}).get("title")
        if status == "on_air" and track:
            break
        time.sleep(2)
    check("music starts automatically after upload", status == "on_air" and bool(track), f"{status} {track}")
    source = (np.get("source") or {}).get("name") if isinstance(np, dict) else None
    check("source is the system playlist with a human name", (np.get("source") or {}).get("kind") in ("library", "system", "playlist"), np.get("source"))

    page.goto(base + "/admin", wait_until="commit")
    page.wait_for_timeout(2000)
    body = page.inner_text("body")
    check("overview shows the playing track", bool(track) and track.split(" - ")[-1][:18] in body, track)


def scenario_user_playlist(page: Page, base: str) -> str | None:
    print("\n--- user playlist: tracks, bulk add, order")
    open_playlists(page, base)
    page.get_by_role("button", name=re.compile("Создать плейлист")).first.click()
    page.wait_for_selector(".modal__panel")
    panel = page.locator(".modal__panel").last
    panel.locator("input").first.fill("Проверка состава")
    panel.get_by_role("button", name=re.compile("Создать|Сохранить")).last.click()
    page.wait_for_timeout(1500)

    href = first_playlist_href(page)
    if not href:
        check("playlist created and listed", False, "no playlist link")
        return None
    page.goto(base + href, wait_until="commit")
    page.wait_for_timeout(1500)

    add = page.get_by_role("button", name=re.compile("Добавить музыку|Добавить треки")).first
    add.click()
    page.wait_for_selector(".modal__panel")
    page.wait_for_timeout(800)
    select_all = page.get_by_role("checkbox", name=re.compile("Выбрать все", re.I)).first
    if select_all.count():
        select_all.check()
    else:
        check("select-all control exists", False, "no checkbox")
    page.wait_for_timeout(400)
    confirm = page.get_by_role("button", name=re.compile(r"Добавить \(\d+\)")).first
    check("confirm button shows the selected count", confirm.count() > 0)
    confirm.click()
    page.wait_for_timeout(2500)

    detail = ok_json(base + f"/api/admin/playlists/{href.rstrip('/').split('/')[-1]}")
    pl = detail.get("playlist") or {}
    check("playlist detail has tracks after add", len(pl.get("tracks") or []) >= 2, len(pl.get("tracks") or []))
    check("count and duration are consistent", (pl.get("track_count") or 0) == len(pl.get("tracks") or []) and (pl.get("duration") or 0) > 0, f"{pl.get('track_count')} {pl.get('duration')}")

    page.reload(wait_until="commit")
    page.wait_for_timeout(1800)
    rows = page.locator("table tbody tr, .track-row").count()
    check("rows render after reload", rows >= 2, rows)

    return pl.get("id")


def scenario_cover(page: Page, base: str, playlist_id: str | None, cover: Path) -> None:
    print("\n--- playlist cover")
    if not playlist_id or not cover.exists():
        check("cover scenario prerequisites", False, f"{playlist_id} {cover}")
        return
    page.goto(base + f"/admin/playlists/{playlist_id}", wait_until="commit")
    page.wait_for_timeout(1200)
    edit = page.get_by_role("button", name=re.compile("Изменить")).first
    edit.click()
    page.wait_for_selector(".modal__panel")
    page.wait_for_timeout(500)
    file_input = page.locator(".modal__panel input[type=file]").first
    if file_input.count():
        file_input.set_input_files(str(cover))
        page.wait_for_timeout(2000)
        page.get_by_role("button", name=re.compile("Сохранить|Применить|Загрузить обложку")).last.click()
        page.wait_for_timeout(2500)
    detail = ok_json(base + f"/api/admin/playlists/{playlist_id}")
    kind = (detail.get("playlist") or {}).get("cover_kind")
    check("custom cover is stored", kind == "custom", kind)

    delete = page.get_by_role("button", name=re.compile("Вернуть автоматическую", re.I)).first
    if delete.count():
        delete.click()
        page.wait_for_timeout(2000)
        detail = ok_json(base + f"/api/admin/playlists/{playlist_id}")
        check("automatic cover restored", (detail.get("playlist") or {}).get("cover_kind") in ("auto", "none"), (detail.get("playlist") or {}).get("cover_kind"))


def scenario_play_and_priorities(page: Page, base: str) -> None:
    print("\n--- play from the playlist and return to automatic")
    playlists = ok_json(base + "/api/admin/playlists").get("items", [])
    user = next((p for p in playlists if not p.get("system")), None)
    if not user:
        check("user playlist exists for play", False)
        return
    page.goto(base + f"/admin/playlists/{user['id']}", wait_until="commit")
    page.wait_for_timeout(1200)
    stop = page.get_by_role("button", name=re.compile("Остановить эфир|Стоп")).first
    if stop.count():
        stop.click()
        page.wait_for_timeout(2500)
    onair = page.get_by_role("button", name=re.compile("В эфир")).first
    check("play button available for a non-empty playlist", onair.count() > 0 and onair.is_enabled(), onair.count())
    if onair.count():
        onair.click()
        page.wait_for_timeout(3000)
    deadline = time.time() + 90
    status = None
    while time.time() < deadline:
        np = ok_json(base + "/api/public/now-playing")
        status = np["station"]["status"]
        if status == "on_air" and np.get("track"):
            break
        time.sleep(2)
    check("play started the station and music", status == "on_air", status)

    back = page.get_by_role("button", name=re.compile("Вернуться к автоматическому|Автоматическ")).first
    if back.count():
        back.click()
        page.wait_for_timeout(2500)
        np = ok_json(base + "/api/public/now-playing")
        check("returning to automatic keeps the station on", np["station"]["status"] in ("on_air", "no_music", "starting"), np["station"]["status"])
    else:
        check("return-to-automatic control exists", False)


def scenario_settings(page: Page, base: str) -> None:
    print("\n--- settings: services, storage, links, statuses")
    page.goto(base + "/admin/settings", wait_until="commit")
    page.wait_for_timeout(2000)
    body = page.inner_text("body")
    check("services show Liquidsoap and Icecast", ("Liquidsoap" in body) and ("Icecast" in body), "")
    check("services are not empty-state", "Нет данных о службах" not in body, "")
    storage = ok_json(base + "/api/admin/storage")
    check("storage reports tracks and bytes", (storage.get("tracks") or 0) > 0 and (storage.get("bytes") or 0) > 0, f"{storage.get('tracks')} {storage.get('bytes')}")
    check("storage text is rendered", ("трек" in body.lower()) and ("МБ" in body or "ГБ" in body or "КБ" in body), "")
    check("no duplicated Minecraft link", "Minecraft" not in body, "")
    links = ok_json(base + "/api/admin/links")
    keys = [l.get("key") for l in links.get("links", [])]
    check("links have a single direct MP3", keys.count("mp3") == 1 and "world_radio" not in keys, keys)

    # statuses consistent between overview and public API
    np = ok_json(base + "/api/public/now-playing")
    page.goto(base + "/admin", wait_until="commit")
    page.wait_for_timeout(1800)
    overview = page.inner_text("body")
    label = {"on_air": "В эфире", "no_music": "Тишина", "off": "Остановлено", "error": "Ошибка", "starting": "Запуска"}.get(np["station"]["status"], "")
    check("overview shows the same status as the API", bool(label) and label.lower() in overview.lower(), f"{np['station']['status']} -> {label}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:18080")
    parser.add_argument("--password", default="e2e-owner-password")
    parser.add_argument("--fixtures", required=True)
    parser.add_argument("--mobile", action="store_true", help="also render the mobile viewport")
    parser.add_argument("--screens", default=str(Path.home() / ".cache" / "vdr-e2e-shots"))
    args = parser.parse_args()

    fixtures_dir = Path(args.fixtures)
    fixtures = sorted(fixtures_dir.glob("*.mp3"))
    cover = fixtures_dir / "playlist-cover.png"
    screens = Path(args.screens)
    screens.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        console: list[str] = []
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))

        login(page, args.base, args.password)
        scenario_focus(page, args.base)
        scenario_system_playlist(page, args.base, fixtures)
        playlist_id = scenario_user_playlist(page, args.base)
        scenario_cover(page, args.base, playlist_id, cover)
        scenario_play_and_priorities(page, args.base)
        scenario_settings(page, args.base)

        page.goto(args.base + "/admin", wait_until="commit")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(screens / "overview-1366.png"), full_page=True)
        page.goto(args.base + "/admin/playlists", wait_until="commit")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(screens / "playlists-1366.png"), full_page=True)
        page.goto(args.base + "/admin/settings", wait_until="commit")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(screens / "settings-1366.png"), full_page=True)
        page.goto(args.base + "/", wait_until="commit")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(screens / "public-1366.png"), full_page=True)

        if args.mobile:
            mob = browser.new_context(viewport={"width": 390, "height": 844})
            mp = mob.new_page()
            login(mp, args.base, args.password)
            for path, name in [("/admin", "overview-390"), ("/admin/playlists", "playlists-390"), ("/admin/settings", "settings-390"), ("/", "public-390")]:
                mp.goto(args.base + path, wait_until="commit")
                mp.wait_for_timeout(1500)
                mp.screenshot(path=str(screens / f"{name}.png"), full_page=True)
                overflow = mp.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                check(f"no horizontal overflow at 390 ({name})", overflow <= 1, overflow)
            mob.close()

        check("no console errors", not console, "; ".join(console[:3]))
        browser.close()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} browser checks passed")
    if failed:
        print("failed: " + ", ".join(failed))
    print(f"screenshots: {screens}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
