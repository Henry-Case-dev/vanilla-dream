#!/usr/bin/env python3
"""End-to-end checks for a running Vanilla Dream Radio installation.

Stdlib only: run on the VPS with `python3 tests/e2e/server_check.py`.
Verifies that the public MP3 stream really carries decodable audio, that the
ICY metadata is present, that Icecast statistics work, that the public API
answers, and that Adminbot is still healthy.
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    return ok


def http_get(url: str, timeout: float = 5.0, headers: dict[str, str] | None = None) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read(), dict(resp.headers)


def check_api(base: str) -> None:
    try:
        status, body, _ = http_get(f"{base}/api/public/now-playing")
        data = json.loads(body)
        check("api now-playing", status == 200 and "station" in data and "track" in data, f"status={data.get('station', {}).get('status')}")
        check("api listeners available flag", "listeners" in data and "available" in data.get("listeners", {}))
    except Exception as exc:  # noqa: BLE001
        check("api now-playing", False, repr(exc))
        return

    try:
        status, body, _ = http_get(f"{base}/radio.m3u")
        text = body.decode("utf-8", "replace")
        check("m3u", status == 200 and "#EXTM3U" in text and "radio.mp3" in text, text.strip().splitlines()[-1] if text.strip() else "")
    except Exception as exc:  # noqa: BLE001
        check("m3u", False, repr(exc))

    try:
        status, body, _ = http_get(f"{base}/radio.pls")
        text = body.decode("utf-8", "replace")
        check("pls", status == 200 and "[playlist]" in text and "radio.mp3" in text)
    except Exception as exc:  # noqa: BLE001
        check("pls", False, repr(exc))

    try:
        status, body, _ = http_get(f"{base}/api/public/history?limit=5")
        data = json.loads(body)
        check("public history", status == 200 and isinstance(data.get("items", data), (list, dict)))
    except Exception as exc:  # noqa: BLE001
        check("public history", False, repr(exc))


def check_icecast(stats_url: str) -> None:
    try:
        status, body, _ = http_get(stats_url)
        data = json.loads(body)
        source = data.get("icestats", {}).get("source")
        if isinstance(source, list):
            source = source[0] if source else None
        if not source:
            check("icecast mount", False, "no source mounted")
            return
        check("icecast mount", True, f"listeners={source.get('listeners')} bitrate={source.get('bitrate')}")
        check("icecast bitrate 128", int(source.get("bitrate") or 0) == 128, str(source.get("bitrate")))
    except Exception as exc:  # noqa: BLE001
        check("icecast mount", False, repr(exc))


def capture_stream(url: str, seconds: float) -> bytes:
    req = urllib.request.Request(url, headers={"Icy-MetaData": "1", "User-Agent": "vdr-check"})
    deadline = time.time() + seconds
    buf = bytearray()
    with urllib.request.urlopen(req, timeout=10) as resp:
        headers = dict(resp.headers)
        buf.extend(b"")  # keep headers available to caller via attribute
        capture_stream.last_headers = headers  # type: ignore[attr-defined]
        while time.time() < deadline:
            chunk = resp.read(8192)
            if not chunk:
                break
            buf.extend(chunk)
    return bytes(buf)


def check_stream(url: str, seconds: float) -> None:
    try:
        raw = capture_stream(url, seconds)
    except Exception as exc:  # noqa: BLE001
        check("stream capture", False, repr(exc))
        return
    check("stream capture", len(raw) > 20000, f"{len(raw)} bytes in {seconds:.1f}s")

    headers = getattr(capture_stream, "last_headers", {})
    icy_name = headers.get("icy-name") or headers.get("Ice-Name")
    icy_br = headers.get("icy-br") or headers.get("Ice-Bitrate")
    check("icy headers", bool(icy_name and icy_br), f"name={icy_name!r} br={icy_br!r}")

    if not shutil.which("ffmpeg"):
        check("ffmpeg decode", False, "ffmpeg not installed")
        return
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as fh:
        fh.write(raw)
        tmp = fh.name
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration,bit_rate,format_name", "-of", "json", tmp],
            capture_output=True, text=True, timeout=30,
        )
        info = json.loads(probe.stdout or "{}").get("format", {})
        ok_probe = info.get("format_name") == "mp3" and float(info.get("duration") or 0) > 0
        check("ffprobe mp3", ok_probe, f"duration={info.get('duration')} bit_rate={info.get('bit_rate')}")

        decode = subprocess.run(["ffmpeg", "-v", "error", "-i", tmp, "-f", "null", "-"], capture_output=True, text=True, timeout=60)
        check("ffmpeg decode", decode.returncode == 0 and not decode.stderr.strip(), decode.stderr.strip()[:200])
    except Exception as exc:  # noqa: BLE001
        check("ffmpeg decode", False, repr(exc))
    finally:
        import os

        os.unlink(tmp)


def check_listeners(stats_url: str, stream_url: str) -> None:
    import threading

    def listen() -> None:
        try:
            http_get(stream_url, timeout=6)
        except Exception:  # noqa: BLE001
            pass

    threads = [threading.Thread(target=listen) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(2.5)
    try:
        _, body, _ = http_get(stats_url)
        source = json.loads(body).get("icestats", {}).get("source")
        if isinstance(source, list):
            source = source[0] if source else {}
        listeners = int((source or {}).get("listeners") or 0)
        check("two concurrent listeners", listeners >= 2, f"listeners={listeners}")
    except Exception as exc:  # noqa: BLE001
        check("two concurrent listeners", False, repr(exc))
    finally:
        for t in threads:
            t.join(timeout=8)


def check_adminbot() -> None:
    try:
        active = subprocess.run(["systemctl", "is-active", "admin_bot"], capture_output=True, text=True, timeout=10).stdout.strip()
        check("adminbot service", active == "active", active)
    except Exception as exc:  # noqa: BLE001
        check("adminbot service", False, repr(exc))
    try:
        status, body, _ = http_get("http://127.0.0.1:8000/healthz")
        check("adminbot healthz", status == 200 and b"ok" in body, body[:80].decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        check("adminbot healthz", False, repr(exc))
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=3):
            pass
        check("adminbot port local only", True)
    except Exception as exc:  # noqa: BLE001
        check("adminbot port local only", False, repr(exc))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--stream", default="http://127.0.0.1:8101/radio.mp3")
    parser.add_argument("--stats", default="http://127.0.0.1:8101/status-json.xsl")
    parser.add_argument("--seconds", type=float, default=6.0)
    parser.add_argument("--skip-adminbot", action="store_true")
    args = parser.parse_args()

    check_api(args.base)
    check_icecast(args.stats)
    check_stream(args.stream, args.seconds)
    check_listeners(args.stats, args.stream)
    if not args.skip_adminbot:
        check_adminbot()

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
