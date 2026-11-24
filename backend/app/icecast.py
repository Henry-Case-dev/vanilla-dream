"""Icecast statistics client (status-json.xsl) with a short cache."""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.request
from typing import Any, Callable

from .config import Settings

LOGGER = logging.getLogger("vdradio.icecast")
MOUNT = "/radio.mp3"


def _default_fetch(url: str, user: str, password: str, timeout: float) -> dict:
    request = urllib.request.Request(url)
    if user or password:
        credentials = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        request.add_header("Authorization", f"Basic {credentials}")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed status URL
        body = response.read(1024 * 512)
    return json.loads(body.decode("utf-8", errors="replace"))


class IcecastClient:
    def __init__(
        self,
        settings: Settings,
        fetcher: Callable[[str, str, str, float], dict] | None = None,
        ttl: float = 5.0,
    ) -> None:
        self.settings = settings
        self.fetcher = fetcher or _default_fetch
        self.ttl = ttl
        self._cached: dict | None = None
        self._cached_at = 0.0

    def _empty(self, available: bool, error: str | None = None) -> dict:
        return {
            "available": available,
            "mount_present": False,
            "listeners": None,
            "bitrate": None,
            "title": None,
            "error": error,
        }

    def stats(self) -> dict:
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < self.ttl:
            return self._cached
        try:
            data = self.fetcher(
                self.settings.icecast_stats_url,
                self.settings.icecast_admin_user,
                self.settings.icecast_admin_password,
                2.0,
            )
        except Exception as exc:  # noqa: BLE001 - network errors are expected
            LOGGER.debug("icecast stats unavailable: %s", exc)
            result = self._empty(False, "stats_unavailable")
            self._cached, self._cached_at = result, now
            return result
        result = self._parse(data)
        self._cached, self._cached_at = result, now
        return result

    def invalidate(self) -> None:
        self._cached = None
        self._cached_at = 0.0

    @staticmethod
    def _parse(data: Any) -> dict:
        sources: list[dict] = []
        if isinstance(data, dict):
            icestats = data.get("icestats") if "icestats" in data else data
            if isinstance(icestats, dict):
                source = icestats.get("source")
                if isinstance(source, list):
                    sources = [item for item in source if isinstance(item, dict)]
                elif isinstance(source, dict):
                    sources = [source]
        if not sources:
            result = {
                "available": True,
                "mount_present": False,
                "listeners": 0,
                "bitrate": None,
                "title": None,
                "error": None,
            }
            return result
        chosen = sources[0]
        for item in sources:
            mount = str(item.get("listenurl") or item.get("mount") or "")
            if mount.endswith(MOUNT):
                chosen = item
                break
        return {
            "available": True,
            "mount_present": True,
            "listeners": int(chosen.get("listeners") or 0),
            "bitrate": _safe_int(chosen.get("bitrate") or chosen.get("ice-bitrate"))
            or _audio_info_int(chosen.get("audio_info"), "bitrate"),
            "title": chosen.get("title") or chosen.get("server_name") or None,
            "error": None,
        }


def _audio_info_int(value: Any, key: str) -> int | None:
    """Icecast 2.4 reports the real encoder parameters inside `audio_info`."""
    if not isinstance(value, str):
        return None
    for part in value.split(";"):
        name, _, raw = part.partition("=")
        if name.strip() == key:
            return _safe_int(raw.strip())
    return None


def _safe_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
