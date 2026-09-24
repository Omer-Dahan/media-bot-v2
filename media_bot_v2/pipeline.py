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

import asyncio
import inspect
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any, Protocol

from media_bot_v2.cache.video_cache import CacheEntry, VideoCacheStore
from media_bot_v2.credits.exceptions import (
    BandwidthExhaustedException,
    CreditsExhaustedException,
    UserBlockedException,
)
from media_bot_v2.credits.service import CreditsService
from media_bot_v2.engines.base import (
    BaseEngine,
    CancellationToken,
    DownloadResult,
    DownloadTooLargeError,
    UnsupportedUrlError,
)
from media_bot_v2.engines.instagram import InstagramDownloadError
from media_bot_v2.engines.tiktok import TikTokDownloadError
from media_bot_v2.engines.youtube import YouTubeDownloadError
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
    def __init__(
        self,
        *,
        credits_service: CreditsService,
        download_dir: Path,
        request_timeout: float = 600.0,
        download_timeout: float | None = None,
        upload_timeout: float | None = None,
    ) -> None:
        self._credits = credits_service
        self._download_dir = download_dir
        self._request_timeout = request_timeout
        self._download_timeout = download_timeout if download_timeout is not None else request_timeout
        self._upload_timeout = upload_timeout if upload_timeout is not None else request_timeout

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
        cancel_token = CancellationToken()
        dl_cm = None
        up_cm = None

        try:
            # 1. Download phase under download_timeout
            await progress.update(texts.DOWNLOADING)
            dl_timeout_ctx = (
                asyncio.timeout(self._download_timeout)
                if self._download_timeout and self._download_timeout > 0
                else asyncio.nullcontext()
            )
            async with dl_timeout_ctx as cm:
                dl_cm = cm
                sig = inspect.signature(engine.download)
                supports_cancel = "cancel_token" in sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                )
                if supports_cancel:
                    result: DownloadResult = await engine.download(
                        url, dest_dir=task_dir, cancel_token=cancel_token
                    )
                else:
                    result = await engine.download(url, dest_dir=task_dir)

            # 2. Processing & splitting phase
            await progress.update(texts.PROCESSING)
            parts: list[Path] = []
            for raw_path in result.file_paths:
                parts.extend(splitter.split_file(Path(raw_path)))

            # 3. Upload phase under upload_timeout, charging & caching per delivered part
            await progress.update(texts.UPLOADING)
            archived_message_ids: list[int] = []
            for index, part in enumerate(parts, start=1):
                caption = result.title if len(parts) == 1 else f"{result.title} ({index}/{len(parts)})"
                up_timeout_ctx = (
                    asyncio.timeout(self._upload_timeout)
                    if self._upload_timeout and self._upload_timeout > 0
                    else asyncio.nullcontext()
                )
                async with up_timeout_ctx as cm:
                    up_cm = cm
                    message = await uploader.send_file(part, caption=caption)

                part_size = part.stat().st_size
                self._credits.use_quota_dynamic(user_id, part_size)
                self._credits.add_bandwidth_used(user_id, part_size)

                try:
                    forwarded = await uploader.forward_to_archive(message)
                except Exception:
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
                    if (
                        cache is not None
                        and cache_key is not None
                        and archive_channel is not None
                    ):
                        cache.put(
                            cache_key,
                            archive_chat=archive_channel,
                            message_ids=archived_message_ids,
                            title=result.title,
                        )

            if (
                result.playlist_total is not None
                and result.playlist_downloaded is not None
                and result.playlist_downloaded < result.playlist_total
            ):
                await progress.update(
                    texts.format_playlist_trimmed(
                        result.playlist_downloaded,
                        result.playlist_total,
                        reason=result.playlist_trimmed_reason,
                    )
                )
            else:
                await progress.update(texts.DOWNLOAD_DONE)
        except TimeoutError as exc:
            cancel_token.set()
            dl_expired = bool(dl_cm and callable(getattr(dl_cm, "expired", None)) and dl_cm.expired())
            up_expired = bool(up_cm and callable(getattr(up_cm, "expired", None)) and up_cm.expired())
            if dl_expired or up_expired:
                logger.warning(
                    "Download pipeline timed out for user=%s url=%s (dl_timeout=%ss dl_expired=%s, up_timeout=%ss up_expired=%s)",
                    user_id,
                    url,
                    self._download_timeout,
                    dl_expired,
                    self._upload_timeout,
                    up_expired,
                )
                await progress.update(texts.REQUEST_TIMEOUT_EXCEEDED)
                raise
            logger.warning(
                "Foreign TimeoutError in pipeline for user=%s url=%s: %s",
                user_id,
                url,
                exc,
            )
            await progress.update(texts.DOWNLOAD_FAILED)
            raise
        except asyncio.CancelledError:
            cancel_token.set()
            raise
        except (
            DownloadTooLargeError,
            UnsupportedUrlError,
            YouTubeDownloadError,
            TikTokDownloadError,
            InstagramDownloadError,
        ) as exc:
            logger.warning("Download pipeline domain error for user=%s url=%s: %s", user_id, url, exc)
            await progress.update(str(exc))
            raise
        except (CreditsExhaustedException, BandwidthExhaustedException, UserBlockedException):
            raise
        except Exception:
            logger.exception("Download pipeline failed for user=%s url=%s", user_id, url)
            await progress.update(texts.DOWNLOAD_FAILED)
            raise
        finally:
            cancel_token.set()
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
