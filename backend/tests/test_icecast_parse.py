from __future__ import annotations

from app.icecast import IcecastClient, _audio_info_int


def test_audio_info_bitrate_is_parsed() -> None:
    assert _audio_info_int("channels=2;samplerate=44100;bitrate=128", "bitrate") == 128
    assert _audio_info_int("bitrate=192;channels=2", "bitrate") == 192
    assert _audio_info_int(None, "bitrate") is None
    assert _audio_info_int("channels=2", "bitrate") is None
    assert _audio_info_int("bitrate=bad", "bitrate") is None


def test_parse_uses_audio_info_when_top_level_bitrate_missing() -> None:
    payload = {
        "icestats": {
            "source": {
                "listenurl": "http://localhost:8101/radio.mp3",
                "listeners": 3,
                "title": "Ваниль - Первый тест",
                "audio_info": "channels=2;samplerate=44100;bitrate=128",
            }
        }
    }
    stats = IcecastClient._parse(payload)
    assert stats["mount_present"] is True
    assert stats["listeners"] == 3
    assert stats["bitrate"] == 128


def test_parse_prefers_explicit_bitrate_field() -> None:
    payload = {
        "icestats": {
            "source": [
                {
                    "listenurl": "http://localhost:8101/radio.mp3",
                    "bitrate": 192,
                    "audio_info": "bitrate=128",
                }
            ]
        }
    }
    assert IcecastClient._parse(payload)["bitrate"] == 192


def test_parse_without_source_reports_no_mount() -> None:
    stats = IcecastClient._parse({"icestats": {"dummy": None}})
    assert stats["mount_present"] is False
    assert stats["listeners"] == 0
