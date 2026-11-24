"""STATION-01..05: allow-listed systemctl, state machine, desired state, recovery."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app import db
from app.config import UNIT_LIQUIDSOAP, Settings
from app.main import create_app
from app.station import StationController, compute_state
from tests.helpers import upload_and_get, write_mp3


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


def test_empty_library_sets_no_music_without_start(env, admin):
    response = env.client.post("/api/admin/station/start")
    assert response.status_code == 200
    body = response.json()
    assert body["desired"] == "playing"
    assert body["state"] == "no_music"
    assert not [call for call in _sudo_calls(env) if call[-2] == "start"]

    # Uploading music with desired=playing makes the scheduler start the engine.
    _upload(env, "late")
    env.app.state.scheduler.tick()
    starts = [call for call in _sudo_calls(env) if call[-2] == "start"]
    assert len(starts) == 1


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
    links = env.client.get("/api/admin/links").json()
    assert links["world_radio"] == f"{base}/radio.mp3"
    assert links["mp3"] == f"{base}/radio.mp3"
    assert links["http_mp3"] == f"{base.replace('https://', 'http://')}/radio.mp3"
    assert links["m3u"] == f"{base}/radio.m3u"
    assert links["pls"] == f"{base}/radio.pls"
    assert links["player"] == f"{base}/"
    assert links["admin"] == f"{base}/admin/"

    services = env.client.get("/api/admin/services").json()
    labels = {item["label"]: item["state"] for item in services["services"]}
    assert labels["Backend"] == "active"
    assert labels["Liquidsoap"] == "inactive"
    backend = next(
        item for item in services["services"] if item["label"] == "Backend"
    )
    assert backend["detail"] == "процесс отвечает"
    assert "last_start" in services and "last_error" in services

    storage = env.client.get("/api/admin/storage").json()
    assert storage["tracks"] == 1
    assert storage["bytes"] == track["size"]
    assert storage["free_bytes"] > 0
    assert storage["total_bytes"] >= storage["free_bytes"]
    assert storage["limit_gb"] == 0
    assert storage["music_dir"] == str(env.settings.music_dir)


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
