"""Helpers for Telethon callback_data.

Telethon delivers/accepts callback_data as bytes (unlike Pyrogram/Kurigram,
which use str). All callback_data builders and parsers go through these two
functions so the encoding is handled in exactly one place.
"""

from __future__ import annotations


def encode(*parts: str) -> bytes:
    return ":".join(parts).encode("utf-8")


def decode(data: bytes) -> list[str]:
    return data.decode("utf-8").split(":")
