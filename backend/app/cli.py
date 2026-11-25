"""Command line helpers used by the deploy scripts.

    python -m app.cli init-db
    python -m app.cli set-password        # reads the password from stdin
    python -m app.cli backup --dest DIR
    python -m app.cli check-config
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import db
from .auth import set_admin_password
from .config import SUPPORTED_BITRATES, Settings
from .util import Clock

BACKUP_PREFIX = "app-"
BACKUP_KEEP = 7


def _settings() -> Settings:
    return Settings.from_env()


def cmd_init_db(_args) -> int:
    settings = _settings()
    db.init_db(settings)
    print(f"Схема базы данных готова: {settings.db_path}")
    return 0


def cmd_set_password(_args) -> int:
    settings = _settings()
    db.init_db(settings)
    password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        print("Пароль не получен (ожидалась строка в stdin).", file=sys.stderr)
        return 2
    conn = db.open_db(settings)
    try:
        set_admin_password(conn, password, Clock().now())
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - surfaced as CLI text, never a traceback
        print(f"Не удалось установить пароль: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print("Пароль администратора обновлён.")
    return 0


def cmd_password_is_set(_args) -> int:
    settings = _settings()
    if not settings.db_path.exists():
        print("unset")
        return 0
    try:
        conn = db.open_db(settings)
    except sqlite3.Error:
        print("unset")
        return 0
    try:
        row = conn.execute("SELECT password_hash FROM admin WHERE id = 1").fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    print("set" if row is not None and row["password_hash"] else "unset")
    return 0


def _prune_backups(directory: Path, keep: int = BACKUP_KEEP) -> list[Path]:
    candidates = sorted(
        (entry for entry in directory.glob(f"{BACKUP_PREFIX}*.db") if entry.is_file()),
        key=lambda entry: entry.stat().st_mtime,
        reverse=True,
    )
    removed = []
    for entry in candidates[keep:]:
        try:
            entry.unlink()
            removed.append(entry)
        except OSError:
            continue
    return removed


def cmd_backup(args) -> int:
    settings = _settings()
    if not settings.db_path.exists():
        print(f"База данных не найдена: {settings.db_path}", file=sys.stderr)
        return 1
    destination = Path(args.dest).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = destination / f"{BACKUP_PREFIX}{stamp}.db"
    source = sqlite3.connect(str(settings.db_path), timeout=10)
    try:
        backup = sqlite3.connect(str(target))
        try:
            source.backup(backup)
        finally:
            backup.close()
    finally:
        source.close()
    removed = _prune_backups(destination)
    print(f"Резервная копия создана: {target}")
    if removed:
        print(f"Удалены старые копии: {len(removed)}")
    return 0


def _check_writable(path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return os.access(path, os.W_OK)
        return os.access(path.parent, os.W_OK)
    except OSError:
        return False


def cmd_check_config(_args) -> int:
    settings = _settings()
    errors: list[str] = []
    warnings: list[str] = []

    if not settings.internal_token:
        errors.append("VD_INTERNAL_TOKEN не задан.")
    if not settings.icecast_admin_password:
        warnings.append("VD_ICECAST_ADMIN_PASSWORD не задан.")
    if not settings.public_base_url:
        errors.append("VD_PUBLIC_BASE_URL не задан.")
    else:
        parsed = urlparse(settings.public_base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            errors.append("VD_PUBLIC_BASE_URL должен быть полным http(s) адресом.")

    for label, directory in (
        ("музыки", settings.music_dir),
        ("обложек", settings.cover_dir),
        ("обложек плейлистов", settings.playlist_cover_dir),
        ("состояния движка", settings.engine_dir),
        ("ссылок очереди", settings.queue_link_dir),
        ("базы данных", settings.db_path.parent),
    ):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            errors.append(f"Каталог {label} недоступен для записи: {directory}")
            continue
        if not os.access(directory, os.W_OK):
            errors.append(f"Каталог {label} недоступен для записи: {directory}")

    for label, path in (
        ("encoder.liq", settings.encoder_file),
        ("station.liq", settings.station_file),
        ("engine-queue.txt", settings.engine_queue_file),
    ):
        if not _check_writable(path):
            errors.append(f"Нет прав на запись файла {label}: {path}")

    if not settings.db_path.exists():
        errors.append("База данных не инициализирована: выполните `python -m app.cli init-db`.")
    else:
        conn = db.open_db(settings)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if int(version or 0) < db.SCHEMA_VERSION:
                errors.append("Схема базы данных устарела: выполните `python -m app.cli init-db`.")
            if db.get_setting(conn, "admin_password_set", "0") != "1":
                errors.append("Пароль администратора не задан: выполните `python -m app.cli set-password`.")
            timezone_name = db.get_setting(conn, "timezone")
            from .scheduler import valid_timezone

            if not valid_timezone(timezone_name):
                errors.append(f"Некорректный часовой пояс в настройках: {timezone_name}")
            bitrate = db.get_int_setting(conn, "bitrate", 128)
            if bitrate not in SUPPORTED_BITRATES:
                errors.append(f"Недопустимый битрейт в настройках: {bitrate}")
        finally:
            conn.close()

    for warning in warnings:
        print(f"Внимание: {warning}", file=sys.stderr)
    if errors:
        for error in errors:
            print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    print("Конфигурация в порядке.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Vanilla Dream Radio CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-db", help="создать или обновить схему базы данных")
    init_parser.set_defaults(handler=cmd_init_db)

    password_parser = subparsers.add_parser(
        "set-password", help="прочитать новый пароль администратора из stdin"
    )
    password_parser.set_defaults(handler=cmd_set_password)

    backup_parser = subparsers.add_parser("backup", help="создать согласованную копию базы данных")
    backup_parser.add_argument("--dest", required=True, help="каталог для резервных копий")
    backup_parser.set_defaults(handler=cmd_backup)

    check_parser = subparsers.add_parser("check-config", help="проверить окружение и файлы")
    check_parser.set_defaults(handler=cmd_check_config)

    password_state_parser = subparsers.add_parser(
        "password-is-set", help="напечатать set или unset для пароля администратора"
    )
    password_state_parser.set_defaults(handler=cmd_password_is_set)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
