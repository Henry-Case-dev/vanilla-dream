from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import util


def test_atomic_write_replaces_file(tmp_path: Path) -> None:
    target = tmp_path / "encoder.liq"
    target.write_text("encoder_bitrate = 128\n", encoding="utf-8")
    util.atomic_write_text(target, "encoder_bitrate = 160\n")
    assert target.read_text(encoding="utf-8") == "encoder_bitrate = 160\n"


def test_atomic_write_falls_back_when_directory_is_read_only(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "encoder.liq"
    target.write_text("encoder_bitrate = 128\n", encoding="utf-8")
    os.chmod(target, 0o660)
    real_replace = os.replace

    def deny_replace(src, dst):
        raise PermissionError(13, "Permission denied", str(dst))

    monkeypatch.setattr(os, "replace", deny_replace)
    util.atomic_write_text(target, "encoder_bitrate = 192\n")
    monkeypatch.setattr(os, "replace", real_replace)

    assert target.read_text(encoding="utf-8") == "encoder_bitrate = 192\n"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]


def test_atomic_write_falls_back_when_tempfile_is_denied(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "station.liq"
    target.write_text('station_name = "Old"\n', encoding="utf-8")

    def deny_mkstemp(*args, **kwargs):
        raise PermissionError(13, "Permission denied", str(tmp_path))

    monkeypatch.setattr(util.tempfile, "mkstemp", deny_mkstemp)
    util.atomic_write_text(target, 'station_name = "New"\n')

    assert target.read_text(encoding="utf-8") == 'station_name = "New"\n'


def test_atomic_write_creates_missing_file(tmp_path: Path) -> None:
    target = tmp_path / "encoder.liq"
    util.atomic_write_text(target, "encoder_bitrate = 128\n")
    assert target.read_text(encoding="utf-8") == "encoder_bitrate = 128\n"
