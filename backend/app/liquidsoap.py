"""Minimal client for the Liquidsoap command server (UNIX socket only)."""

from __future__ import annotations

import logging
import socket
from pathlib import Path

LOGGER = logging.getLogger("vdradio.liquidsoap")

ALLOWED_COMMANDS = frozenset({"vd_sync", "vd_skip", "radio.skip"})
SKIP_COMMANDS = frozenset({"vd_skip", "radio.skip"})
END_MARKER = "END"
MAX_REPLY_BYTES = 65536
DONE_REPLIES = frozenset({"DONE", "DONE."})


class LiquidsoapClient:
    def __init__(
        self,
        socket_path: Path | str,
        timeout: float = 5.0,
        socket_factory=None,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self._socket_factory = socket_factory or self._default_socket
        self._logged_unavailable = False

    @staticmethod
    def _default_socket():
        if not hasattr(socket, "AF_UNIX"):
            raise OSError("AF_UNIX sockets are not supported by this platform")
        return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    def command(self, name: str) -> tuple[bool, str]:
        """Send one allow-listed command; return (ok, reply text)."""

        if name not in ALLOWED_COMMANDS:
            raise ValueError(f"command not allowed: {name}")
        try:
            with self._socket_factory() as client:
                client.settimeout(self.timeout)
                client.connect(str(self.socket_path))
                client.sendall((name + "\n").encode("utf-8"))
                chunks = []
                total = 0
                while True:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_REPLY_BYTES:
                        break
                    chunks.append(chunk)
                    text = b"".join(chunks).decode("utf-8", errors="replace")
                    if any(line.strip() == END_MARKER for line in text.splitlines()):
                        break
        except (OSError, ValueError) as exc:
            if not self._logged_unavailable:
                LOGGER.warning("liquidsoap command socket unavailable: %s", exc)
                self._logged_unavailable = True
            return False, ""
        self._logged_unavailable = False
        raw = b"".join(chunks).decode("utf-8", errors="replace")
        lines = []
        for line in raw.splitlines():
            lines.append(line)
            if line.strip() == END_MARKER:
                break
        reply = "\n".join(lines).strip()
        first_line = reply.splitlines()[0].strip().upper() if reply else ""
        ok = first_line.startswith("OK")
        if not ok and name in SKIP_COMMANDS:
            # Liquidsoap's builtin radio.skip answers "Done." while a registered
            # custom command (vd_skip) answers "OK".
            ok = first_line in DONE_REPLIES
        return ok, reply

    def sync(self) -> bool:
        ok, _ = self.command("vd_sync")
        return ok

    def skip(self) -> bool:
        ok, _ = self.command("vd_skip")
        if ok:
            return True
        ok, _ = self.command("radio.skip")
        return ok
