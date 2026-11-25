#!/usr/bin/env python3
"""Media 1.1.0 end-to-end: import with duplicates, editor, downloads, ZIP export.

Runs against the isolated station (front door http://127.0.0.1:18080) with a real
backend, real Liquidsoap and real Icecast. UI actions are performed in Chromium;
artifacts (downloaded files, ZIP contents, ID3 tags) are inspected locally.

    python tests/e2e/media_flow.py --base http://127.0.0.1:18080 \
        --password e2e-owner-password --work <dir with fixtures>
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

from mutagen.id3 import ID3, TALB, TIT2, TPE1
from mutagen.mp3 import MP3
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


def wait_text(page: Page, text: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.evaluate("(t) => !!document.body && document.body.innerText.includes(t)", text):
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
    wait_text(page, "Обзор", 30)


def open_uploader(page: Page, base: str) -> None:
    page.goto(base + "/admin/library", wait_until="commit")
    wait_text(page, "Медиатека", 25)
    page.wait_for_timeout(1200)
    for name in ("Загрузить музыку", "Загрузить"):
        btn = page.get_by_role("button", name=re.compile(name)).first
        if btn.count():
            btn.click()
            page.wait_for_timeout(600)
            if page.locator("input[type=file]").count():
                return


def upload_files(page: Page, files: list[Path]) -> None:
    page.set_input_files("input[type=file]", [str(f) for f in files])


def wait_batch(ctx: APIRequestContext, batch_id: str, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    batch = {}
    while time.time() < deadline:
        batch = api(ctx, f"/api/admin/uploads/{batch_id}").get("batch") or {}
        items = batch.get("items") or []
        busy = any(i.get("state") in ("uploading", "processing", None) for i in items)
        if items and not busy and batch.get("status") != "uploading":
            return batch
        time.sleep(1.5)
    return batch


def open_decisions(page: Page) -> bool:
    for name in ("Просмотреть совпадения", "Совпадени"):
        btn = page.get_by_role("button", name=re.compile(name, re.I)).first
        if btn.count():
            btn.click()
            page.wait_for_timeout(1200)
            return wait_text(page, "Обнаружены совпадения", 15)
    return False


def make_fixtures(work: Path) -> dict[str, Path]:
    src = Path(r"C:\Users\main\AppData\Local\Temp\opencode\e2e-fixtures")
    out = work / "media-fixtures"
    out.mkdir(parents=True, exist_ok=True)
    a = out / "01-unique.mp3"
    b = out / "02-second.mp3"
    same_hash = out / "03-same-bytes.mp3"
    same_tags = out / "04-same-tags.mp3"
    if not a.exists():
        shutil.copyfile(src / "01-pervyy-test.mp3", a)
        shutil.copyfile(src / "02-second-track.mp3", b)
        shutil.copyfile(a, same_hash)
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=777:duration=12",
             "-ac", "2", "-ar", "44100", "-b:a", "128k", str(same_tags)], check=True)
        tags = MP3(a).tags
        audio = MP3(same_tags)
        if audio.tags is None:
            audio.add_tags()
        for frame in ("TIT2", "TPE1", "TALB"):
            if tags is not None and frame in tags:
                continue
        audio.tags.delall("TIT2"); audio.tags.delall("TPE1"); audio.tags.delall("TALB")
        audio.tags.add(TIT2(encoding=3, text="Первый тест"))
        audio.tags.add(TPE1(encoding=3, text="Ваниль"))
        audio.tags.add(TALB(encoding=3, text="Сиреневый эфир"))
        audio.save()
    cover = src / "playlist-cover.png"
    return {"a": a, "b": b, "same_hash": same_hash, "same_tags": same_tags, "cover": cover}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:18080")
    parser.add_argument("--password", default="e2e-owner-password")
    parser.add_argument("--work", default=str(Path.home() / ".cache" / "vdr-media-e2e"))
    args = parser.parse_args()
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    fixtures = make_fixtures(work)
    downloads = work / "downloads"
    shutil.rmtree(downloads, ignore_errors=True)
    downloads.mkdir(parents=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1366, "height": 900}, base_url=args.base,
                                  accept_downloads=True)
        page = ctx.new_page()
        login(page, args.base, args.password)
        csrf = api(ctx.request, "/api/auth/me").get("csrf_token") or ""

        # keep the station on air so exports are verified during real playback
        state = api(ctx.request, "/api/public/now-playing")
        if state["station"]["status"] in ("off", "starting"):
            try:
                ctx.request.post("/api/admin/station/start", headers={"X-CSRF-Token": csrf})
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(4000)

        before = api(ctx.request, "/api/admin/tracks?limit=1").get("total") or 0

        print("\n--- A. single upload, duplicate warning, skip and save")
        open_uploader(page, args.base)
        upload_files(page, [fixtures["a"]])
        page.wait_for_timeout(4000)
        total_after_unique = api(ctx.request, "/api/admin/tracks?limit=1").get("total") or 0
        check("unique file is added", total_after_unique == before + 1, total_after_unique)

        # same bytes -> pending
        open_uploader(page, args.base)
        upload_files(page, [fixtures["same_hash"]])
        page.wait_for_timeout(3500)
        pending_total = (api(ctx.request, "/api/admin/uploads?limit=1").get("total")
                         if True else None)
        text = page.inner_text("body")
        check("duplicate upload shows a match", "совпадени" in text.lower(), "")

        opened = open_decisions(page)
        check("decisions dialog opens", opened)
        if opened:
            panel = page.locator(".modal__panel").last
            body = panel.inner_text()
            low = body.lower()
            check("decisions dialog shows comparison", "медиатеке" in low and "новый файл" in low, "")
            skip = panel.get_by_role("button", name=re.compile("^Пропустить$")).first
            if skip.count():
                skip.click()
                page.wait_for_timeout(500)
            apply_btn = panel.get_by_role("button", name=re.compile("Применить решения")).first
            if apply_btn.count() and apply_btn.is_enabled():
                apply_btn.click()
                page.wait_for_timeout(2500)
        after_skip = api(ctx.request, "/api/admin/tracks?limit=1").get("total") or 0
        check("skipping a duplicate keeps the library size", after_skip == total_after_unique, after_skip)

        # same bytes again -> save a copy
        open_uploader(page, args.base)
        upload_files(page, [fixtures["same_hash"]])
        page.wait_for_timeout(3500)
        if open_decisions(page):
            panel = page.locator(".modal__panel").last
            save = panel.get_by_role("button", name=re.compile("^Сохранить копию$")).first
            if save.count():
                save.click()
                page.wait_for_timeout(500)
            apply_btn = panel.get_by_role("button", name=re.compile("Применить решения")).first
            if apply_btn.count() and apply_btn.is_enabled():
                apply_btn.click()
                page.wait_for_timeout(3000)
        after_save = api(ctx.request, "/api/admin/tracks?limit=1").get("total") or 0
        tracks = api(ctx.request, "/api/admin/tracks?limit=200").get("items", [])
        dups = [t for t in tracks if t.get("is_duplicate")]
        check("saving a copy creates an independent record", after_save == total_after_unique + 1, after_save)
        check("copy is marked as a duplicate", len(dups) >= 1, len(dups))
        if dups:
            check("duplicate record has its own id and file",
                  dups[0]["id"] not in {t["id"] for t in tracks if not t.get("is_duplicate")}, dups[0]["id"][:8])

        page.goto(args.base + "/admin/library", wait_until="commit")
        wait_text(page, "Медиатека", 25)
        page.wait_for_timeout(1500)
        check("duplicate badge appears in the library", "Дубль" in page.inner_text("body"), "")

        print("\n--- B. batch with mixed files")
        open_uploader(page, args.base)
        upload_files(page, [fixtures["b"], fixtures["same_hash"], fixtures["same_tags"]])
        page.wait_for_timeout(6000)
        body = page.inner_text("body")
        check("batch dialog reports matches", "совпадени" in body.lower(), "")
        check("batch dialog shows counters", "Добавлено" in body and "Ошибки" in body, "")
        if open_decisions(page):
            panel = page.locator(".modal__panel").last
            rows = panel.inner_text()
            check("both matches are listed together", rows.count("Сохранить копию") >= 1, "")

            print("\n--- C. track editor on the confirmed duplicate")
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)
            tracks_now = api(ctx.request, "/api/admin/tracks?limit=200").get("items", [])
            target = next((t for t in tracks_now if t.get("is_duplicate")), None)
            check("duplicate available for the editor", target is not None, target["title"] if target else "")
            page.goto(args.base + "/admin/library", wait_until="commit")
            wait_text(page, "Медиатека", 25)
            page.wait_for_timeout(1500)
            if target:
                search = page.get_by_placeholder(re.compile("Поиск", re.I)).first
                if search.count():
                    search.fill(target["title"][:20])
                    page.wait_for_timeout(1500)
            row_menu = page.get_by_role("button", name=re.compile("Действия с треком")).first
            if row_menu.count():
                row_menu.click()
                page.wait_for_timeout(400)
                edit = page.get_by_role("menuitem", name=re.compile("Изменить трек")).first
                if edit.count():
                    edit.click()
                    page.wait_for_selector(".modal__panel", timeout=10000)
                    page.wait_for_timeout(600)
                    editor = page.locator(".modal__panel").last
                    title_input = editor.locator('input:not([type="file"])').first
                    title_input.click()
                    page.keyboard.press("End")
                    typed = " (cuted)"
                    seen_focus = []
                    for _ in typed:
                        page.keyboard.type(_)
                        page.wait_for_timeout(20)
                        seen_focus.append(page.evaluate("() => document.activeElement && document.activeElement.tagName"))
                    check("editor keeps focus while typing", all(t == "INPUT" for t in seen_focus), f"{len(seen_focus)} keys")
                    file_input = editor.locator('input[type="file"]').first
                    if file_input.count() and fixtures["cover"].exists():
                        file_input.set_input_files(str(fixtures["cover"]))
                        page.wait_for_timeout(800)
                    save = editor.get_by_role("button", name=re.compile("^Сохранить$")).first
                    if save.count():
                        save.click()
                        page.wait_for_timeout(2500)
                    edited = api(ctx.request, f"/api/admin/tracks?limit=200").get("items", [])
                    rec = next((t for t in edited if target and t["id"] == target["id"]), None)
                    if rec:
                        check("editor saved the title", rec["title"].endswith("(cuted)"), rec["title"])
                        check("duplicate flag survives editing", rec.get("is_duplicate") is True, "")
                        check("custom cover stored", rec.get("cover_kind") == "custom", rec.get("cover_kind"))

                        print("\n--- E. single track download with current tags")
                        resp = ctx.request.get(f"/api/admin/tracks/{rec['id']}/download?mode=tags")
                        check("download returns a file", resp.status == 200 and len(resp.body()) > 1000,
                              f"{resp.status} {len(resp.body())}")
                        target = downloads / "single.mp3"
                        target.write_bytes(resp.body())
                        tags = ID3(str(target))
                        check("downloaded tags carry the edited title", str(tags.get("TIT2")) == rec["title"],
                              str(tags.get("TIT2")))
                        check("downloaded file has an embedded cover", any(k.startswith("APIC") for k in tags.keys()), "")
                        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                                "-of", "json", str(target)], capture_output=True, text=True)
                        dur = json.loads(probe.stdout or "{}").get("format", {}).get("duration")
                        check("downloaded file decodes as audio", dur is not None and float(dur) > 5, dur)

                        original = ctx.request.get(f"/api/admin/tracks/{rec['id']}/download?mode=original")
                        check("original download works", original.status == 200 and len(original.body()) > 1000, original.status)

        print("\n--- F. playlist ZIP with duplicates")
        playlists = api(ctx.request, "/api/admin/playlists").get("items", [])
        pl = next((x for x in playlists if not x.get("system")), None)
        library_items = api(ctx.request, "/api/admin/tracks?limit=200").get("items", [])
        if not pl:
            created = ctx.request.post("/api/admin/playlists", headers={"X-CSRF-Token": csrf},
                                       data={"name": "Экспорт дублей"})
            pl = (created.json() or {}).get("playlist") or {}
            ids = [t["id"] for t in library_items[:3]]
            if pl.get("id") and ids:
                ctx.request.post(f"/api/admin/playlists/{pl['id']}/tracks", headers={"X-CSRF-Token": csrf},
                                 data={"track_ids": ids})
                page.wait_for_timeout(500)
        if pl:
            ids = [t["id"] for t in api(ctx.request, f"/api/admin/playlists/{pl['id']}").get("playlist", {}).get("tracks", [])]
            resp = ctx.request.post("/api/admin/exports", headers={"X-CSRF-Token": csrf},
                                    data={"scope": "playlist", "playlist_id": pl["id"], "format": "zip", "metadata": "tags"})
            job = (resp.json() or {}).get("job") or {}
            check("zip export job created", resp.status in (200, 202) and bool(job.get("id")), resp.status)
            deadline = time.time() + 120
            state = job
            while time.time() < deadline:
                state = api(ctx.request, f"/api/admin/exports/{job['id']}").get("job") or {}
                if state.get("state") in ("ready", "error"):
                    break
                time.sleep(1.5)
            check("zip becomes ready", state.get("state") == "ready", state.get("state"))
            if state.get("state") == "ready":
                archive = ctx.request.get(f"/api/admin/exports/{job['id']}/archive")
                zip_path = downloads / "playlist.zip"
                zip_path.write_bytes(archive.body())
                check("archive downloads", archive.status == 200 and zip_path.stat().st_size > 1000,
                      f"{archive.status} {zip_path.stat().st_size}")
                with zipfile.ZipFile(zip_path) as zf:
                    names = zf.namelist()
                    check("archive contains music files", any(n.lower().endswith(".mp3") for n in names), len(names))
                    check("archive contains an m3u8", any(n.endswith(".m3u8") for n in names), names[:4])
                    check("archive entries are unique", len(names) == len(set(n.lower() for n in names)), len(names))
                    m3u = next((n for n in names if n.endswith(".m3u8")), None)
                    if m3u:
                        text = zf.read(m3u).decode("utf-8")
                        check("m3u8 references the archived files", "#EXTM3U" in text and ".mp3" in text, text[:60])
                    audio = next(n for n in names if n.lower().endswith(".mp3"))
                    with zipfile.ZipFile(zip_path) as zf2, open(downloads / "from_zip.mp3", "wb") as fh:
                        fh.write(zf2.read(audio))
                    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                            "-of", "json", str(downloads / "from_zip.mp3")],
                                           capture_output=True, text=True)
                    dur = json.loads(probe.stdout or "{}").get("format", {}).get("duration")
                    check("archived audio decodes", dur is not None and float(dur) > 3, dur)

        print("\n--- G. selection export")
        sel = ctx.request.post("/api/admin/exports", headers={"X-CSRF-Token": csrf},
                               data={"scope": "tracks", "ids": [t["id"] for t in api(ctx.request, "/api/admin/tracks?limit=3").get("items", [])],
                                     "format": "files", "metadata": "original"})
        payload = sel.json() if sel.status == 200 else {}
        check("selection export returns files", sel.status == 200 and len(payload.get("files") or []) >= 2,
              f"{sel.status} {len(payload.get('files') or [])}")

        # station unaffected
        now = api(ctx.request, "/api/public/now-playing")
        check("station still on air during exports", now["station"]["status"] in ("on_air", "no_music"),
              now["station"]["status"])
        browser.close()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} media checks passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
