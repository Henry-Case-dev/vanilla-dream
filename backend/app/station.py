"""Station control: desired vs actual state and allow-listed systemctl calls."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from . import db, media, queue_engine
from .config import UNIT_API, UNIT_ICECAST, UNIT_LIQUIDSOAP, Settings
from .liquidsoap import LiquidsoapClient
from .models import STATE_LABELS
from .util import ApiError, Clock, parse_iso, remove_queue_link, to_iso

LOGGER = logging.getLogger("vdradio.station")

SYSTEMCTL = "/usr/bin/systemctl"
STARTING_GRACE_SECONDS = 30
FAILURE_WINDOW_SECONDS = 120
FAILURE_LIMIT = 3
AUTO_START_COOLDOWN = 10.0


def compute_state(
    *,
    desired: str,
    unit_active: bool,
    unit_state: str,
    mount_present: bool,
    has_current: bool,
    active_since: datetime | None,
    now: datetime,
    recent_failures: int,
    has_library: bool = True,
) -> str:
    if desired != "playing":
        return "off"
    if not has_library:
        return "no_music"
    if unit_state == "failed" or recent_failures >= FAILURE_LIMIT:
        return "error"
    if not unit_active:
        if unit_state in ("activating", "reloading", "deactivating"):
            return "starting"
        return "error"
    if mount_present:
        return "on_air" if has_current else "no_music"
    if active_since is None:
        return "starting"
    if (now - active_since).total_seconds() <= STARTING_GRACE_SECONDS:
        return "starting"
    return "reconnecting"


class StationController:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        engine: LiquidsoapClient | None = None,
        icecast=None,
        runner=subprocess.run,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.engine = engine
        self.icecast = icecast
        self.runner = runner
        self._unit_cache: dict[str, tuple[str, datetime]] = {}

    # -- systemctl ---------------------------------------------------------
    def _systemctl(self, action: str, unit: str = UNIT_LIQUIDSOAP, sudo: bool = True):
        if action not in ("start", "stop", "restart", "is-active"):
            raise ValueError(f"systemctl action not allowed: {action}")
        command = []
        if sudo:
            command.extend(["sudo", "-n", SYSTEMCTL, action, unit])
        else:
            command.extend([SYSTEMCTL, action, unit])
        env = {
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            "LANG": "C",
        }
        try:
            result = self.runner(
                command,
                capture_output=True,
                text=True,
                timeout=20,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.warning("systemctl %s failed to run: %s", action, exc)
            return False, str(exc), -1
        output = (result.stdout or "").strip() or (result.stderr or "").strip()
        ok = result.returncode == 0
        if action == "is-active":
            ok = output in ("active", "activating", "reloading")
        return ok, output, result.returncode

    def invalidate_unit_cache(self, unit: str | None = None) -> None:
        if unit is None:
            self._unit_cache.clear()
        else:
            self._unit_cache.pop(unit, None)

    def _unit_state(self, unit: str, sudo: bool, now: datetime | None = None) -> str:
        now = now or self.clock.now()
        ttl = float(self.settings.unit_state_ttl or 0)
        cached = self._unit_cache.get(unit)
        if cached is not None and ttl > 0:
            age = (now - cached[1]).total_seconds()
            if 0 <= age < ttl:
                return cached[0]
        _ok, output, code = self._systemctl("is-active", unit, sudo=sudo)
        if code == -1:
            state = "unknown"
        else:
            state = output.splitlines()[0].strip() if output else "unknown"
        self._unit_cache[unit] = (state, now)
        return state

    def unit_info(self, now: datetime | None = None) -> tuple[bool, str]:
        state = self._unit_state(UNIT_LIQUIDSOAP, sudo=True, now=now)
        return state in ("active", "activating", "reloading"), state

    def unit_state_of(self, unit: str, now: datetime | None = None) -> str:
        return self._unit_state(unit, sudo=False, now=now)

    # -- desired state -----------------------------------------------------
    def _set_desired(self, conn, value: str) -> None:
        db.set_setting(conn, "desired_state", value)

    def desired(self, conn) -> str:
        return db.get_setting(conn, "desired_state", "stopped") or "stopped"

    def _failures(self, conn, now: datetime) -> list[str]:
        try:
            values = json.loads(db.get_setting(conn, "_engine_failures") or "[]")
        except (TypeError, ValueError):
            values = []
        recent = []
        for value in values:
            moment = parse_iso(value)
            if moment and (now - moment).total_seconds() <= FAILURE_WINDOW_SECONDS:
                recent.append(value)
        return recent

    def _record_failure(self, conn, now: datetime, message: str) -> int:
        failures = self._failures(conn, now) + [to_iso(now) or ""]
        db.set_json_setting(conn, "_engine_failures", failures)
        db.set_setting(conn, "engine_last_error", message[:500])
        return len(failures)

    def _clear_failures(self, conn) -> None:
        db.delete_setting(conn, "_engine_failures")
        db.set_setting(conn, "engine_last_error", "")

    def active_since(self, conn, now: datetime, unit_active: bool) -> datetime | None:
        stored = parse_iso(db.get_setting(conn, "_engine_active_since"))
        if unit_active:
            if stored is None:
                db.set_setting(conn, "_engine_active_since", to_iso(now) or "")
                return now
            return stored
        if stored is not None:
            db.delete_setting(conn, "_engine_active_since")
        return None

    # -- status ------------------------------------------------------------
    def status(self, conn, now: datetime, stats: dict | None = None) -> dict:
        stats = stats if stats is not None else (
            self.icecast.stats() if self.icecast is not None else {"available": False, "mount_present": False}
        )
        unit_active, unit_state = self.unit_info(now)
        active_since = self.active_since(conn, now, unit_active)
        desired = self.desired(conn)
        state_row = conn.execute("SELECT track_id FROM playback_state WHERE singleton = 1").fetchone()
        has_current = bool(state_row and state_row["track_id"])
        has_library = db.library_totals(conn)["count"] > 0
        failures = len(self._failures(conn, now))
        state = compute_state(
            desired=desired,
            unit_active=unit_active,
            unit_state=unit_state,
            mount_present=bool(stats.get("mount_present")),
            has_current=has_current,
            active_since=active_since,
            now=now,
            recent_failures=failures,
            has_library=has_library,
        )
        conn.commit()
        return {
            "state": state,
            "label": STATE_LABELS.get(state, state),
            "desired": desired,
            "unit": UNIT_LIQUIDSOAP,
            "unit_active": unit_active,
            "unit_state": unit_state,
            "mount_present": bool(stats.get("mount_present")),
            "stats_available": bool(stats.get("available")),
            "listeners": stats.get("listeners"),
            "bitrate": stats.get("bitrate"),
            "has_current": has_current,
            "since": to_iso(active_since),
            "last_start": db.get_setting(conn, "engine_last_start") or None,
            "last_error": db.get_setting(conn, "engine_last_error") or None,
        }

    # -- commands ----------------------------------------------------------
    def _start_unit(self, conn, now: datetime, raise_on_error: bool) -> bool:
        ok, output, _code = self._systemctl("start")
        self.invalidate_unit_cache(UNIT_LIQUIDSOAP)
        if ok:
            db.set_setting(conn, "engine_last_start", to_iso(now) or "")
            self._clear_failures(conn)
            db.log_event(conn, "info", "station", "Эфир запущен", None)
            conn.commit()
            return True
        count = self._record_failure(conn, now, output or "Не удалось запустить движок")
        db.log_event(conn, "error", "station", "Не удалось запустить движок", {"output": output})
        conn.commit()
        if raise_on_error:
            raise ApiError(
                502,
                "engine_start_failed",
                "Не удалось запустить движок. Проверьте службу и журнал.",
                extra={"attempts": count},
            )
        return False

    def start(self, conn, now: datetime | None = None) -> dict:
        now = now or self.clock.now()
        self._set_desired(conn, "playing")
        conn.commit()
        queue_engine.ensure_queue(conn, self.settings, now)
        library = db.library_totals(conn)["count"]
        if library == 0:
            db.log_event(
                conn, "warning", "station", "Эфир включён, но в медиатеке нет музыки", None
            )
            conn.commit()
            return self.status(conn, now)
        self._start_unit(conn, now, raise_on_error=True)
        if self.engine is not None:
            queue_engine.sync_engine(conn, self.settings, self.engine)
        return self.status(conn, now)

    def stop(self, conn, now: datetime | None = None) -> dict:
        now = now or self.clock.now()
        ok, output, _code = self._systemctl("stop")
        self.invalidate_unit_cache(UNIT_LIQUIDSOAP)
        if not ok:
            db.set_setting(conn, "engine_last_error", (output or "Не удалось остановить движок")[:500])
            db.log_event(conn, "error", "station", "Не удалось остановить движок", {"output": output})
            conn.commit()
            raise ApiError(
                502,
                "engine_stop_failed",
                "Не удалось остановить движок. Проверьте службу и журнал.",
            )
        self._set_desired(conn, "stopped")
        db.delete_setting(conn, "_engine_active_since")
        state = conn.execute(
            "SELECT position_id FROM playback_state WHERE singleton = 1"
        ).fetchone()
        if state is not None and state["position_id"]:
            conn.execute(
                "UPDATE history SET ended_at = ? WHERE position_id = ? AND ended_at IS NULL",
                (to_iso(now), state["position_id"]),
            )
            conn.execute(
                "DELETE FROM queue_positions WHERE id = ?", (state["position_id"],)
            )
            remove_queue_link(self.settings, str(state["position_id"]))
        conn.execute("DELETE FROM playback_state WHERE singleton = 1")
        media.delete_pending_after_change(conn, self.settings)
        db.log_event(conn, "info", "station", "Эфир остановлен владельцем", None)
        conn.commit()
        return self.status(conn, now)

    def restart(self, conn, now: datetime | None = None) -> dict:
        now = now or self.clock.now()
        if self.desired(conn) != "playing":
            raise ApiError(
                409,
                "station_stopped",
                "Сначала включите эфир: перезапуск сохраняет желаемое состояние.",
            )
        queue_engine.ensure_queue(conn, self.settings, now)
        ok, output, _code = self._systemctl("restart")
        self.invalidate_unit_cache(UNIT_LIQUIDSOAP)
        if not ok:
            self._record_failure(conn, now, output or "Не удалось перезапустить движок")
            db.log_event(
                conn, "error", "station", "Не удалось перезапустить движок", {"output": output}
            )
            conn.commit()
            raise ApiError(
                502,
                "engine_restart_failed",
                "Не удалось перезапустить движок. Проверьте службу и журнал.",
            )
        if self.engine is not None:
            queue_engine.sync_engine(conn, self.settings, self.engine)
        db.set_setting(conn, "engine_last_start", to_iso(now) or "")
        self._clear_failures(conn)
        db.log_event(conn, "info", "station", "Движок перезапущен", None)
        conn.commit()
        return self.status(conn, now)

    def restart_for_settings(self, conn, now: datetime) -> bool:
        if self.desired(conn) != "playing":
            return False
        ok, output, _code = self._systemctl("restart")
        self.invalidate_unit_cache(UNIT_LIQUIDSOAP)
        if ok:
            db.set_setting(conn, "engine_last_start", to_iso(now) or "")
            self._clear_failures(conn)
            db.log_event(conn, "info", "station", "Движок перезапущен после смены настроек", None)
        else:
            self._record_failure(conn, now, output or "Не удалось перезапустить движок")
            db.log_event(
                conn, "error", "station", "Не удалось перезапустить движок после смены настроек", None
            )
        conn.commit()
        return ok

    def skip(self, conn) -> None:
        if self.engine is None or not self.engine.skip():
            raise ApiError(503, "engine_unavailable", "Движок недоступен. Пропуск не выполнен.")

    def auto_start_if_needed(self, conn, now: datetime, status: dict) -> bool:
        if status.get("desired") != "playing" or status.get("unit_active"):
            return False
        if db.library_totals(conn)["count"] == 0:
            return False
        if len(self._failures(conn, now)) >= FAILURE_LIMIT:
            # Stop the restart loop: the owner sees `error` and must intervene.
            return False
        last = parse_iso(db.get_setting(conn, "_last_auto_start"))
        if last is not None and (now - last).total_seconds() < AUTO_START_COOLDOWN:
            return False
        db.set_setting(conn, "_last_auto_start", to_iso(now) or "")
        conn.commit()
        queue_engine.ensure_queue(conn, self.settings, now)
        started = self._start_unit(conn, now, raise_on_error=False)
        if started and self.engine is not None:
            queue_engine.sync_engine(conn, self.settings, self.engine)
        return started

    def services(self, conn) -> list[dict]:
        units = [
            (UNIT_API, "Backend", True),
            (UNIT_LIQUIDSOAP, "Liquidsoap", False),
            (UNIT_ICECAST, "Icecast", False),
        ]
        result = []
        for unit, label, always in units:
            if always:
                state = "active"
            else:
                state = self.unit_state_of(unit)
            result.append({"unit": unit, "label": label, "state": state})
        return result
