"""End-to-end download pipeline: check quota -> download -> split -> upload
-> archive forward -> charge credits -> delete local files.

Credit-charging decision (fixes spec/INVENTORY.md section 2's flagged bug):
credits are deducted only after every part of a download has been
successfully uploaded. The old bot charged credits *before* the ffmpeg
split+upload for large videos (src/engine/base.py:976), so a send that
failed after a successful split still cost the user credits, with no refund
path anywhere in that codebase.

Charge-after-success was chosen over charge-then-refund-on-failure because
it needs exactly one accounting write, gated on total success - there is
never a window where a user has been charged and no file was delivered. A
refund-on-failure design needs a second write (the refund) to itself
succeed for the accounting to stay correct, which is one more failure mode
for no real benefit here.

Every failure path (download error, split error, upload error, or the
pipeline being interrupted) is caught by the single try/except/finally
below: no credits are deducted, the user sees one error message (via the
same progress message used for status updates), and every file this run
wrote to disk - including split parts and the original pre-split download -
is deleted.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path
from typing import Any, Protocol

from media_bot_v2.cache.video_cache import CacheEntry, VideoCacheStore
from media_bot_v2.credits.service import CreditsService
from media_bot_v2.engines.base import BaseEngine, DownloadResult
from media_bot_v2.telegram import texts
from media_bot_v2.upload import splitter

logger = logging.getLogger(__name__)


class ProgressReporter(Protocol):
    async def update(self, text: str) -> None: ...


class Uploader(Protocol):
    async def send_file(self, path: Path, *, caption: str | None = None) -> Any: ...
    async def forward_to_archive(self, message: Any) -> Any | None: ...
    async def send_cached(self, archive_chat: str, message_ids: list[int]) -> Any: ...


class DownloadPipeline:
    def __init__(self, *, credits_service: CreditsService, download_dir: Path) -> None:
        self._credits = credits_service
        self._download_dir = download_dir

    async def run(
        self,
        *,
        user_id: int,
        url: str,
        engine: BaseEngine,
        uploader: Uploader,
        progress: ProgressReporter,
        cache: VideoCacheStore | None = None,
        cache_key: str | None = None,
        archive_channel: str | None = None,
    ) -> None:
        self._credits.check_quota(user_id)

        if cache is not None and cache_key is not None:
            cached = cache.get(cache_key)
            if cached is not None and await self._try_serve_from_cache(
                user_id=user_id, uploader=uploader, cached=cached, progress=progress
            ):
                return
            if cached is not None:
                # Entry looked valid but couldn't actually be resent (the
                # archive message was deleted, the channel access changed,
                # etc.) - drop it so we don't keep retrying a dead reference,
                # then fall through to a normal fresh download below.
                cache.delete(cache_key)

        # A per-task subdirectory, not a per-user one: two downloads running
        # concurrently for the same user (queue/limiter.py allows more than
        # one) must not write into the same directory, where one download's
        # cleanup could delete the other's still-in-flight files.
        task_dir = self._download_dir / str(user_id) / uuid.uuid4().hex
        try:
            await progress.update(texts.DOWNLOADING)
            result: DownloadResult = await engine.download(url, dest_dir=task_dir)

            await progress.update(texts.PROCESSING)
            parts: list[Path] = []
            for raw_path in result.file_paths:
                parts.extend(splitter.split_file(Path(raw_path)))

            await progress.update(texts.UPLOADING)
            total_size = 0
            archived_message_ids: list[int] = []
            for index, part in enumerate(parts, start=1):
                caption = result.title if len(parts) == 1 else f"{result.title} ({index}/{len(parts)})"
                message = await uploader.send_file(part, caption=caption)
                total_size += part.stat().st_size
                try:
                    forwarded = await uploader.forward_to_archive(message)
                except Exception:
                    # The file already reached the user and its bytes count
                    # toward their charge - an archive-channel hiccup is not
                    # their problem and must not undo either.
                    logger.warning(
                        "Archive forward failed for user=%s url=%s part=%s",
                        user_id,
                        url,
                        part,
                        exc_info=True,
                    )
                    forwarded = None
                if forwarded is not None:
                    archived_message_ids.append(forwarded.id)

            self._credits.use_quota_dynamic(user_id, total_size)
            self._credits.add_bandwidth_used(user_id, total_size)

            if (
                cache is not None
                and cache_key is not None
                and archive_channel is not None
                and parts
                and len(archived_message_ids) == len(parts)
            ):
                cache.put(
                    cache_key,
                    archive_chat=archive_channel,
                    message_ids=archived_message_ids,
                    title=result.title,
                )

            await progress.update(texts.DOWNLOAD_DONE)
        except Exception:
            logger.exception("Download pipeline failed for user=%s url=%s", user_id, url)
            await progress.update(texts.DOWNLOAD_FAILED)
            raise
        finally:
            self._cleanup(task_dir)

    async def _try_serve_from_cache(
        self,
        *,
        user_id: int,
        uploader: Uploader,
        cached: CacheEntry,
        progress: ProgressReporter,
    ) -> bool:
        try:
            await progress.update(texts.DOWNLOAD_FROM_CACHE)
            await uploader.send_cached(cached.archive_chat, cached.message_ids)
        except Exception:
            logger.warning("Cache resend failed for user=%s, falling back to a fresh download", user_id, exc_info=True)
            return False
        self._credits.use_quota_dynamic(user_id, 0)
        self._credits.add_bandwidth_used(user_id, 0)
        await progress.update(texts.DOWNLOAD_DONE)
        return True

    def _cleanup(self, task_dir: Path) -> None:
        """Remove every file this run wrote, regardless of how far it got.

        Must not depend on which download/split step actually ran or on
        `DownloadResult`/part lists being populated - a download that fails
        partway through (engine crash mid-write, ffmpeg crash mid-split)
        still leaves bytes in `task_dir` that a result/parts-based cleanup
        would silently miss.
        """
        if task_dir.exists():
            for child in task_dir.iterdir():
                try:
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                except OSError:
                    logger.warning("Failed to delete leftover path %s", child)
            try:
                task_dir.rmdir()
            except OSError:
                pass

        try:
            task_dir.parent.rmdir()
        except OSError:
            pass  # other concurrent tasks for this user still have files here
