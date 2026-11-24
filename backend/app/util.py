"""Small shared helpers: UTC clock, ISO timestamps, API errors, atomic writes."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ApiError(Exception):
    """HTTP error carrying a machine code and a Russian user-facing message."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        fields: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.fields = fields
        self.extra = extra or {}

    def payload(self) -> dict[str, Any]:
        detail: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.fields:
            detail["fields"] = self.fields
        detail.update(self.extra)
        return {"detail": detail}


class Clock:
    """Injectable wall clock (always timezone-aware UTC)."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def epoch_to_dt(value: float | str | None) -> datetime | None:
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def atomic_write_text(path: Path, text: str, default_mode: int = 0o640) -> None:
    """Write text atomically, preserving the existing file mode when possible.

    The production configuration directory is not writable by the service user
    (root:vdradio 0750), so a rename cannot always be used. In that case the
    existing file is rewritten in place: it keeps its owner and mode, and the
    whole content is written in one small write.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = default_mode
    try:
        fd, tmp_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    except PermissionError:
        tmp_name = ""
        fd = -1
    if fd != -1:
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp_name, mode)
            except OSError:
                pass
            os.replace(tmp_name, path)
            return
        except PermissionError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    data = text.encode("utf-8")
    try:
        with open(path, "r+b") as handle:
            handle.seek(0)
            handle.truncate()
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileNotFoundError:
        with open(path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def escape_liq_string(value: str) -> str:
    """Escape a value for a double quoted Liquidsoap string literal."""

    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def seconds_between(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds()


def is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


def safe_join(base: Path, *parts: str) -> Path:
    candidate = base.joinpath(*parts).resolve()
    if not is_relative_to(candidate, base):
        raise ApiError(400, "unsafe_path", "Недопустимый путь к файлу.")
    return candidate


def queue_link_path(settings, position_id: str) -> Path:
    return Path(settings.queue_link_dir) / f"{position_id}.mp3"


def ensure_queue_link(settings, position_id: str, target: Path) -> Path:
    """Create (or refresh) the symlink Liquidsoap reads for one queue position."""

    link_dir = Path(settings.queue_link_dir).resolve()
    link_dir.mkdir(parents=True, exist_ok=True)
    link = link_dir / f"{position_id}.mp3"
    resolved_target = Path(target).resolve()
    try:
        if link.is_symlink():
            try:
                if Path(os.readlink(link)).resolve() == resolved_target:
                    return link
            except OSError:
                pass
        temp = link_dir / f".{position_id}.{os.getpid()}.tmp"
        if temp.is_symlink() or temp.exists():
            temp.unlink()
        os.symlink(str(resolved_target), str(temp))
        os.replace(temp, link)
    except OSError as exc:
        raise OSError(f"cannot create queue link {link}: {exc}") from exc
    return link


def remove_queue_link(settings, position_id: str) -> None:
    link = queue_link_path(settings, position_id)
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
    except OSError:
        pass
