#!/usr/bin/env python3
"""Real-browser end-to-end scenarios for Vanilla Dream Radio 1.0.1.

Drives an isolated station (front door on 127.0.0.1:18080 by default) with a real
backend, real Liquidsoap and real Icecast. UI actions happen in the browser;
assertions read the same session through Playwright's API context, so nothing is
mocked and the state is the one the UI itself produced.

    python tests/e2e/playwright_flow.py --base http://127.0.0.1:18080 \
        --password e2e-owner-password --fixtures <dir> [--mobile]
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path

from playwright.sync_api import APIRequestContext, Page, sync_playwright

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, str(detail)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + str(detail)) if detail else ''}")
    return ok


def api_json(ctx: APIRequestContext, path: str, method: str = "GET", data=None) -> dict:
    resp = ctx.fetch(path, method=method, data=data) if data is not None else ctx.fetch(path, method=method)
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"_status": resp.status, "_text": resp.text()[:200]}


def active(page: Page) -> dict:
    return page.evaluate(
        """() => { const a = document.activeElement; if (!a) return {tag: null};
        return {tag: a.tagName, type: a.getAttribute('type'), placeholder: a.getAttribute('placeholder'),
                aria: a.getAttribute('aria-label'), cls: (a.className || '').toString().slice(0, 40),
                text: (a.innerText || '').trim().slice(0, 40),
                value: 'value' in a ? String(a.value).slice(0, 40) : null}; }"""
    )


def wait_panel(page: Page, marker: str = "Обзор", timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.evaluate(f"document.body.innerText.includes({marker!r})"):
                return True
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(300)
    return False


def login(page: Page, base: str, password: str) -> None:
    page.goto(base + "/admin/login", wait_until="commit")
    page.wait_for_selector("input[type=password]", timeout=30000)
    page.fill("input[type=password]", password)
    page.get_by_role("button", name=re.compile("Войти")).first.click()
    if not wait_panel(page, "Обзор", 30):
        page.wait_for_timeout(1500)
        page.get_by_role("button", name=re.compile("Войти")).first.click()
        wait_panel(page, "Обзор", 20)


def goto_panel(page: Page, base: str, path: str, marker: str, wait: float = 2.0) -> None:
    page.goto(base + path, wait_until="commit")
    wait_panel(page, marker, 25)
    page.wait_for_timeout(int(wait * 1000))


def wait_text(page: Page, text: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.evaluate("(t) => !!document.body && document.body.innerText.includes(t)", text):
                return True
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(400)
    return False


def api_stop(page: Page, base: str) -> None:
    """Stop the station through the UI if it is running (keeps the flow repeatable)."""
    if page.get_by_role("button", name=re.compile("Остановить эфир")).count() == 0:
        return
    page.get_by_role("button", name=re.compile("Остановить эфир")).first.click()
    deadline = time.time() + 30
    while time.time() < deadline:
        page.wait_for_timeout(800)
        try:
            status = page.evaluate("async () => (await (await fetch('/api/public/now-playing')).json()).station.status")
        except Exception:  # noqa: BLE001
            continue
        if status == "off":
            return


def open_modal(page: Page, button_name: str, timeout: int = 15000) -> bool:
    try:
        page.get_by_role("button", name=re.compile(button_name)).first.click(timeout=timeout)
        page.wait_for_selector(".modal__panel", timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False


def type_chars(page: Page, text: str) -> list[dict]:
    walked = []
    for _ in text:
        page.keyboard.type(_)
        page.wait_for_timeout(20)
        walked.append(active(page))
    return walked


def scenario_focus(page: Page, base: str) -> None:
    print("\n--- focus regression (P0-INPUT-FOCUS)")
    goto_panel(page, base, "/admin/playlists", "Плейлисты")
    trigger = page.get_by_role("button", name=re.compile("Создать плейлист")).first
    trigger.click()
    page.wait_for_selector(".modal__panel", timeout=15000)
    opener_before = active(page)
    check("create dialog opens", True)
    panel = page.locator(".modal__panel").last
    field = panel.locator("input").first
    field.click()
    typed = "Проверка фокуса Кириллица"
    walked = type_chars(page, typed)
    check(
        "focus stays on the input for every keystroke",
        len(walked) == len(typed) and all(item.get("tag") == "INPUT" for item in walked),
        f"{len(walked)}/{len(typed)} keys",
    )
    check("typed value matches", field.input_value() == typed, field.input_value())

    field.press("Home")
    for _ in range(3):
        page.keyboard.press("ArrowRight")
    page.keyboard.type("XY")
    check("middle edit keeps focus and text", active(page).get("tag") == "INPUT" and "ПроXY" in field.input_value(),
          field.input_value()[:12])

    page.keyboard.press("Control+A")
    page.keyboard.type("Новое имя")
    page.keyboard.press("Backspace")
    check("select-all replace + backspace", field.input_value() == "Новое им", field.input_value())

    page.keyboard.press("Tab")
    info = active(page)
    check("tab moves to the description", info.get("tag") == "TEXTAREA", info.get("tag"))
    if info.get("tag") == "TEXTAREA":
        type_chars(page, "Строка один")
        page.keyboard.press("Enter")
        type_chars(page, "Строка два")
        check("textarea keeps focus across a newline", active(page).get("tag") == "TEXTAREA")

    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    check("escape closes the dialog", page.locator(".modal__panel").count() == 0)
    restored = active(page)
    check("escape returns focus to the trigger",
          restored.get("tag") == "BUTTON" and "Создать плейлист" in (restored.get("text") or restored.get("aria") or ""),
          f"{restored.get('tag')} {restored.get('text')}")

    check("re-opened dialog focuses a field", open_modal(page, "Создать плейлист"))
    page.wait_for_timeout(500)
    first = active(page)
    check("initial focus is not the close button", first.get("tag") in ("INPUT", "TEXTAREA"), first.get("tag"))
    page.keyboard.press("Escape")


def scenario_system_playlist(page: Page, base: str, ctx: APIRequestContext, fixtures: list[Path]) -> None:
    print("\n--- system playlist and automatic air")
    goto_panel(page, base, "/admin", "Радиостанция")
    body = page.inner_text("body")
    check("overview shows an honest stopped/silent state",
          ("Эфир остановлен" in body) or ("тишин" in body.lower()) or ("медиатек" in body.lower()),
          body[:80].replace("\n", " "))

    start = page.get_by_role("button", name=re.compile("Запустить эфир")).first
    if start.count():
        start.click()
    else:
        stop = page.get_by_role("button", name=re.compile("Остановить эфир")).first
        if stop.count():
            stop.click()
            api_stop(page, base)
            page.wait_for_timeout(2500)
            page.get_by_role("button", name=re.compile("Запустить эфир")).first.click()
    page.wait_for_timeout(3500)
    now = api_json(ctx, "/api/public/now-playing")
    check("station starts with an empty library", now["station"]["desired"] == "playing", now["station"]["status"])
    check("empty library is explained honestly",
          now["station"].get("reason") in ("library_empty", "engine_starting", "playing", "mount_missing"),
          now["station"].get("reason"))

    goto_panel(page, base, "/admin/library", "Медиатека")
    opened = False
    for name in ("Загрузить музыку", "Загрузить"):
        btn = page.get_by_role("button", name=re.compile(name)).first
        if btn.count():
            btn.click()
            page.wait_for_timeout(800)
            opened = page.locator("input[type=file]").count() > 0
            if opened:
                break
    check("upload panel opens", opened)
    page.set_input_files("input[type=file]", [str(f) for f in fixtures[:2]])
    page.wait_for_timeout(9000)
    page.keyboard.press("Escape")

    tracks = api_json(ctx, "/api/admin/tracks?limit=50")
    check("upload through the UI adds tracks", (tracks.get("total") or 0) >= 2, tracks.get("total"))
    system = next((p for p in api_json(ctx, "/api/admin/playlists").get("items", []) if p.get("system")), {})
    check("system playlist contains the uploads", (system.get("track_count") or 0) >= 2, system.get("track_count"))
    check("system playlist defaults persist", system.get("shuffle") is True and system.get("repeat_mode") == "all",
          f"{system.get('shuffle')} {system.get('repeat_mode')}")

    deadline = time.time() + 120
    np: dict = {}
    while time.time() < deadline:
        np = api_json(ctx, "/api/public/now-playing")
        if np["station"]["status"] == "on_air" and (np.get("track") or {}).get("id"):
            break
        time.sleep(2)
    track = np.get("track") or {}
    check("music starts automatically after upload", np["station"]["status"] == "on_air" and bool(track.get("id")),
          f"{np['station']['status']} {track.get('title')}")
    check("source is the system playlist", (np.get("source") or {}).get("kind") in ("library", "system"),
          np.get("source"))
    check("next track is queued", bool((np.get("next_track") or {}).get("id")),
          (np.get("next_track") or {}).get("title"))

    goto_panel(page, base, "/admin", "Радиостанция")
    body = page.inner_text("body")
    title = track.get("title") or ""
    check("overview shows the playing track",
          bool(title) and (title[:14] in body or title.split(" - ")[-1][:14] in body), title)
    check("overview names the source «Вся медиатека»", "Вся медиатека" in body, "")


def scenario_user_playlist(page: Page, base: str, ctx: APIRequestContext) -> dict:
    print("\n--- user playlist: tracks, bulk add, persistence")
    goto_panel(page, base, "/admin/playlists", "Плейлисты")
    csrf = api_json(ctx, "/api/auth/me").get("csrf_token") or ""
    for stale in api_json(ctx, "/api/admin/playlists").get("items", []):
        if stale["name"] == "Проверка состава":
            ctx.fetch(f"/api/admin/playlists/{stale['id']}", method="DELETE", headers={"X-CSRF-Token": csrf})
    page.reload(wait_until="commit")
    wait_panel(page, "Плейлисты", 25)
    page.wait_for_timeout(1200)
    if not open_modal(page, "Создать плейлист"):
        check("playlist dialog opens", False)
        return {}
    panel = page.locator(".modal__panel").last
    panel.locator("input").first.fill("Проверка состава")
    page.get_by_role("button", name=re.compile("^Создать|Сохранить")).last.click()
    page.wait_for_timeout(2500)
    check("create dialog closed", page.locator(".modal__panel").count() == 0)

    playlists = api_json(ctx, "/api/admin/playlists").get("items", [])
    user = next((p for p in playlists if not p.get("system") and p["name"] == "Проверка состава"), None)
    check("playlist created via UI", bool(user), [p["name"] for p in playlists])
    if not user:
        return {}

    goto_panel(page, base, f"/admin/playlists/{user['id']}", "Проверка состава")
    add_trigger = page.get_by_role("button", name=re.compile("Добавить музыку|Добавить треки")).first
    add_trigger.click()
    page.wait_for_selector(".modal__panel", timeout=15000)
    page.wait_for_timeout(1200)
    add_opened = active(page)
    checkbox = page.get_by_role("checkbox", name=re.compile("Выбрать все", re.I)).first
    check("select-all control exists", checkbox.count() > 0)
    if checkbox.count():
        checkbox.check()
        page.wait_for_timeout(500)
    confirm = page.get_by_role("button", name=re.compile(r"Добавить \(\d+\)")).first
    check("confirm button shows the count", confirm.count() > 0, confirm.inner_text() if confirm.count() else "")
    if confirm.count():
        confirm.click()
        page.wait_for_timeout(3000)

    detail = api_json(ctx, f"/api/admin/playlists/{user['id']}").get("playlist") or {}
    tracks = detail.get("tracks") or []
    check("playlist detail has tracks", len(tracks) >= 2, len(tracks))
    check("count and duration are consistent",
          (detail.get("track_count") or 0) == len(tracks) and (detail.get("duration") or 0) > 0,
          f"{detail.get('track_count')} {detail.get('duration')}")

    page.reload(wait_until="commit")
    wait_panel(page, "Проверка состава", 25)
    page.wait_for_timeout(2000)
    body = page.inner_text("body")
    rendered = sum(1 for t in tracks[:2] if (t.get("title") or "")[:12] and (t["title"][:12] in body))
    check("tracks render after reload", rendered >= 2, f"{rendered}/{min(2, len(tracks))}")
    check("playlist counter rendered", f"{len(tracks)} трек" in body, body[:120].replace("\n", " "))

    if open_modal(page, "Добавить музыку|Добавить треки"):
        page.wait_for_timeout(1200)
        text = page.inner_text(".modal__panel")
        check("already added tracks are marked", "Уже в плейлисте" in text, "")
        confirm2 = page.get_by_role("button", name=re.compile(r"Добавить \(\d+\)")).first
        check("nothing new selected for an already filled playlist",
              confirm2.count() == 0 or confirm2.is_disabled(), "")
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        restored = active(page)
        check("add-tracks escape returns focus to the trigger",
              restored.get("tag") == "BUTTON" and "Добавить" in (restored.get("text") or restored.get("aria") or ""),
              f"{restored.get('tag')} {restored.get('text')}")

    return detail


def scenario_cover(page: Page, base: str, ctx: APIRequestContext, playlist: dict, cover: Path) -> None:
    print("\n--- playlist cover")
    if not playlist or not cover.exists():
        check("cover scenario prerequisites", False, f"{bool(playlist)} {cover}")
        return
    pid = playlist["id"]
    goto_panel(page, base, f"/admin/playlists/{pid}", "Изменить")
    if not open_modal(page, "Изменить"):
        check("edit dialog opens", False)
        return
    page.wait_for_timeout(700)
    file_input = page.locator(".modal__panel input[type=file]").first
    if file_input.count():
        file_input.set_input_files(str(cover))
        page.wait_for_timeout(1500)
        for name in ("Загрузить обложку", "Сохранить", "Применить"):
            btn = page.get_by_role("button", name=re.compile(name)).last
            if btn.count():
                btn.click()
                break
        page.wait_for_timeout(3000)
    kind = (api_json(ctx, f"/api/admin/playlists/{pid}").get("playlist") or {}).get("cover_kind")
    check("custom cover is stored", kind == "custom", kind)

    page.keyboard.press("Escape")
    page.wait_for_timeout(600)
    if open_modal(page, "Изменить"):
        reset = page.get_by_role("button", name=re.compile("Вернуть автоматическую", re.I)).first
        if reset.count():
            reset.click()
            page.wait_for_timeout(2500)
        page.keyboard.press("Escape")
    kind2 = (api_json(ctx, f"/api/admin/playlists/{pid}").get("playlist") or {}).get("cover_kind")
    check("automatic cover restored", kind2 in ("auto", "none"), kind2)


def scenario_play_and_priorities(page: Page, base: str, ctx: APIRequestContext, playlist: dict) -> None:
    print("\n--- play from the playlist and return to automatic")
    if not playlist:
        check("playlist available for play", False)
        return
    goto_panel(page, base, f"/admin/playlists/{playlist['id']}", "В эфир")
    play = page.get_by_role("button", name=re.compile("^В эфир$")).first
    check("play button available", play.count() > 0 and play.is_enabled(), play.count())
    if play.count():
        play.click()
        page.wait_for_timeout(3000)
    deadline = time.time() + 90
    np: dict = {}
    while time.time() < deadline:
        np = api_json(ctx, "/api/public/now-playing")
        if np["station"]["status"] == "on_air" and (np.get("track") or {}).get("id"):
            break
        time.sleep(2)
    check("play starts music", np["station"]["status"] == "on_air", np["station"]["status"])
    page.reload(wait_until="commit")
    wait_panel(page, "В эфир", 25)
    page.wait_for_timeout(1500)
    check("manual override is visible in the UI", wait_text(page, "Ручной выбор", 15), "")

    back = page.get_by_role("button", name=re.compile("Вернуться к автоматическому|Автоматическ")).first
    check("return-to-automatic control exists", back.count() > 0, back.count())
    if back.count():
        back.click()
        page.wait_for_timeout(3000)
        np2 = api_json(ctx, "/api/public/now-playing")
        check("returning to automatic keeps the station running",
              np2["station"]["status"] in ("on_air", "no_music", "starting", "reconnecting"), np2["station"]["status"])


def scenario_settings(page: Page, base: str, ctx: APIRequestContext) -> None:
    print("\n--- settings, statuses and links")
    goto_panel(page, base, "/admin/settings", "Настройки")
    body = page.inner_text("body")
    check("services show Liquidsoap and Icecast", ("Liquidsoap" in body) and ("Icecast" in body), "")
    check("no empty services state", "Нет данных о службах" not in body, "")
    storage = api_json(ctx, "/api/admin/storage")
    check("storage reports tracks and bytes", (storage.get("tracks") or 0) > 0 and (storage.get("bytes") or 0) > 0,
          f"{storage.get('tracks')} {storage.get('bytes')}")
    check("storage rendered as numbers", "трек" in body.lower(), "")
    check("default source option mentions the system playlist", "Вся медиатека (автоматически)" in body, "")
    check("links do not mention Minecraft", "Minecraft" not in body, "")
    links = api_json(ctx, "/api/admin/links").get("links", [])
    keys = [item.get("key") for item in links]
    check("one direct MP3 link", keys.count("mp3") == 1 and "world_radio" not in keys, keys)

    np = api_json(ctx, "/api/public/now-playing")
    page.goto(base + "/admin", wait_until="commit")
    wait_panel(page, "Радиостанция", 25)
    page.wait_for_timeout(1500)
    mapping = {"on_air": "В эфире", "no_music": "Тишин", "off": "Остановлен", "error": "Ошибк", "starting": "Запуска"}
    word = mapping.get(np["station"]["status"], "")
    check("overview status matches the API",
          bool(word) and wait_text(page, word, 12),
          f"{np['station']['status']} -> {word}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:18080")
    parser.add_argument("--password", default="e2e-owner-password")
    parser.add_argument("--fixtures", required=True)
    parser.add_argument("--mobile", action="store_true")
    parser.add_argument("--screens", default=str(Path.home() / ".cache" / "vdr-e2e-shots"))
    args = parser.parse_args()

    fixtures_dir = Path(args.fixtures)
    fixtures = sorted(fixtures_dir.glob("*.mp3"))
    cover = fixtures_dir / "playlist-cover.png"
    screens = Path(args.screens)
    screens.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1366, "height": 900}, base_url=args.base)
        page = ctx.new_page()
        console: list[str] = []
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))

        login(page, args.base, args.password)
        check("admin login through the UI", wait_panel(page, "Обзор", 10), page.url)

        scenario_focus(page, args.base)
        scenario_system_playlist(page, args.base, ctx.request, fixtures)
        playlist = scenario_user_playlist(page, args.base, ctx.request)
        scenario_cover(page, args.base, ctx.request, playlist, cover)
        scenario_play_and_priorities(page, args.base, ctx.request, playlist)
        scenario_settings(page, args.base, ctx.request)

        for path, name, marker in [
            ("/admin", "overview-1366", "Радиостанция"),
            ("/admin/playlists", "playlists-1366", "Плейлисты"),
            ("/admin/settings", "settings-1366", "Настройки"),
            ("/", "public-1366", "Сейчас в эфире"),
        ]:
            page.goto(args.base + path, wait_until="commit")
            if path != "/":
                wait_panel(page, marker, 20)
            page.wait_for_timeout(1800)
            page.screenshot(path=str(screens / f"{name}.png"), full_page=True)

        if args.mobile:
            mob = browser.new_context(viewport={"width": 390, "height": 844}, base_url=args.base)
            mp = mob.new_page()
            login(mp, args.base, args.password)
            for path, name in [("/admin", "overview-390"), ("/admin/playlists", "playlists-390"),
                               ("/admin/settings", "settings-390"), ("/", "public-390")]:
                mp.goto(args.base + path, wait_until="commit")
                if path != "/":
                    wait_panel(mp, "Обзор", 20)
                mp.wait_for_timeout(1800)
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
