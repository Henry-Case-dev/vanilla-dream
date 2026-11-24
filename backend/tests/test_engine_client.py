"""Liquidsoap command socket protocol: allow-list, END marker, failure handling."""

from __future__ import annotations

import pytest

from app.liquidsoap import LiquidsoapClient


class FakeSocket:
    def __init__(self, replies: bytes) -> None:
        self.replies = replies
        self.sent = b""
        self.connected_to: str | None = None
        self.timeout = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def settimeout(self, value):
        self.timeout = value

    def connect(self, path):
        self.connected_to = path

    def sendall(self, data):
        self.sent += data

    def recv(self, size):
        size = min(size, 27)
        chunk, self.replies = self.replies[:size], self.replies[size:]
        return chunk


def _client(replies: bytes, socket_path="/run/vanilla-dream-radio/liquidsoap.sock"):
    sock = FakeSocket(replies)
    client = LiquidsoapClient(socket_path, timeout=5, socket_factory=lambda: sock)
    return client, sock


def test_sync_reads_reply_until_end():
    client, sock = _client(b"OK\nprepared 2 requests\nEND\nremaining")
    ok, reply = client.command("vd_sync")
    assert ok is True
    assert sock.sent == b"vd_sync\n"
    assert sock.connected_to.endswith("liquidsoap.sock")
    assert reply == "OK\nprepared 2 requests\nEND"
    assert sock.replies == b"remaining"  # reading stopped at the END line


def test_skip_uses_radio_skip():
    client, sock = _client(b"OK\nEND\n")
    assert client.skip() is True
    assert sock.sent == b"radio.skip\n"


def test_error_reply_is_not_success():
    client, _sock = _client(b"ERROR: unknown command\nEND\n")
    ok, reply = client.command("vd_sync")
    assert ok is False
    assert "ERROR" in reply


def test_commands_outside_allow_list_are_rejected(tmp_path):
    client = LiquidsoapClient(tmp_path / "ls.sock")
    with pytest.raises(ValueError):
        client.command("radio.stop")
    with pytest.raises(ValueError):
        client.command("shell rm -rf /")


def test_missing_socket_is_reported_not_faked(tmp_path):
    client = LiquidsoapClient(tmp_path / "missing.sock", timeout=1)
    ok, reply = client.command("vd_sync")
    assert ok is False
    assert reply == ""


def test_connect_error_returns_false():
    def broken():
        raise OSError("socket directory missing")

    client = LiquidsoapClient("/run/nope.sock", socket_factory=broken)
    assert client.command("vd_sync") == (False, "")
