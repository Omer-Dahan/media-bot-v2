"""End-to-end download pipeline: check quota -> download -> split -> upload
-> archive copy -> charge credits -> delete local files.

Credit charging (volume model, restored from the old bot): a credit is a unit
of *volume*, not of requests or parts. One request costs
`max(1, ceil(total_delivered_MB / MB_PER_CREDIT))` credits, computed by
`credits.service.credits_for_sizes` over the summed size of every part that
was delivered (default 200MB per credit: 200MB -> 1, 400MB -> 2, 5GB -> 26,
three 100MB parts -> 2). Splitting a file never changes its price.

* Nothing is charged up front. The size of each part is recorded once it is
  uploaded successfully, and the request is charged exactly once, after the
  upload loop, for the parts that were actually delivered. If part 2 of 3
  fails, the user pays for part 1 only (the charge still happens on the error
  path) and the request errors out. Nothing delivered means nothing charged.
* Bandwidth is recorded per delivered part.
* A cache hit re-sends the archived media (never a forward: no "Forwarded
  from" header) with a caption rebuilt for the requesting user, and charges
  nothing.

Delivery style (restored from the old bot, see spec/DESIGN-PARITY.md): every
file is probed (duration/resolution) and given a thumbnail *before* any
splitting, videos go out as streamable videos or - per the user's setting - as
documents, captions are the old bot's HTML signatures, a split file's parts
are labelled "📎 חלק i/N" with the full signature moving to the latest part
sent, subtitles follow as separate uncharged documents when the user enabled
them, and the "full description" message follows when the description length
is set to 4000. The signature never contains a credits line.

Cache writes are all-or-nothing: an archive-cache entry is stored only after
every part of the result was uploaded and copied to the archive, and only
if the result is not a trimmed playlist. A partial delivery is never cached,
because a later identical request would otherwise be served the partial set
from cache and told "done" without the download ever running again.

Every failure path (download error, split error, upload error, or the
pipeline being interrupted) is caught by the single try/except/finally
below: the user sees one error message (via the same progress message used
for status updates), and every file this run wrote to disk - including split
parts and the original pre-split download - is deleted.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from media_bot_v2.cache.video_cache import CachedItem, CacheEntry, VideoCacheStore
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
from media_bot_v2.telegram import captions, texts
from media_bot_v2.telegram.delivery import DeliveryOptions
from media_bot_v2.telegram.flood_wait import FLOOD_WAIT_ERRORS, get_flood_wait_seconds
from media_bot_v2.telegram.progress import MessageProgressReporter, UploadProgress
from media_bot_v2.upload import splitter
from media_bot_v2.upload.media_probe import KIND_VIDEO, MediaInfo, probe, probe_with_thumb
from media_bot_v2.upload.streamable import DEFAULT_FIX_TIMEOUT_SECONDS, ensure_streamable

logger = logging.getLogger(__name__)


class ProgressReporter(Protocol):
    async def update(self, text: str) -> None: ...


async def _update_progress(
    progress: ProgressReporter | None,
    text: str,
    *,
    buttons: Any = None,
    is_terminal: bool = False,
) -> None:
    if progress is None:
        return
    if isinstance(progress, MessageProgressReporter):
        await progress.update(text, buttons=buttons, is_terminal=is_terminal)
        return
    if buttons is not None:
        try:
            await progress.update(text, buttons=buttons)
        except TypeError:
            await progress.update(text)
    else:
        try:
            await progress.update(text)
        except TypeError:
            await progress.update(text, is_terminal=is_terminal)


class Uploader(Protocol):
    async def send_file(
        self,
        path: Path,
        *,
        caption: str | None = None,
        media: MediaInfo | None = None,
        as_document: bool = False,
        title: str | None = None,
        progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> Any: ...
    async def copy_to_archive(self, message: Any, *, caption: str) -> Any | None: ...
    async def edit_caption(self, message: Any, caption: str) -> None: ...
    async def send_subtitle(self, path: Path) -> Any: ...
    async def send_description(self, text: str, *, reply_to: Any) -> Any: ...
    async def send_cached(
        self, archive_chat: str, message_ids: list[int], *, captions: list[str] | None = None
    ) -> Any: ...


@dataclass
class _FileGroup:
    """One downloaded file and the on-disk parts it was split into (or itself)."""

    info: MediaInfo  # probed on the whole file, before splitting
    parts: list[Path]


class DownloadPipeline:
    def __init__(
        self,
        *,
        credits_service: CreditsService,
        download_dir: Path,
        request_timeout: float = 600.0,
        download_timeout: float | None = None,
        upload_timeout: float | None = None,
        convert_timeout: float | None = None,
    ) -> None:
        self._credits = credits_service
        self._download_dir = download_dir
        self._request_timeout = request_timeout
        self._download_timeout = download_timeout if download_timeout is not None else request_timeout
        self._upload_timeout = upload_timeout if upload_timeout is not None else request_timeout
        self._convert_timeout = convert_timeout

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
        delivery: DeliveryOptions | None = None,
    ) -> None:
        self._credits.check_quota(user_id)
        delivery = delivery or DeliveryOptions()

        if cache is not None and cache_key is not None:
            cached = cache.get(cache_key)
            if cached is not None and await self._try_serve_from_cache(
                user_id=user_id,
                url=url,
                uploader=uploader,
                cached=cached,
                progress=progress,
                delivery=delivery,
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
        delivered_sizes: list[int] = []
        charged = False

        try:
            # 1. Download phase under download_timeout
            await _update_progress(progress, texts.DOWNLOADING, is_terminal=False)
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

            # 2. Processing phase: probe + thumbnail every file while it is still
            # whole (splitting deletes the source), then split oversized ones.
            await _update_progress(progress, texts.PROCESSING, is_terminal=False)
            groups: list[_FileGroup] = []
            for raw_path in result.file_paths:
                source = Path(raw_path)
                if not delivery.as_document:
                    # "Send as file" delivers the bytes untouched; everything
                    # sent as playable video must be H.264/AAC MP4 with the
                    # moov up front or clients fail with IO_UNSPECIFIED. This
                    # runs OUTSIDE the upload budget with its own smaller
                    # one: a slow conversion is killed and the original is
                    # sent, and it never eats the time the upload needs.
                    source = await self._make_streamable(source)
                async with self._upload_budget() as cm:
                    up_cm = cm
                    info = await asyncio.to_thread(probe_with_thumb, source)
                parts = await asyncio.to_thread(splitter.split_file, source)
                groups.append(_FileGroup(info=info, parts=parts))

            # 3. Upload phase under upload_timeout, recording delivered sizes; cache only a complete result
            await _update_progress(progress, texts.UPLOADING, is_terminal=False)
            upload_progress = UploadProgress(
                progress, texts.UPLOADING, sum(p.stat().st_size for g in groups for p in g.parts)
            )
            archived_message_ids: list[int] = []
            cached_items: list[CachedItem] = []
            all_parts_archived = True
            last_media_message: Any = None
            any_parts = False
            for group in groups:
                total_parts = len(group.parts)
                previous: tuple[Any, str] | None = None
                for index, part in enumerate(group.parts, start=1):
                    any_parts = True
                    async with self._upload_budget() as cm:
                        up_cm = cm
                        send_info, label, playable = await self._prepare_part(
                            group, part, index, total_parts, as_document=delivery.as_document
                        )
                        full_caption = captions.build_user_caption(
                            kind=group.info.kind,
                            title=result.title,
                            url=url,
                            width=group.info.width,
                            height=group.info.height,
                            duration=group.info.duration,
                            title_length=delivery.title_length,
                            reserved_units=captions.utf16_units(label) + 2 if label else 0,
                        )
                        caption = captions.with_part_label(label, full_caption) if label else full_caption
                        upload_progress.start_part(sum(delivered_sizes))
                        message = await uploader.send_file(
                            part,
                            caption=caption,
                            media=send_info,
                            as_document=delivery.as_document or not playable,
                            title=result.title,
                            progress=upload_progress,
                        )
                        last_media_message = message

                        part_size = part.stat().st_size
                        delivered_sizes.append(part_size)
                        self._credits.add_bandwidth_used(user_id, part_size)

                        archive_id = await self._archive_copy(
                            uploader, message, part.name, user_id=user_id, url=url, delivery=delivery
                        )
                        if archive_id is not None:
                            archived_message_ids.append(archive_id)
                        else:
                            all_parts_archived = False
                        cached_items.append(
                            CachedItem(
                                kind=group.info.kind,
                                duration=group.info.duration,
                                width=group.info.width,
                                height=group.info.height,
                                part_index=index if label else 0,
                                part_total=total_parts if label else 0,
                                part_label=label or None,
                            )
                        )

                        # The full signature lives on the newest part; the one
                        # before it drops to its bare label. Doing it only after
                        # the next part arrived means a failed part leaves the
                        # last delivered one still carrying the full signature.
                        if previous is not None:
                            await self._edit_caption_quietly(uploader, previous[0], previous[1])
                        if label:
                            previous = (message, label)

            charged = True
            self._credits.use_quota_dynamic(user_id, delivered_sizes)

            # 4. Extras after the media: subtitles (never charged) and the
            # "full description" message. Neither may fail the download.
            subtitle_ids: list[int] = []
            subtitle_names: list[str] = []
            subtitles_archived = True
            if delivery.subtitles:
                for sub_path in result.subtitle_paths:
                    sub = Path(sub_path)
                    try:
                        async with self._upload_budget():
                            sub_message = await uploader.send_subtitle(sub)
                            sub_archive_id = await self._archive_copy(
                                uploader, sub_message, sub.name, user_id=user_id, url=url, delivery=delivery
                            )
                    except Exception:
                        logger.warning("Failed to send subtitle %s for url=%s", sub, url, exc_info=True)
                        subtitles_archived = False
                        continue
                    if sub_archive_id is None:
                        subtitles_archived = False
                    else:
                        subtitle_ids.append(sub_archive_id)
                        subtitle_names.append(sub.name)

            await self._send_description_quietly(
                uploader,
                title=result.title,
                description=result.description,
                delivery=delivery,
                reply_to=last_media_message,
            )

            trimmed = (
                result.playlist_total is not None
                and result.playlist_downloaded is not None
                and result.playlist_downloaded < result.playlist_total
            )
            if (
                cache is not None
                and cache_key is not None
                and archive_channel is not None
                and any_parts
                and all_parts_archived
                and subtitles_archived
                and not trimmed
            ):
                cache.put(
                    cache_key,
                    archive_chat=archive_channel,
                    message_ids=archived_message_ids,
                    title=result.title,
                    items=cached_items,
                    description=result.description[: captions.DESCRIPTION_LIMIT] if result.description else None,
                    subtitle_ids=subtitle_ids,
                    subtitle_names=subtitle_names,
                )
            if trimmed:
                await _update_progress(
                    progress,
                    texts.format_playlist_trimmed(
                        result.playlist_downloaded,
                        result.playlist_total,
                        reason=result.playlist_trimmed_reason,
                    ),
                    is_terminal=True,
                )
            else:
                await _update_progress(progress, texts.DOWNLOAD_DONE, is_terminal=True)
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
                await _update_progress(progress, texts.REQUEST_TIMEOUT_EXCEEDED, is_terminal=True)
                raise
            logger.warning(
                "Foreign TimeoutError in pipeline for user=%s url=%s: %s",
                user_id,
                url,
                exc,
            )
            await _update_progress(progress, texts.DOWNLOAD_FAILED, is_terminal=True)
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
            await _update_progress(progress, str(exc), is_terminal=True)
            raise
        except (CreditsExhaustedException, BandwidthExhaustedException, UserBlockedException):
            raise
        except FLOOD_WAIT_ERRORS as exc:
            cancel_token.set()
            wait_seconds = get_flood_wait_seconds(exc)
            logger.warning(
                "Download pipeline aborted due to %s (%ss) for user=%s url=%s",
                type(exc).__name__,
                wait_seconds,
                user_id,
                url,
            )
            await _update_progress(progress, texts.FLOOD_WAIT_FAILED, is_terminal=True)
            raise
        except Exception:
            logger.exception("Download pipeline failed for user=%s url=%s", user_id, url)
            await _update_progress(progress, texts.DOWNLOAD_FAILED, is_terminal=True)
            raise
        finally:
            cancel_token.set()
            if delivered_sizes and not charged:
                # Error/cancel after at least one part reached the user:
                # charge for exactly what was delivered.
                try:
                    self._credits.use_quota_dynamic(user_id, delivered_sizes)
                except Exception:
                    logger.exception("Failed to charge delivered parts for user=%s url=%s", user_id, url)
            self._cleanup(task_dir)

    def _fix_timeout(self) -> float:
        """Conversion budget: always smaller than the upload budget."""
        if self._convert_timeout is not None and self._convert_timeout > 0:
            return float(self._convert_timeout)
        if self._upload_timeout and self._upload_timeout > 0:
            return min(DEFAULT_FIX_TIMEOUT_SECONDS, self._upload_timeout / 3)
        return DEFAULT_FIX_TIMEOUT_SECONDS

    async def _make_streamable(self, source: Path) -> Path:
        """`ensure_streamable` in a worker thread. Its subprocess timeout kills
        the ffmpeg child; any failure (timeout included) is logged and the
        original file is returned - never an error for the user."""
        try:
            return await asyncio.to_thread(ensure_streamable, source, timeout=self._fix_timeout())
        except Exception:
            logger.warning("Streamable conversion failed for %s, sending it as-is", source, exc_info=True)
            return source

    def _upload_budget(self):
        if self._upload_timeout and self._upload_timeout > 0:
            return asyncio.timeout(self._upload_timeout)
        return asyncio.nullcontext()

    async def _prepare_part(
        self, group: _FileGroup, part: Path, index: int, total: int, *, as_document: bool
    ) -> tuple[MediaInfo, str, bool]:
        """(media info to send with, caption label, is the part playable media).

        A whole file uses the info probed before splitting. A split part is
        probed on its own so it gets its real duration (ffmpeg's segments are
        not equal length), while resolution and the thumbnail come from the
        source. A part that is not playable on its own - a raw byte chunk, the
        splitter's fallback - is sent as a document with a file-name label.
        """
        if total == 1:
            return group.info, "", True
        part_info = await asyncio.to_thread(probe, part)
        playable = part_info.kind == group.info.kind and group.info.kind in (KIND_VIDEO, "audio")
        if not playable:
            return MediaInfo(), captions.doc_part_label(index, total, part.name), False
        send_info = replace(
            part_info,
            width=group.info.width,
            height=group.info.height,
            thumb_path=group.info.thumb_path,
        )
        if as_document:
            return send_info, captions.doc_part_label(index, total, part.name), True
        return send_info, captions.part_label(index, total), True

    async def _archive_copy(
        self,
        uploader: Uploader,
        message: Any,
        filename: str,
        *,
        user_id: int,
        url: str,
        delivery: DeliveryOptions,
    ) -> int | None:
        """Archive one delivered message; a failure only means "not cached"."""
        try:
            copied = await uploader.copy_to_archive(
                message,
                caption=captions.build_archive_caption(
                    user_display=delivery.user_display or str(user_id),
                    user_id=user_id,
                    filename=filename,
                    url=url,
                ),
            )
        except Exception:
            logger.warning("Archive copy failed for user=%s url=%s file=%s", user_id, url, filename, exc_info=True)
            return None
        return copied.id if copied is not None else None

    async def _edit_caption_quietly(self, uploader: Uploader, message: Any, caption: str) -> None:
        try:
            await uploader.edit_caption(message, caption)
        except Exception:
            logger.warning("Failed to edit a part's caption to %r", caption, exc_info=True)

    async def _send_description_quietly(
        self,
        uploader: Uploader,
        *,
        title: str | None,
        description: str | None,
        delivery: DeliveryOptions,
        reply_to: Any,
    ) -> None:
        """The "full description" message of description-length 4000: sent only
        when there is something the caption could not hold."""
        if delivery.title_length != captions.DESCRIPTION_LIMIT:
            return
        if not description and len(title or "") <= captions.title_limit(delivery.title_length):
            return
        text = captions.build_description_message(title, description)
        if text is None:
            return
        try:
            async with self._upload_budget():
                await uploader.send_description(text, reply_to=reply_to)
        except Exception:
            logger.warning("Failed to send the full description message", exc_info=True)

    def _cached_caption(self, cached: CacheEntry, index: int, *, url: str, title_length: int) -> str:
        """Caption for an archived message being re-sent to a user, rebuilt
        for that user (never the archive's operator caption). Rows written
        before per-message metadata existed get a basic video caption."""
        item = cached.items[index] if index < len(cached.items) else CachedItem()
        label = item.part_label or ""
        full = captions.build_user_caption(
            kind=item.kind,
            title=cached.title,
            url=url,
            width=item.width,
            height=item.height,
            duration=item.duration,
            title_length=title_length,
            reserved_units=captions.utf16_units(label) + 2 if label else 0,
        )
        if not label:
            return full
        if item.part_index < item.part_total:
            return label
        return captions.with_part_label(label, full)

    async def _try_serve_from_cache(
        self,
        *,
        user_id: int,
        url: str,
        uploader: Uploader,
        cached: CacheEntry,
        progress: ProgressReporter,
        delivery: DeliveryOptions,
    ) -> bool:
        try:
            await _update_progress(progress, texts.DOWNLOAD_FROM_CACHE, is_terminal=False)
            sent = await uploader.send_cached(
                cached.archive_chat,
                cached.message_ids,
                captions=[
                    self._cached_caption(cached, i, url=url, title_length=delivery.title_length)
                    for i in range(len(cached.message_ids))
                ],
            )
        except Exception:
            logger.warning("Cache resend failed for user=%s, falling back to a fresh download", user_id, exc_info=True)
            return False

        # The media is delivered; from here on nothing may report a failure.
        if delivery.subtitles and cached.subtitle_ids:
            try:
                await uploader.send_cached(
                    cached.archive_chat,
                    cached.subtitle_ids,
                    captions=[captions.subtitle_caption(name) for name in cached.subtitle_names],
                )
            except Exception:
                logger.warning("Cached subtitles could not be re-sent for user=%s", user_id, exc_info=True)
        reply_to = sent[-1] if isinstance(sent, list) and sent else None
        await self._send_description_quietly(
            uploader,
            title=cached.title,
            description=cached.description,
            delivery=delivery,
            reply_to=reply_to,
        )
        await _update_progress(progress, texts.DOWNLOAD_DONE, is_terminal=True)
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
