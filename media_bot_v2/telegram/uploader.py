"""Telethon-backed uploader used by the download pipeline.

Sends each downloaded/split file to the requesting chat and forwards a copy
to the archive channel, matching the old bot's per-download archive forward
(spec/SPEC.md locked decision 4).

The archive copy is a server-side forward of the message already sent to
the user, not a second upload from disk: Telegram just re-points a file
reference server-side, so a 2GB part is not read off disk and pushed over
the wire to Telegram a second time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from telethon import TelegramClient


class TelethonUploader:
    def __init__(self, client: TelegramClient, *, chat_id: int, archive_channel: str | None) -> None:
        self._client = client
        self._chat_id = chat_id
        self._archive_channel = archive_channel

    async def send_file(self, path: Path, *, caption: str | None = None) -> Any:
        return await self._client.send_file(self._chat_id, str(path), caption=caption or "")

    async def forward_to_archive(self, message: Any) -> None:
        if not self._archive_channel:
            return
        await self._client.forward_messages(self._archive_channel, message)
