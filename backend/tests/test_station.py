"""STATION-01..05: allow-listed systemctl, state machine, desired state, recovery."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app import db
from app.config import UNIT_LIQUIDSOAP, Settings
from app.main import create_app
from app.station import StationController, compute_state
from tests.helpers import play_next, upload_and_get, write_mp3


def _upload(env, name: str) -> dict:
    return upload_and_get(env.client, write_mp3(env.settings.music_dir.parent / f"{name}.mp3", title=name))


def test_state_machine_mapping(env):
    now = env.clock.now()
    base = dict(
        desired="playing",
        unit_active=True,
        unit_state="active",
        mount_present=True,
        has_current=True,
        active_since=now - timedelta(seconds=5),
        now=now,
        recent_failures=0,
        has_library=True,
    )
    assert compute_state(desired="stopped", **{k: v for k, v in base.items() if k != "desired"}) == "off"
    assert compute_state(**base) == "on_air"
    assert compute_state(**{**base, "has_current": False}) == "no_music"
    assert compute_state(**{**base, "mount_present": False}) == "starting"
    assert (
        compute_state(
            **{**base, "mount_present": False, "active_since": now - timedelta(seconds=45)}
        )
        == "reconnecting"
    )
    assert compute_state(**{**base, "unit_state": "failed"}) == "error"
    assert compute_state(**{**base, "recent_failures": 3}) == "error"
    assert (
        compute_state(**{**base, "unit_active": False, "unit_state": "inactive"}) == "error"
    )
    assert (
        compute_state(
            **{**base, "unit_active": False, "unit_state": "activating", "mount_present": False}
        )
        == "starting"
    )
    assert compute_state(**{**base, "has_library": False}) == "no_music"


def _sudo_calls(env):
    return [call for call in env.runner.calls if call and call[0] == "sudo"]


def _control_calls(env):
    return [call for call in _sudo_calls(env) if call[-2] in ("start", "stop", "restart")]


def test_start_stop_use_exact_allow_listed_commands(env, admin):
    _upload(env, "station1")
    started = env.client.post("/api/admin/station/start")
    assert started.status_code == 200, started.text
    assert _control_calls(env)[0] == [
        "sudo",
        "-n",
        "/usr/bin/systemctl",
        "start",
        UNIT_LIQUIDSOAP,
    ]
    conn = db.open_db(env.settings)
    try:
        assert db.get_setting(conn, "desired_state") == "playing"
    finally:
        conn.close()

    stopped = env.client.post("/api/admin/station/stop")
    assert stopped.status_code == 200
    assert _control_calls(env)[-1] == [
        "sudo",
        "-n",
        "/usr/bin/systemctl",
        "stop",
        UNIT_LIQUIDSOAP,
    ]
    assert stopped.json()["state"] == "off"


def test_empty_library_starts_technical_silence(env, admin):
    response = env.client.post("/api/admin/station/start")
    assert response.status_code == 200
    body = response.json()
    assert body["desired"] == "playing"
    assert body["state"] == "no_music"
    assert body["reason"] == "library_empty"
    assert [call for call in _sudo_calls(env) if call[-2] == "start"]

    _upload(env, "late")
    env.app.state.scheduler.tick()
    status = env.client.get("/api/admin/station/status").json()
    assert status["desired"] == "playing"


def test_restart_requires_playing(env, admin):
    _upload(env, "restart1")
    blocked = env.client.post("/api/admin/station/restart")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "station_stopped"
    assert not [call for call in _sudo_calls(env) if call[-2] == "restart"]

    env.client.post("/api/admin/station/start")
    restarted = env.client.post("/api/admin/station/restart")
    assert restarted.status_code == 200
    assert _control_calls(env)[-1] == [
        "sudo",
        "-n",
        "/usr/bin/systemctl",
        "restart",
        UNIT_LIQUIDSOAP,
    ]
    conn = db.open_db(env.settings)
    try:
        assert db.get_setting(conn, "desired_state") == "playing"
    finally:
        conn.close()


def test_failed_start_is_reported_and_not_faked(env, admin):
    _upload(env, "failstart")
    env.runner.fail_actions.add("start")
    response = env.client.post("/api/admin/station/start")
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "engine_start_failed"
    conn = db.open_db(env.settings)
    try:
        assert "boom" in db.get_setting(conn, "engine_last_error")
        assert db.get_setting(conn, "desired_state") == "playing"
    finally:
        conn.close()


def test_auto_start_is_throttled(env, admin):
    _upload(env, "auto")
    env.client.post("/api/admin/station/start")
    env.client.post("/api/admin/station/stop")

    def set_desired(value: str) -> None:
        conn = db.open_db(env.settings)
        try:
            db.set_setting(conn, "desired_state", value)
            conn.commit()
        finally:
            conn.close()

    def status_now() -> dict:
        conn = db.open_db(env.settings)
        try:
            return env.station.status(conn, env.clock.now())
        finally:
            conn.close()

    def auto_start() -> bool:
        conn = db.open_db(env.settings)
        try:
            return env.station.auto_start_if_needed(conn, env.clock.now(), status_now())
        finally:
            conn.close()

    # Simulate an unexpected engine exit with desired=playing.
    set_desired("playing")
    env.runner.units[UNIT_LIQUIDSOAP] = "inactive"
    starts_before = len([call for call in _sudo_calls(env) if call[-2] == "start"])
    assert auto_start() is True
    env.runner.units[UNIT_LIQUIDSOAP] = "inactive"
    assert auto_start() is False
    starts_after = len([call for call in _sudo_calls(env) if call[-2] == "start"])
    assert starts_after - starts_before == 1

    env.clock.advance(11)
    env.runner.units[UNIT_LIQUIDSOAP] = "inactive"
    assert auto_start() is True


def test_services_and_links_and_storage(env, admin):
    track = _upload(env, "svc")
    base = env.settings.public_base_url
    links = env.client.get("/api/admin/links").json()["links"]
    by_key = {item["key"]: item for item in links}
    assert "world_radio" not in by_key
    assert by_key["mp3"]["url"] == f"{base}/radio.mp3"
    assert by_key["mp3"]["primary"] is True
    assert "http_mp3" not in by_key  # VD_PUBLIC_HTTP_BASE_URL is not configured
    assert by_key["m3u"]["url"] == f"{base}/radio.m3u"
    assert by_key["pls"]["url"] == f"{base}/radio.pls"
    assert by_key["player"]["url"] == f"{base}/"
    assert by_key["metadata"]["url"] == f"{base}/api/public/now-playing"
    for item in links:
        assert set(("key", "label", "url", "description")) <= set(item)

    services = env.client.get("/api/admin/services").json()["services"]
    by_id = {item["id"]: item for item in services}
    assert set(by_id) == {"api", "liquidsoap", "icecast"}
    for item in services:
        assert set(
            ("id", "unit", "label", "state", "state_label", "detail", "last_start", "last_error")
        ) <= set(item)
    assert by_id["api"]["state"] == "active"
    assert by_id["api"]["detail"] == "процесс отвечает"
    assert by_id["liquidsoap"]["state"] == "inactive"
    assert by_id["liquidsoap"]["state_label"] == "Остановлена"
    assert by_id["liquidsoap"]["last_start"]

    storage = env.client.get("/api/admin/storage").json()
    assert storage["tracks"] == 1
    assert storage["bytes"] == track["size"]
    assert isinstance(storage["free_bytes"], int)
    assert isinstance(storage["total_bytes"], int)
    assert isinstance(storage["cover_bytes"], int)
    assert isinstance(storage["db_bytes"], int)
    assert isinstance(storage["limit_gb"], (int, float))
    assert "music_dir" not in storage


def test_startup_resumes_desired_playing(env, admin):
    _upload(env, "resume")
    conn = db.open_db(env.settings)
    try:
        db.set_setting(conn, "desired_state", "playing")
        conn.commit()
    finally:
        conn.close()
    app2 = create_app(
        settings=env.settings,
        clock=env.clock,
        station=env.station,
        engine=env.engine,
        icecast=env.icecast,
    )
    with TestClient(app2) as client:
        login = client.post("/api/auth/login", json={"password": env.admin_password})
        assert login.status_code == 200
        calls = [call for call in env.runner.calls if call[-2] == "start"]
        assert calls
        status = client.get("/api/admin/station/status").json()
        assert status["desired"] == "playing"


def test_auto_start_stops_after_repeated_failures(env, admin):
    _upload(env, "loopfail")

    def set_desired(value: str) -> None:
        conn = db.open_db(env.settings)
        try:
            db.set_setting(conn, "desired_state", value)
            conn.commit()
        finally:
            conn.close()

    def status_now() -> dict:
        conn = db.open_db(env.settings)
        try:
            return env.station.status(conn, env.clock.now())
        finally:
            conn.close()

    def auto_start() -> bool:
        conn = db.open_db(env.settings)
        try:
            return env.station.auto_start_if_needed(conn, env.clock.now(), status_now())
        finally:
            conn.close()

    set_desired("playing")
    env.runner.fail_actions.add("start")
    env.runner.units[UNIT_LIQUIDSOAP] = "inactive"
    for _ in range(3):
        env.runner.units[UNIT_LIQUIDSOAP] = "inactive"
        assert auto_start() is False
        env.clock.advance(11)
    attempts = len([call for call in _sudo_calls(env) if call[-2] == "start"])
    env.runner.units[UNIT_LIQUIDSOAP] = "inactive"
    assert auto_start() is False
    after = len([call for call in _sudo_calls(env) if call[-2] == "start"])
    assert attempts == after == 3
    status = status_now()
    assert status["state"] == "error"


def test_start_failure_marks_error_state(env, admin):
    _upload(env, "errmark")
    env.runner.fail_actions.add("start")
    env.client.post("/api/admin/station/start")
    conn = db.open_db(env.settings)
    try:
        status = env.station.status(conn, env.clock.now())
    finally:
        conn.close()
    assert status["state"] == "error"
    assert status["last_error"]


def test_station_play_now_and_play_next_endpoints(env, admin):
    first = upload_and_get(
        env.client, write_mp3(env.settings.music_dir.parent / "spn1.mp3", title="SPN1")
    )
    second = upload_and_get(
        env.client, write_mp3(env.settings.music_dir.parent / "spn2.mp3", title="SPN2")
    )
    env.client.post("/api/admin/station/start")
    played = env.client.post(f"/api/admin/station/play-now/{first['id']}")
    assert played.status_code == 200
    assert played.json()["items"][0]["track"]["id"] == first["id"]
    assert env.engine.skip_calls == 1
    queued = env.client.post(f"/api/admin/station/play-next/{second['id']}")
    assert queued.status_code == 200
    assert queued.json()["items"][0]["track"]["id"] == second["id"]
    assert env.engine.skip_calls == 1


def test_unit_state_ttl_is_configurable(monkeypatch):
    monkeypatch.setenv("VD_UNIT_STATE_TTL", "7.5")
    assert Settings.from_env().unit_state_ttl == 7.5


def test_unit_state_is_cached_for_ttl_and_invalidated_by_controls(env, admin):
    def is_active_calls() -> int:
        return len(
            [
                call
                for call in env.runner.calls
                if call[0] == "sudo" and call[-2] == "is-active"
            ]
        )

    controller = StationController(
        env.settings, env.clock, engine=env.engine, icecast=env.icecast, runner=env.runner
    )
    conn = db.open_db(env.settings)
    try:
        controller.status(conn, env.clock.now())
        first = is_active_calls()
        assert first == 1
        controller.status(conn, env.clock.now())
        assert is_active_calls() == first  # within TTL: no new process
        env.clock.advance(1)
        controller.status(conn, env.clock.now())
        assert is_active_calls() == first
        controller.stop(conn, env.clock.now())  # own stop invalidates immediately
        after_stop = is_active_calls()
        assert after_stop == first + 1
        controller.status(conn, env.clock.now())
        assert is_active_calls() == after_stop  # first read after stop repopulates cache
        env.clock.advance(3.1)
        controller.status(conn, env.clock.now())
        assert is_active_calls() == after_stop + 1
    finally:
        conn.close()


def test_skip_returns_503_only_on_real_engine_failure(env, admin):
    _upload(env, "skipfail")
    env.client.post("/api/admin/station/start")
    original = env.station.engine

    class BrokenEngine:
        def sync(self):
            return True

        def skip(self):
            return False

    env.station.engine = BrokenEngine()
    failed = env.client.post("/api/admin/station/skip")
    assert failed.status_code == 503
    assert failed.json()["detail"]["code"] == "engine_unavailable"

    env.station.engine = original
    ok = env.client.post("/api/admin/station/skip")
    assert ok.status_code == 200


def test_admin_station_status_has_status_alias(env, admin):
    def status() -> dict:
        response = env.client.get("/api/admin/station/status")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == body["state"]
        return body

    off = status()
    assert off["state"] == "off"

    env.client.post("/api/admin/station/start")
    no_music = status()
    assert no_music["state"] == "no_music"

    _upload(env, "status-on-air")
    env.icecast.payload = {
        "available": True,
        "mount_present": True,
        "listeners": 1,
        "bitrate": 128,
        "title": None,
        "error": None,
    }
    play_next(env)
    on_air = status()
    assert on_air["state"] == "on_air"

    env.runner.units[UNIT_LIQUIDSOAP] = "failed"
    env.station.invalidate_unit_cache()
    error = status()
    assert error["state"] == "error"


def test_engine_unit_and_sudo_are_configurable(env, admin):
    import dataclasses

    custom = dataclasses.replace(
        env.settings,
        engine_unit="vanilla-dream-radio-liquidsoap-e2e.service",
        station_sudo=False,
        engine_label="Liquidsoap E2E",
    )
    controller = StationController(
        custom, env.clock, engine=env.engine, icecast=env.icecast, runner=env.runner
    )
    conn = db.open_db(custom)
    try:
        controller._start_unit(conn, env.clock.now(), raise_on_error=False)
        engine_calls = [
            call for call in env.runner.calls if call[-1] == custom.engine_unit
        ]
        assert engine_calls
        assert all("sudo" not in call for call in engine_calls)
        assert engine_calls[-1] == [
            "/usr/bin/systemctl",
            "start",
            custom.engine_unit,
        ]
        status = controller.status(conn, env.clock.now())
        assert status["unit"] == custom.engine_unit
        services = {item["id"]: item for item in controller.services(conn)}
        assert services["liquidsoap"]["unit"] == custom.engine_unit
        assert services["liquidsoap"]["label"] == "Liquidsoap E2E"
    finally:
        conn.close()


def test_engine_env_defaults_and_overrides(monkeypatch):
    for name in ("VD_ENGINE_UNIT", "VD_STATION_SUDO", "VD_ENGINE_LABEL"):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings.from_env()
    assert defaults.engine_unit == "vanilla-dream-radio-liquidsoap.service"
    assert defaults.station_sudo is True
    assert defaults.engine_label == "Liquidsoap"
    assert defaults.engine_start_grace == 10.0

    monkeypatch.setenv("VD_ENGINE_UNIT", "vanilla-dream-radio-liquidsoap-e2e.service")
    monkeypatch.setenv("VD_STATION_SUDO", "0")
    monkeypatch.setenv("VD_ENGINE_LABEL", "Liquidsoap E2E")
    monkeypatch.setenv("VD_ENGINE_START_GRACE", "2.5")
    custom = Settings.from_env()
    assert custom.engine_unit == "vanilla-dream-radio-liquidsoap-e2e.service"
    assert custom.station_sudo is False
    assert custom.engine_label == "Liquidsoap E2E"
    assert custom.engine_start_grace == 2.5


def test_stop_failure_keeps_desired_state(env, admin):
    _upload(env, "stopfail")
    env.client.post("/api/admin/station/start")
    env.runner.fail_actions.add("stop")
    response = env.client.post("/api/admin/station/stop")
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "engine_stop_failed"
    conn = db.open_db(env.settings)
    try:
        assert db.get_setting(conn, "desired_state") == "playing"
    finally:
        conn.close()
