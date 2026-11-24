"""Shared fixtures: isolated temp settings, fake engine/station, logged-in client."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, db
from app.config import Settings
from app.main import create_app
from app.station import StationController
from app.util import Clock

INTERNAL_TOKEN = "test-internal-token"
ADMIN_PASSWORD = "correct-horse-9"


class FakeClock(Clock):
    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> datetime:
        self.current = self.current + timedelta(seconds=seconds)
        return self.current

    def set(self, value: datetime) -> None:
        self.current = value


class FakeEngine:
    def __init__(self) -> None:
        self.sync_ok = True
        self.skip_ok = True
        self.sync_calls = 0
        self.skip_calls = 0
        self.commands: list[str] = []

    def sync(self) -> bool:
        self.sync_calls += 1
        self.commands.append("vd_sync")
        return self.sync_ok

    def skip(self) -> bool:
        self.skip_calls += 1
        self.commands.append("radio.skip")
        return self.skip_ok

    def command(self, name: str):
        self.commands.append(name)
        return True, "OK\nEND"


class FakeIcecast:
    def __init__(self) -> None:
        self.payload = {
            "available": True,
            "mount_present": False,
            "listeners": 0,
            "bitrate": None,
            "title": None,
            "error": None,
        }

    def stats(self) -> dict:
        return dict(self.payload)

    def invalidate(self) -> None:
        return None


class FakeRunner:
    """Simulates `sudo -n systemctl ...` without touching a real service manager."""

    def __init__(self) -> None:
        self.units: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.fail_actions: set[str] = set()

    def __call__(self, command, capture_output=True, text=True, timeout=None, env=None):
        self.calls.append(list(command))
        action = command[-2]
        unit = command[-1]
        if action in self.fail_actions:
            return subprocess.CompletedProcess(command, 1, "", "boom")
        if action == "is-active":
            state = self.units.get(unit, "inactive")
            code = 0 if state == "active" else 3
            return subprocess.CompletedProcess(command, code, state + "\n", "")
        if action == "start":
            self.units[unit] = "active"
            return subprocess.CompletedProcess(command, 0, "", "")
        if action == "stop":
            self.units[unit] = "inactive"
            return subprocess.CompletedProcess(command, 0, "", "")
        if action == "restart":
            self.units[unit] = "active"
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    music = tmp_path / "music"
    covers = tmp_path / "covers"
    engine_dir = tmp_path / "run"
    return Settings(
        db_path=tmp_path / "app.db",
        music_dir=music,
        cover_dir=covers,
        engine_dir=engine_dir,
        engine_queue_file=engine_dir / "engine-queue.txt",
        engine_socket=engine_dir / "liquidsoap.sock",
        queue_link_dir=engine_dir / "queue",
        encoder_file=tmp_path / "etc" / "encoder.liq",
        station_file=tmp_path / "etc" / "station.liq",
        internal_token=INTERNAL_TOKEN,
        public_base_url="https://vanilladream.duckdns.org",
        backend_url="http://127.0.0.1:8100",
        icecast_stats_url="http://127.0.0.1:8101/status-json.xsl",
        icecast_admin_user="admin",
        icecast_admin_password="icecast-secret",
        secure_cookies=False,
        scheduler_tick=3600.0,
        frontend_dist=tmp_path / "frontend",
        log_dir=tmp_path / "logs",
        min_free_bytes=0,
        scheduler_enabled=False,
    )


@pytest.fixture
def env(settings, clock):
    engine = FakeEngine()
    icecast = FakeIcecast()
    runner = FakeRunner()
    station = StationController(settings, clock, engine=engine, icecast=icecast, runner=runner)
    app = create_app(
        settings=settings, clock=clock, station=station, engine=engine, icecast=icecast
    )
    app.state.icecast = icecast
    app.state.engine = engine
    with TestClient(app) as client:
        yield SimpleNamespace(
            app=app,
            client=client,
            settings=app.state.settings,
            clock=clock,
            engine=engine,
            icecast=icecast,
            station=station,
            runner=runner,
            internal_token=INTERNAL_TOKEN,
            admin_password=ADMIN_PASSWORD,
        )


@pytest.fixture
def admin(env) -> str:
    conn = db.open_db(env.settings)
    try:
        auth.set_admin_password(conn, ADMIN_PASSWORD, env.clock.now())
        conn.commit()
    finally:
        conn.close()
    response = env.client.post("/api/auth/login", json={"password": ADMIN_PASSWORD})
    assert response.status_code == 200, response.text
    csrf = response.json()["csrf_token"]
    env.client.headers.update({"X-CSRF-Token": csrf})
    return csrf


def with_settings(**overrides):
    base = Settings.from_env()
    return base, overrides
