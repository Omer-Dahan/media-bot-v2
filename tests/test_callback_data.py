"""Telethon callback_data is bytes, unlike Pyrogram/Kurigram's str - lock this in."""

from media_bot_v2.telegram.callback_data import decode, encode


def test_encode_returns_bytes():
    data = encode("ytq", "1080", "abc123")
    assert isinstance(data, bytes)
    assert data == b"ytq:1080:abc123"


def test_decode_round_trips():
    data = encode("cancel", "42", "99")
    assert decode(data) == ["cancel", "42", "99"]
