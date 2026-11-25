#!/usr/bin/env python3
"""Isolated harness front door: static frontend + reverse proxy to the e2e station.

Listens on 127.0.0.1:8300, serves the built frontend from /opt/vanilla-dream-radio/frontend-dist,
proxies /api/* and /radio.* to the isolated API/Icecast ports and streams the audio
body without buffering. Test-only: bound to localhost, never installed as a service.
"""
from __future__ import annotations

import http.client
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DIST = Path(os.environ.get("VDR_E2E_DIST", "/opt/vanilla-dream-radio/frontend-dist"))
API = ("127.0.0.1", int(os.environ.get("VDR_E2E_API_PORT", "8301")))
ICECAST = ("127.0.0.1", int(os.environ.get("VDR_E2E_ICECAST_PORT", "8401")))
PORT = int(os.environ.get("VDR_E2E_PROXY_PORT", "8300"))

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".json": "application/json",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):  # keep the harness quiet
        pass

    def _proxy(self, target, streaming: bool = False) -> None:
        path = self.path
        body = None
        length = self.headers.get("Content-Length")
        if length:
            body = self.rfile.read(int(length))
        conn = http.client.HTTPConnection(*target, timeout=600 if streaming else 60)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in {"host", "connection"}}
        headers["Host"] = "127.0.0.1:%d" % target[1]
        conn.request(self.command, path, body=body, headers=headers)
        resp = conn.getresponse()
        self.send_response(resp.status)
        for key, value in resp.getheaders():
            if key.lower() in {"transfer-encoding", "connection", "content-length", "content-encoding"}:
                continue
            self.send_header(key, value)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(16384)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()
            self.close_connection = True

    def _static(self) -> None:
        path = self.path.split("?", 1)[0]
        if path.startswith("/admin"):
            path = "/index.html"
        candidate = (DIST / path.lstrip("/")).resolve()
        if not str(candidate).startswith(str(DIST.resolve())) or not candidate.is_file():
            candidate = DIST / "index.html"
        data = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(candidate.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self._proxy(API)
        if self.path.startswith("/radio.mp3"):
            return self._proxy(ICECAST, streaming=True)
        if self.path.startswith(("/radio.m3u", "/radio.pls")):
            return self._proxy(API)
        return self._static()

    def do_POST(self):
        return self._proxy(API)

    def do_PATCH(self):
        return self._proxy(API)

    def do_PUT(self):
        return self._proxy(API)

    def do_DELETE(self):
        return self._proxy(API)

    def do_HEAD(self):
        if self.path.startswith("/api/"):
            return self._proxy(API)
        self.send_response(200)
        self.end_headers()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"e2e proxy on 127.0.0.1:{PORT} dist={DIST} api={API} icecast={ICECAST}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
