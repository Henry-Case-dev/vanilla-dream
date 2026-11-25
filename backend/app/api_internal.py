"""Internal engine callback endpoints (token protected, localhost only)."""

from __future__ import annotations

import asyncio
import hmac
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request

from . import queue_engine
from .auth import get_conn
from .util import ApiError, epoch_to_dt

router = APIRouter(prefix="/api/internal", tags=["internal"])

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _require_token(request: Request) -> None:
    expected = request.app.state.settings.internal_token
    provided = request.headers.get("X-Internal-Token") or ""
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        raise ApiError(403, "forbidden", "Неверный внутренний токен.")


@router.post("/engine/track-start")
async def engine_track_start(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _require_token(request)
    form = await request.form()
    settings = request.app.state.settings
    file_value = form.get("f")
    position_id = ""
    if file_value:
        # Accept either the path alone or a full `<id>\t<path>` engine line.
        link = Path(str(file_value).split("\t")[-1])
        try:
            in_link_dir = link.is_absolute() and link.parent.resolve() == Path(
                settings.queue_link_dir
            ).resolve()
        except OSError:
            in_link_dir = False
        if not in_link_dir:
            raise ApiError(422, "invalid_engine_path", "Недопустимый путь очереди движка.")
        position_id = link.stem
    if not position_id:
        # Defensive legacy support for the form field used before symlinks.
        position_id = str(form.get("vd") or "")
    now = request.app.state.clock.now()
    started_at = epoch_to_dt(form.get("ts"))
    if started_at is None:
        raise ApiError(422, "invalid_timestamp", "Не удалось прочитать время начала трека.")
    result = await asyncio.to_thread(
        _handle_with_retry, conn, settings, position_id, started_at, now
    )
    request.app.state.scheduler.wake()
    return {"status": result.get("status", "ok"), "track_id": result.get("track_id")}


def _handle_with_retry(conn, settings, position_id, started_at, now):
    result = None
    last_error: Exception | None = None
    for delay in (0.0, 0.05, 0.15, 0.4):
        if delay:
            time.sleep(delay)
        try:
            result = queue_engine.handle_track_start(
                conn, settings, position_id, started_at, now
            )
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            last_error = exc
    if result is None:
        raise ApiError(
            503,
            "engine_callback_busy",
            "Не удалось сохранить событие движка. Повторите отправку.",
        ) from last_error
    return result

@router.get("/health")
def internal_health(request: Request):
    host = request.client.host if request.client else ""
    headers = getattr(request, "headers", {})
    if headers.get("X-Forwarded-For"):
        raise ApiError(404, "not_found", "Не найдено.")
    if host not in LOCAL_HOSTS:
        raise ApiError(404, "not_found", "Не найдено.")
    return {"status": "ok"}
