"""FastAPI application factory: lifespan, scheduler task, error handlers."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import api_admin, api_internal, api_public, auth, db, queue_engine
from .config import Settings
from .icecast import IcecastClient
from .liquidsoap import LiquidsoapClient
from .scheduler import SchedulerLoop
from .station import StationController
from .util import ApiError, Clock, to_iso

LOGGER = logging.getLogger("vdradio")


def _error(status: int, code: str, message: str, fields: dict | None = None) -> JSONResponse:
    detail: dict = {"code": code, "message": message}
    if fields:
        detail["fields"] = fields
    return JSONResponse({"detail": detail}, status_code=status)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    app.state.shutdown_event.clear()
    settings.ensure_directories()
    db.init_db(settings)

    conn = db.open_db(settings)
    try:
        base = db.get_setting(conn, "public_base_url")
        if base and base.rstrip("/") != settings.public_base_url:
            settings = dataclasses.replace(settings, public_base_url=base.rstrip("/"))
            app.state.settings = settings
            app.state.station.settings = settings
            app.state.scheduler.settings = settings
        # Positions handed to the engine before restart are unknown to a fresh
        # Liquidsoap process; keep them as editable pending items.
        conn.execute("UPDATE queue_positions SET state = 'pending' WHERE state = 'prepared'")
        playback = conn.execute("SELECT * FROM playback_state WHERE singleton = 1").fetchone()
        if playback is not None and playback["position_id"]:
            conn.execute(
                "UPDATE queue_positions SET state = 'current' WHERE id = ?",
                (playback["position_id"],),
            )
        auth.purge_expired_sessions(conn, app.state.clock.now())
        queue_engine.deactivate_expired_overrides(conn, app.state.clock.now())
        conn.commit()
        now = app.state.clock.now()
        # Cross-check the persisted current track against the live stream title
        # when Icecast is reachable. Mismatches are logged, never guessed away.
        try:
            stats = app.state.icecast.stats() if app.state.icecast is not None else {}
        except Exception:  # noqa: BLE001 - reconciliation is best effort
            stats = {}
        if playback is not None and playback["track_id"] and stats.get("mount_present"):
            track = conn.execute(
                "SELECT title, artist FROM tracks WHERE id = ?", (playback["track_id"],)
            ).fetchone()
            stream_title = str(stats.get("title") or "")
            if track is not None and stream_title:
                expected = (
                    f"{track['artist']} - {track['title']}"
                    if track["artist"]
                    else str(track["title"] or "")
                )
                if expected.lower() not in stream_title.lower() and str(
                    track["title"] or ""
                ).lower() not in stream_title.lower():
                    db.log_event(
                        conn,
                        "warning",
                        "engine",
                        "Название в потоке не совпадает с сохранённым текущим треком",
                        {"expected": expected, "stream": stream_title},
                        at=to_iso(now),
                    )
                    conn.commit()
        queue_engine.ensure_queue(conn, settings, now)
        if db.get_setting(conn, "desired_state") == "playing" and db.library_totals(conn)["count"]:
            db.delete_setting(conn, "_last_auto_start")
            conn.commit()
            try:
                status = app.state.station.status(conn, now)
                app.state.station.auto_start_if_needed(conn, now, status)
            except Exception:  # noqa: BLE001 - engine resume must not stop the API
                LOGGER.exception("failed to resume desired state")
    finally:
        conn.close()

    scheduler: SchedulerLoop = app.state.scheduler
    if settings.scheduler_enabled:
        scheduler.task = asyncio.create_task(scheduler.run(), name="vd-scheduler")
    try:
        yield
    finally:
        app.state.shutdown_event.set()
        scheduler.stop()
        if scheduler.task is not None:
            try:
                await asyncio.wait_for(scheduler.task, timeout=5)
            except (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError):
                scheduler.task.cancel()
        # The audio engine keeps playing on shutdown by design.


def create_app(
    settings: Settings | None = None,
    clock: Clock | None = None,
    station: StationController | None = None,
    engine=None,
    icecast=None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    clock = clock or Clock()
    engine = engine if engine is not None else LiquidsoapClient(settings.engine_socket)
    icecast = icecast if icecast is not None else IcecastClient(settings)
    station = station or StationController(settings, clock, engine=engine, icecast=icecast)

    app = FastAPI(title="Vanilla Dream Radio", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.clock = clock
    app.state.engine = engine
    app.state.icecast = icecast
    app.state.station = station
    app.state.stats = None
    app.state.station_status = None
    app.state.next_schedule_change = None
    app.state.login_attempts = {}
    app.state.now_playing_changed = asyncio.Event()
    app.state.shutdown_event = asyncio.Event()
    app.state.sync_state = {}
    app.state.scheduler = SchedulerLoop(
        app.state, settings, clock, station, engine, icecast
    )

    app.include_router(api_public.router)
    app.include_router(auth.router)
    app.include_router(api_admin.router)
    app.include_router(api_internal.router)

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError):
        return JSONResponse(exc.payload(), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError):
        fields = {}
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", []) if part != "body")
            fields[location or "body"] = "Некорректное значение."
        return _error(422, "validation_error", "Проверьте параметры запроса.", fields)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException):
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        messages = {
            400: "Некорректный запрос.",
            401: "Требуется авторизация.",
            403: "Доступ запрещён.",
            404: "Не найдено.",
            405: "Метод не поддерживается.",
            409: "Конфликт запроса.",
            413: "Слишком большой запрос.",
            422: "Проверьте параметры запроса.",
            500: "Внутренняя ошибка сервера.",
        }
        message = messages.get(exc.status_code, "Ошибка запроса.")
        return _error(exc.status_code, "http_error", message)

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception):
        LOGGER.exception("unhandled API error: %s", exc)
        return _error(500, "internal_error", "Внутренняя ошибка сервера.")

    return app


app = create_app()
