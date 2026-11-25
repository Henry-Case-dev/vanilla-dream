"""Runtime configuration.

Every value comes from a ``VD_`` environment variable so deployment secrets never
live in code.  The defaults match the production layout from the frozen contract;
local development and tests build ``Settings`` explicitly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BITRATE = 128
SUPPORTED_BITRATES = (96, 112, 128, 160, 192, 224, 256, 320)
SYSTEM_PLAYLIST_ID = "library"
SYSTEM_PLAYLIST_NAME = "Вся медиатека"
SYSTEM_PLAYLIST_DESCRIPTION = "Все треки медиатеки. Обновляется автоматически."

UNIT_LIQUIDSOAP = "vanilla-dream-radio-liquidsoap.service"
UNIT_ICECAST = "vanilla-dream-radio-icecast.service"
UNIT_API = "vanilla-dream-radio-api.service"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)) or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    db_path: Path
    music_dir: Path
    cover_dir: Path
    engine_dir: Path
    engine_queue_file: Path
    engine_socket: Path
    queue_link_dir: Path
    encoder_file: Path
    station_file: Path
    internal_token: str
    public_base_url: str
    backend_url: str
    icecast_stats_url: str
    icecast_admin_user: str
    icecast_admin_password: str
    secure_cookies: bool
    scheduler_tick: float
    frontend_dist: Path
    log_dir: Path
    min_free_bytes: int
    scheduler_enabled: bool = True
    unit_state_ttl: float = 3.0
    public_http_base_url: str = ""
    engine_unit: str = UNIT_LIQUIDSOAP
    station_sudo: bool = True
    engine_label: str = "Liquidsoap"
    playlist_cover_dir: Path | None = None
    engine_start_grace: float = 10.0

    def __post_init__(self) -> None:
        if self.playlist_cover_dir is None:
            object.__setattr__(self, "playlist_cover_dir", self.cover_dir / "playlists")

    @classmethod
    def from_env(cls) -> "Settings":
        engine_dir = Path(_env("VD_ENGINE_DIR", "/run/vanilla-dream-radio") or "/run/vanilla-dream-radio")
        cover_dir = Path(_env("VD_COVER_DIR", "/srv/vanilla-dream-radio/covers") or "")
        return cls(
            db_path=Path(_env("VD_DB_PATH", "/var/lib/vanilla-dream-radio/app.db") or ""),
            music_dir=Path(_env("VD_MUSIC_DIR", "/srv/vanilla-dream-radio/music") or ""),
            cover_dir=cover_dir,
            playlist_cover_dir=Path(
                _env("VD_PLAYLIST_COVER_DIR", str(cover_dir / "playlists")) or ""
            ),
            engine_dir=engine_dir,
            engine_queue_file=Path(
                _env("VD_ENGINE_QUEUE_FILE", str(engine_dir / "engine-queue.txt")) or ""
            ),
            engine_socket=Path(
                _env("VD_ENGINE_SOCKET", str(engine_dir / "liquidsoap.sock")) or ""
            ),
            queue_link_dir=Path(
                _env("VD_QUEUE_LINK_DIR", str(engine_dir / "queue")) or ""
            ),
            encoder_file=Path(_env("VD_ENCODER_FILE", "/etc/vanilla-dream-radio/encoder.liq") or ""),
            station_file=Path(_env("VD_STATION_FILE", "/etc/vanilla-dream-radio/station.liq") or ""),
            internal_token=_env("VD_INTERNAL_TOKEN", "") or "",
            public_base_url=(_env("VD_PUBLIC_BASE_URL", "https://vanilladream.duckdns.org") or "").rstrip("/"),
            backend_url=(_env("VD_BACKEND_URL", "http://127.0.0.1:8100") or "").rstrip("/"),
            icecast_stats_url=_env("VD_ICECAST_STATS_URL", "http://127.0.0.1:8101/status-json.xsl") or "",
            icecast_admin_user=_env("VD_ICECAST_ADMIN_USER", "admin") or "admin",
            icecast_admin_password=_env("VD_ICECAST_ADMIN_PASSWORD", "") or "",
            secure_cookies=_env_bool("VD_SECURE_COOKIES", False),
            scheduler_tick=_env_float("VD_SCHEDULER_TICK", 5.0),
            frontend_dist=Path(_env("VD_FRONTEND_DIST", "/opt/vanilla-dream-radio/frontend-dist") or ""),
            log_dir=Path(_env("VD_LOG_DIR", "/var/log/vanilla-dream-radio") or ""),
            min_free_bytes=_env_int("VD_MIN_FREE_BYTES", 3 * 1024 * 1024 * 1024),
            scheduler_enabled=_env_bool("VD_SCHEDULER_ENABLED", True),
            unit_state_ttl=_env_float("VD_UNIT_STATE_TTL", 3.0),
            public_http_base_url=(_env("VD_PUBLIC_HTTP_BASE_URL", "") or "").rstrip("/"),
            engine_unit=_env("VD_ENGINE_UNIT", UNIT_LIQUIDSOAP) or UNIT_LIQUIDSOAP,
            station_sudo=_env_bool("VD_STATION_SUDO", True),
            engine_label=_env("VD_ENGINE_LABEL", "Liquidsoap") or "Liquidsoap",
            engine_start_grace=_env_float("VD_ENGINE_START_GRACE", 10.0),
        )

    def ensure_directories(self) -> None:
        for path in (
            self.music_dir,
            self.cover_dir,
            self.playlist_cover_dir,
            self.engine_dir,
            self.queue_link_dir,
            self.db_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)
