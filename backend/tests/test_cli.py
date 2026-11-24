"""CLI-01: init-db, set-password, backup, check-config (deploy-facing commands)."""

from __future__ import annotations

import io
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app import auth, cli, db
from app.config import Settings


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    paths = {
        "VD_DB_PATH": tmp_path / "state" / "app.db",
        "VD_MUSIC_DIR": tmp_path / "music",
        "VD_COVER_DIR": tmp_path / "covers",
        "VD_ENGINE_DIR": tmp_path / "run",
        "VD_ENCODER_FILE": tmp_path / "etc" / "encoder.liq",
        "VD_STATION_FILE": tmp_path / "etc" / "station.liq",
        "VD_INTERNAL_TOKEN": "cli-internal-token",
        "VD_PUBLIC_BASE_URL": "https://radio.test",
        "VD_ICECAST_ADMIN_PASSWORD": "icecast-secret",
        "VD_MIN_FREE_BYTES": "0",
        "VD_SCHEDULER_ENABLED": "0",
    }
    for name, value in paths.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.setenv("VD_ENGINE_QUEUE_FILE", str(tmp_path / "run" / "engine-queue.txt"))
    monkeypatch.setenv("VD_ENGINE_SOCKET", str(tmp_path / "run" / "liquidsoap.sock"))
    monkeypatch.setenv("VD_QUEUE_LINK_DIR", str(tmp_path / "run" / "queue"))
    return Settings.from_env()


def test_init_db_creates_schema(cli_env, capsys):
    assert cli.main(["init-db"]) == 0
    assert cli_env.db_path.exists()
    conn = sqlite3.connect(str(cli_env.db_path))
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        settings = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    finally:
        conn.close()
    assert version == db.SCHEMA_VERSION
    assert {"tracks", "playlists", "queue_positions", "schedule_rules", "settings"} <= tables
    assert settings["station_name"] == "Vanilla Dream Radio"
    assert "готов" in capsys.readouterr().out.lower()
    # Idempotent second run must not fail or reset anything.
    assert cli.main(["init-db"]) == 0


def test_set_password_reads_stdin_and_hides_it(cli_env, monkeypatch, capsys):
    password = "super-secret-42"
    monkeypatch.setattr(sys, "stdin", io.StringIO(password + "\n"))
    assert cli.main(["init-db"]) == 0
    assert cli.main(["set-password"]) == 0
    output = capsys.readouterr().out
    assert password not in output
    conn = db.open_db(cli_env)
    try:
        row = conn.execute("SELECT password_hash, salt FROM admin WHERE id = 1").fetchone()
        assert row is not None
        assert auth.verify_password(password, row["password_hash"], row["salt"])
        assert db.get_setting(conn, "admin_password_set") == "1"
    finally:
        conn.close()


def test_set_password_rejects_empty_stdin(cli_env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    assert cli.main(["set-password"]) == 2
    assert "Пароль не получен" in capsys.readouterr().err


def test_password_is_set_reports_machine_token(cli_env, monkeypatch, capsys):
    assert cli.main(["password-is-set"]) == 0
    assert capsys.readouterr().out == "unset\n"
    assert cli.main(["init-db"]) == 0
    capsys.readouterr()
    assert cli.main(["password-is-set"]) == 0
    assert capsys.readouterr().out == "unset\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO("password-123\n"))
    assert cli.main(["set-password"]) == 0
    capsys.readouterr()
    assert cli.main(["password-is-set"]) == 0
    assert capsys.readouterr().out == "set\n"


def test_backup_copies_database_and_prunes(cli_env, capsys):
    assert cli.main(["init-db"]) == 0
    conn = db.open_db(cli_env)
    try:
        conn.execute(
            "INSERT INTO tracks(id, sha256, path, ext, title, created_at, updated_at) "
            "VALUES('t1', 'hash', 'hash.mp3', '.mp3', 'Backup track', '2026-01-01T00:00:00Z', "
            "'2026-01-01T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()
    destination = cli_env.db_path.parent / "backups"
    destination.mkdir(parents=True, exist_ok=True)
    for index in range(8):
        stale = destination / f"app-2020010{index}T000000Z.db"
        stale.write_bytes(b"old")
        os.utime(stale, (1000 + index, 1000 + index))

    assert cli.main(["backup", "--dest", str(destination)]) == 0
    backups = sorted(destination.glob("app-*.db"))
    assert len(backups) == 7
    newest = max(backups, key=lambda path: path.stat().st_mtime)
    copy = sqlite3.connect(str(newest))
    try:
        title = copy.execute("SELECT title FROM tracks WHERE id = 't1'").fetchone()[0]
        integrity = copy.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        copy.close()
    assert title == "Backup track"
    assert integrity == "ok"
    assert not (destination / "app-20200100T000000Z.db").exists()
    assert "Резервная копия создана" in capsys.readouterr().out


def test_check_config_reports_missing_state(cli_env, capsys):
    assert cli.main(["check-config"]) == 1
    err = capsys.readouterr().err
    assert "не инициализирована" in err


def test_check_config_ok_after_init_and_password(cli_env, monkeypatch, capsys):
    assert cli.main(["init-db"]) == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO("password-123\n"))
    assert cli.main(["set-password"]) == 0
    capsys.readouterr()
    assert cli.main(["check-config"]) == 0
    assert "Конфигурация в порядке" in capsys.readouterr().out


def test_check_config_requires_internal_token(cli_env, monkeypatch, capsys):
    assert cli.main(["init-db"]) == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO("password-123\n"))
    assert cli.main(["set-password"]) == 0
    monkeypatch.delenv("VD_INTERNAL_TOKEN")
    assert cli.main(["check-config"]) == 1
    assert "VD_INTERNAL_TOKEN" in capsys.readouterr().err


def test_cli_module_is_runnable(cli_env):
    env = {**os.environ}
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "init-db"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert cli_env.db_path.exists()
