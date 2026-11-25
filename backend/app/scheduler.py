"""Station schedule: station timezone, active rules, boundaries, conflicts."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db

DAY_NAMES = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")


def station_zone(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        try:
            return ZoneInfo("UTC")
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return timezone.utc


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False


def station_tz(conn: sqlite3.Connection) -> ZoneInfo:
    return station_zone(db.get_setting(conn, "timezone", "Asia/Magadan") or "Asia/Magadan")


def local_now(now_utc: datetime, zone: ZoneInfo) -> datetime:
    return now_utc.astimezone(zone)


def parse_days(raw: str | list | None) -> list[int]:
    if isinstance(raw, list):
        values = raw
    else:
        try:
            values = json.loads(raw or "[]")
        except (TypeError, ValueError):
            values = []
    result = []
    for value in values:
        try:
            day = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6 and day not in result:
            result.append(day)
    return sorted(result)


def encode_days(days: list[int]) -> str:
    return json.dumps(sorted({int(day) for day in days}), ensure_ascii=False)


def _overnight(rule: sqlite3.Row) -> bool:
    return int(rule["end_min"]) <= int(rule["start_min"])


def rule_matches(rule: sqlite3.Row, local_dt: datetime, days: list[int] | None = None) -> bool:
    days = parse_days(rule["days"]) if days is None else days
    if not days:
        return False
    minute = local_dt.hour * 60 + local_dt.minute
    weekday = local_dt.weekday()
    previous = (weekday - 1) % 7
    start = int(rule["start_min"])
    end = int(rule["end_min"])
    if _overnight(rule):
        if weekday in days and minute >= start:
            return True
        if previous in days and minute < end:
            return True
        return False
    return weekday in days and start <= minute < end


def active_rule(conn: sqlite3.Connection, now_utc: datetime) -> sqlite3.Row | None:
    if db.get_setting(conn, "schedule_enabled", "1") != "1":
        return None
    zone = station_tz(conn)
    local = local_now(now_utc, zone)
    rows = conn.execute(
        "SELECT * FROM schedule_rules WHERE enabled = 1 ORDER BY priority DESC, start_min ASC, id ASC"
    ).fetchall()
    for row in rows:
        if rule_matches(row, local):
            return row
    return None


def active_rules(conn: sqlite3.Connection, now_utc: datetime) -> list[sqlite3.Row]:
    """All active rules in deterministic priority order (first = winner)."""

    if db.get_setting(conn, "schedule_enabled", "1") != "1":
        return []
    local = local_now(now_utc, station_tz(conn))
    rows = conn.execute(
        "SELECT * FROM schedule_rules WHERE enabled = 1 ORDER BY priority DESC, start_min ASC, id ASC"
    ).fetchall()
    return [row for row in rows if rule_matches(row, local)]


def _rule_occurrences(rule: sqlite3.Row, zone: ZoneInfo, start_date: date, days_ahead: int = 10):
    """Yield (utc_dt, event, name) for rule starts and ends."""

    days = parse_days(rule["days"])
    start_min = int(rule["start_min"])
    end_min = int(rule["end_min"])
    overnight = _overnight(rule)
    for offset in range(days_ahead):
        current = start_date + timedelta(days=offset)
        if current.weekday() in days:
            start_local = datetime.combine(current, time(0, 0), tzinfo=zone) + timedelta(minutes=start_min)
            yield start_local.astimezone(timezone.utc), "start", rule["name"]
            end_date = current + timedelta(days=1) if overnight else current
            end_local = datetime.combine(end_date, time(0, 0), tzinfo=zone) + timedelta(minutes=end_min)
            yield end_local.astimezone(timezone.utc), "end", rule["name"]


def next_schedule_change(
    conn: sqlite3.Connection, now_utc: datetime
) -> tuple[datetime | None, str | None]:
    if db.get_setting(conn, "schedule_enabled", "1") != "1":
        return None, None
    zone = station_tz(conn)
    today = local_now(now_utc, zone).date()
    best: tuple[datetime, str] | None = None
    rows = conn.execute("SELECT * FROM schedule_rules WHERE enabled = 1").fetchall()
    for rule in rows:
        for moment, _event, name in _rule_occurrences(rule, zone, today):
            if moment <= now_utc:
                continue
            if best is None or moment < best[0]:
                best = (moment, name or "Расписание")
    if best is None:
        return None, None
    return best[0], best[1]


def upcoming_changes(
    conn: sqlite3.Connection, now_utc: datetime, limit: int = 8
) -> list[dict]:
    if db.get_setting(conn, "schedule_enabled", "1") != "1":
        return []
    zone = station_tz(conn)
    today = local_now(now_utc, zone).date()
    changes = []
    for rule in conn.execute("SELECT * FROM schedule_rules WHERE enabled = 1").fetchall():
        for moment, event, name in _rule_occurrences(rule, zone, today):
            if moment <= now_utc:
                continue
            changes.append({"at": moment, "event": event, "rule_name": name or "Расписание"})
    changes.sort(key=lambda item: item["at"])
    return changes[:limit]


def _segments(rule: sqlite3.Row) -> list[tuple[int, int]]:
    start = int(rule["start_min"])
    end = int(rule["end_min"])
    if _overnight(rule):
        return [(start, 24 * 60), (0, end)]
    return [(start, end)]


def conflicts(conn: sqlite3.Connection) -> list[dict]:
    rules = conn.execute(
        "SELECT * FROM schedule_rules WHERE enabled = 1 ORDER BY priority DESC, id ASC"
    ).fetchall()
    found = []
    for index, first in enumerate(rules):
        first_days = set(parse_days(first["days"]))
        if not first_days:
            continue
        for second in rules[index + 1 :]:
            second_days = set(parse_days(second["days"]))
            if not second_days:
                continue
            overlapping_days = sorted(first_days & second_days)
            # An overnight rule starting on day X also touches day X+1.
            for day in list(overlapping_days):
                if _overnight(first) and (day + 1) % 7 in second_days:
                    overlapping_days.append((day + 1) % 7)
                if _overnight(second) and (day + 1) % 7 in first_days:
                    overlapping_days.append((day + 1) % 7)
            overlapping_days = sorted(set(overlapping_days))
            if not overlapping_days:
                continue
            overlap_minutes = []
            for start_a, end_a in _segments(first):
                for start_b, end_b in _segments(second):
                    low = max(start_a, start_b)
                    high = min(end_a, end_b)
                    if low < high:
                        overlap_minutes.append((low, high))
            if overlap_minutes:
                found.append(
                    {
                        "a_id": first["id"],
                        "a_name": first["name"],
                        "b_id": second["id"],
                        "b_name": second["name"],
                        "days": overlapping_days,
                        "days_label": ", ".join(DAY_NAMES[day] for day in overlapping_days),
                        "priority_a": int(first["priority"]),
                        "priority_b": int(second["priority"]),
                        "winner_id": first["id"],
                        "message": (
                            f"«{first['name']}» и «{second['name']}» пересекаются. "
                            f"Приоритет выше у «{first['name']}»."
                        ),
                    }
                )
    return found


# ---------------------------------------------------------------------------
# Periodic loop (one asyncio task, owned by the app lifespan)
# ---------------------------------------------------------------------------


class SchedulerLoop:
    """Runs one tick every ``VD_SCHEDULER_TICK`` seconds in a worker thread."""

    def __init__(self, app_state, settings, clock, station, engine, icecast):
        import asyncio

        self.app_state = app_state
        self.settings = settings
        self.clock = clock
        self.station = station
        self.engine = engine
        self.icecast = icecast
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._sync_state: dict = getattr(app_state, "sync_state", {})
        self.task = None

    def wake(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        import asyncio
        import logging

        logger = logging.getLogger("vdradio.scheduler")
        self._stop.clear()
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.tick)
            except Exception:  # noqa: BLE001 - the loop must survive a bad tick
                logger.exception("scheduler tick failed")
            self.app_state.now_playing_changed.set()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.settings.scheduler_tick)
            except (asyncio.TimeoutError, TimeoutError):
                pass
            self._wake.clear()

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> dict:
        from . import db, queue_engine
        from . import scheduler as schedule_module
        from .auth import purge_expired_sessions
        from .util import to_iso

        conn = db.open_db(self.settings)
        try:
            now = self.clock.now()
            purge_expired_sessions(conn, now)
            queue_engine.deactivate_expired_overrides(conn, now)
            stats = self.icecast.stats() if self.icecast is not None else {"available": False}
            status = self.station.status(conn, now, stats)
            desired = status.get("desired")
            rule = schedule_module.active_rule(conn, now)
            new_rule_id = str(rule["id"]) if rule is not None else ""
            if new_rule_id != db.get_setting(conn, "_active_rule"):
                db.set_setting(conn, "_active_rule", new_rule_id)
                db.set_setting(conn, "last_schedule_change", to_iso(now) or "")
                if new_rule_id:
                    # A new schedule block is a fresh pass even for repeat=none
                    # sources and even when the same rule returns the next day.
                    queue_engine.invalidate_position_refs(
                        conn, [f"schedule:{new_rule_id}:"]
                    )
                    conn.execute(
                        "DELETE FROM settings WHERE key LIKE ?",
                        (f"_cycle:schedule:{new_rule_id}:%",),
                    )
                if (
                    rule is not None
                    and str(rule["transition"] or "after_track") == "immediate"
                    and desired == "playing"
                    and self.engine is not None
                ):
                    self.engine.skip()
            if desired == "playing":
                queue_engine.ensure_queue(conn, self.settings, now)
                if self.engine is not None:
                    queue_engine.sync_engine(
                        conn,
                        self.settings,
                        self.engine,
                        now=now,
                        sync_state=self._sync_state,
                    )
                if self.station.auto_start_if_needed(conn, now, status):
                    status = self.station.status(conn, now, stats)
            else:
                queue_engine.ensure_queue(conn, self.settings, now)
            # Status is computed after queue/engine work so reason and
            # playback_stalled reflect the actual queue state of this tick.
            status = self.station.status(conn, now, stats)
            change_at, rule_name = schedule_module.next_schedule_change(conn, now)
            self.app_state.station_status = status
            self.app_state.stats = stats
            self.app_state.next_schedule_change = (
                {"at": change_at.isoformat(), "rule_name": rule_name} if change_at else None
            )
            conn.execute(
                "DELETE FROM history WHERE id NOT IN "
                "(SELECT id FROM history ORDER BY id DESC LIMIT 1000)"
            )
            conn.execute(
                "DELETE FROM events WHERE id NOT IN "
                "(SELECT id FROM events ORDER BY id DESC LIMIT 2000)"
            )
            conn.commit()
            return status
        finally:
            conn.close()
