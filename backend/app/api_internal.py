"""Internal engine callback endpoints (token protected, localhost only)."""

from __future__ import annotations

import asyncio
import hmac
import sqlite3
from datetime import timedelta
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
        link = Path(str(file_value))
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
    if started_at is None or started_at > now + timedelta(seconds=5):
        started_at = now
    if started_at < now - timedelta(hours=1):
        started_at = now
    result = await asyncio.to_thread(
        queue_engine.handle_track_start, conn, settings, position_id, started_at, now
    )
    request.app.state.scheduler.wake()
    return {"ok": True, "duplicate": bool(result.get("duplicate"))}

@router.get("/health")
def internal_health(request: Request):
    host = request.client.host if request.client else ""
    headers = getattr(request, "headers", {})
    if headers.get("X-Forwarded-For"):
        raise ApiError(404, "not_found", "Не найдено.")
    if host not in LOCAL_HOSTS:
        raise ApiError(404, "not_found", "Не найдено.")
    return {"status": "ok"}
