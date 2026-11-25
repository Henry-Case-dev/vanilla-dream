"""Liquidsoap command socket protocol: allow-list, END marker, skip variants."""

from __future__ import annotations

import pytest

from app.liquidsoap import ALLOWED_COMMANDS, LiquidsoapClient


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


def _multi_client(replies_list: list[bytes]):
    sockets = [FakeSocket(replies) for replies in replies_list]
    iterator = iter(sockets)
    client = LiquidsoapClient(
        "/run/vanilla-dream-radio/liquidsoap.sock",
        timeout=5,
        socket_factory=lambda: next(iterator),
    )
    return client, sockets


def test_sync_reads_reply_until_end():
    client, sock = _client(b"OK\nprepared 2 requests\nEND\nremaining")
    ok, reply = client.command("vd_sync")
    assert ok is True
    assert sock.sent == b"vd_sync\n"
    assert sock.connected_to.endswith("liquidsoap.sock")
    assert reply == "OK\nprepared 2 requests\nEND"
    assert sock.replies == b"remaining"  # reading stopped at the END line


def test_sync_report_parses_resolved_and_failed():
    client, sock = _client(b"OK 2 1\nEND\n")
    report = client.sync_report()
    assert report["ok"] is True
    assert report["resolved"] == 2
    assert report["failed"] == 1
    assert sock.sent == b"vd_sync\n"


def test_sync_report_tolerates_plain_ok():
    client, _sock = _client(b"OK\nEND\n")
    report = client.sync_report()
    assert report["ok"] is True
    assert report["resolved"] is None
    assert report["failed"] is None


def test_vd_skip_is_allow_listed_and_used_first():
    assert "vd_skip" in ALLOWED_COMMANDS
    client, sock = _client(b"OK\nEND\n")
    assert client.skip() is True
    assert sock.sent == b"vd_skip\n"


def test_skip_accepts_done_and_done_dot():
    for reply in (b"Done.\nEND\n", b"Done\nEND\n", b"DONE.\nEND\n"):
        client, _sock = _client(reply)
        assert client.skip() is True, reply


def test_skip_falls_back_to_radio_skip_after_unknown_vd_skip():
    client, sockets = _multi_client(
        [b"ERROR: unknown command vd_skip\nEND\n", b"Done.\nEND\n"]
    )
    assert client.skip() is True
    assert sockets[0].sent == b"vd_skip\n"
    assert sockets[1].sent == b"radio.skip\n"


def test_skip_unknown_replies_rejected():
    client, _sockets = _multi_client(
        [b"ERROR: nope\nEND\n", b"ERROR: nope\nEND\n"]
    )
    assert client.skip() is False


def test_sync_does_not_accept_done():
    client, _sock = _client(b"Done.\nEND\n")
    ok, _reply = client.command("vd_sync")
    assert ok is False


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
