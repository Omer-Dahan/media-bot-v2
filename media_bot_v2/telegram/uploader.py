"""Telethon-backed uploader used by the download pipeline.

Sending style is restored from the old bot (src/engine/base.py): HTML
captions, `supports_streaming` video with duration/width/height and a
thumbnail, audio with `DocumentAttributeAudio`, or a plain document when the
user picked "send as file".

Nothing here ever calls `forward_messages`. A forward shows the recipient a
"Forwarded from <channel>" header, which for the archive/cache flow would
leak the internal archive channel's name to the paying user and break the
look of the delivery. Instead:

* the archive copy is `send_file(archive, message.media, ...)` - Telegram
  re-points the already-uploaded file server-side, so a 2GB part is not
  uploaded a second time, and the archive gets the operator caption;
* a cache hit is `get_messages(archive, ids)` followed by
  `send_file(user_chat, message.media, caption=<user caption>)` - the same
  server-side trick in reverse, with a caption built for the requesting user
  (never the archive's, which names another user).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    DocumentInvalidError,
    MediaEmptyError,
    MediaInvalidError,
    VideoContentTypeInvalidError,
)
from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo

from media_bot_v2.telegram.captions import subtitle_caption
from media_bot_v2.telegram.flood_wait import calculate_connections, call_with_flood_retry
from media_bot_v2.telegram.parallel_upload import ProgressCallback, upload_file_parallel
from media_bot_v2.upload.media_probe import KIND_AUDIO, KIND_PHOTO, KIND_VIDEO, MediaInfo

logger = logging.getLogger(__name__)

# Errors Telegram raises when it refuses a file *as a video/audio*. One retry
# as a plain document is allowed for these (a send attempt, not a download
# route, so the M4 one-attempt-per-route rule is untouched).
_MEDIA_REJECTED = (MediaInvalidError, VideoContentTypeInvalidError, MediaEmptyError, DocumentInvalidError)


class TelethonUploader:
    def __init__(
        self,
        client: TelegramClient,
        *,
        chat_id: int,
        archive_channel: str | None,
        workers: int = 1,
        connections: int = 1,
        adaptive: bool = False,
        on_flood: Any = None,
        on_flood_cleared: Any = None,
    ) -> None:
        self._client = client
        self._workers = workers
        self._connections = connections
        self._chat_id = chat_id
        self._archive_channel = archive_channel
        self._adaptive = adaptive
        self._on_flood = on_flood
        self._on_flood_cleared = on_flood_cleared

    async def send_file(
        self,
        path: Path,
        *,
        caption: str | None = None,
        media: MediaInfo | None = None,
        as_document: bool = False,
        title: str | None = None,
        progress: ProgressCallback | None = None,
        on_flood: Any = None,
        on_flood_cleared: Any = None,
    ) -> Any:
        info = media or MediaInfo()
        # Photos are always sent as photos, whatever the "send as" setting.
        as_document = as_document and info.kind != KIND_PHOTO
        kwargs = self._send_kwargs(info, caption=caption, as_document=as_document, title=title)
        flood_cb = on_flood or getattr(progress, "handle_flood_wait", None) or self._on_flood
        flood_cleared_cb = (
            on_flood_cleared or getattr(progress, "handle_flood_cleared", None) or self._on_flood_cleared
        )
        source = await self._source(path, info, progress, on_flood=flood_cb, on_flood_cleared=flood_cleared_cb)
        try:
            return await call_with_flood_retry(
                self._client.send_file,
                self._chat_id,
                source,
                on_flood=flood_cb,
                on_flood_cleared=flood_cleared_cb,
                **kwargs,
            )
        except _MEDIA_REJECTED:
            if as_document:
                raise
            logger.warning("Telegram rejected %s as %s, retrying once as a document", path.name, info.kind)
            fallback = self._send_kwargs(info, caption=caption, as_document=True, title=title)
            return await call_with_flood_retry(
                self._client.send_file,
                self._chat_id,
                source,
                on_flood=flood_cb,
                on_flood_cleared=flood_cleared_cb,
                **fallback,
            )

    async def _source(
        self,
        path: Path,
        info: MediaInfo,
        progress: ProgressCallback | None,
        on_flood: Any = None,
        on_flood_cleared: Any = None,
    ) -> Any:
        """What to hand to `client.send_file`: the plain path (Telethon uploads it
        sequentially) or, with several workers, an already-uploaded handle.
        The handle is reused for the "retry as a document" send, so a rejected
        video is not uploaded twice. Photos keep the path: Telethon resizes
        oversized photos itself, and they are far too small to gain anything."""
        if self._workers <= 1 or info.kind == KIND_PHOTO:
            return str(path)
        file_size = path.stat().st_size
        connections = (
            calculate_connections(file_size, self._connections)
            if self._adaptive
            else self._connections
        )
        extra_kwargs: dict[str, Any] = {}
        if on_flood is not None:
            extra_kwargs["on_flood"] = on_flood
        if on_flood_cleared is not None:
            extra_kwargs["on_flood_cleared"] = on_flood_cleared
        return await upload_file_parallel(
            self._client,
            path,
            workers=self._workers,
            connections=connections,
            progress=progress,
            **extra_kwargs,
        )

    @staticmethod
    def _send_kwargs(info: MediaInfo, *, caption: str | None, as_document: bool, title: str | None) -> dict:
        kwargs: dict[str, Any] = {"caption": caption or "", "parse_mode": "html"}
        thumb = str(info.thumb_path) if info.thumb_path else None
        if as_document:
            kwargs["force_document"] = True
            if thumb:
                kwargs["thumb"] = thumb
            return kwargs

        kwargs["force_document"] = False
        if info.kind == KIND_VIDEO:
            kwargs["supports_streaming"] = True
            if thumb:
                kwargs["thumb"] = thumb
            if info.width and info.height:
                kwargs["attributes"] = [
                    DocumentAttributeVideo(
                        duration=info.duration,
                        w=info.width,
                        h=info.height,
                        supports_streaming=True,
                    )
                ]
        elif info.kind == KIND_AUDIO:
            kwargs["attributes"] = [DocumentAttributeAudio(duration=info.duration, title=title)]
        return kwargs

    async def edit_caption(self, message: Any, caption: str) -> None:
        await call_with_flood_retry(
            self._client.edit_message,
            self._chat_id,
            message,
            caption,
            parse_mode="html",
            on_flood=self._on_flood,
            on_flood_cleared=self._on_flood_cleared,
        )

    async def send_subtitle(self, path: Path) -> Any:
        return await call_with_flood_retry(
            self._client.send_file,
            self._chat_id,
            str(path),
            caption=subtitle_caption(path.name),
            parse_mode="html",
            force_document=True,
            on_flood=self._on_flood,
            on_flood_cleared=self._on_flood_cleared,
        )

    async def send_description(self, text: str, *, reply_to: Any) -> Any:
        return await call_with_flood_retry(
            self._client.send_message,
            self._chat_id,
            text,
            parse_mode="html",
            reply_to=reply_to,
            link_preview=False,
            on_flood=self._on_flood,
            on_flood_cleared=self._on_flood_cleared,
        )

    async def copy_to_archive(self, message: Any, *, caption: str) -> Any | None:
        """Put a copy of an already-sent message into the archive channel.

        Server-side re-send of the same media (no re-upload, no "Forwarded
        from" header), captioned for the operator."""
        if not self._archive_channel:
            return None
        return await call_with_flood_retry(
            self._client.send_file,
            self._archive_channel,
            message.media,
            caption=caption,
            parse_mode="html",
        )

    async def send_cached(
        self, archive_chat: str, message_ids: list[int], *, captions: list[str] | None = None
    ) -> list[Any]:
        """Re-send archived messages to the requesting chat, one per id, with
        the caption built for this user. Raises if any archived message is
        gone, so the caller falls back to a fresh download."""
        archived = await call_with_flood_retry(self._client.get_messages, archive_chat, ids=message_ids)
        if not isinstance(archived, list):
            archived = [archived]
        if len(archived) != len(message_ids) or any(m is None or not getattr(m, "media", None) for m in archived):
            raise LookupError("archived message no longer available")

        sent: list[Any] = []
        for index, message in enumerate(archived):
            caption = captions[index] if captions and index < len(captions) else ""
            sent.append(
                await call_with_flood_retry(
                    self._client.send_file,
                    self._chat_id,
                    message.media,
                    caption=caption,
                    parse_mode="html",
                    on_flood=self._on_flood,
                    on_flood_cleared=self._on_flood_cleared,
                )
            )
        return sent
