"""Request router: Telethon event handlers wired to commands, the settings
menu, the YouTube quality-select menu (UI only - the YouTube engine itself
lands in M2), and the direct-link download pipeline, which is the one
engine implemented end-to-end in M1 (spec/SPEC.md M1 item 7).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import sessionmaker
from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError, RPCError

from media_bot_v2.cache.video_cache import VideoCacheStore, compute_cache_key
from media_bot_v2.credits.exceptions import (
    BandwidthExhaustedException,
    CreditsExhaustedException,
    UserBlockedException,
)
from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Setting
from media_bot_v2.db.session import session_scope
from media_bot_v2.engines.base import DownloadTooLargeError, UnsupportedUrlError
from media_bot_v2.engines.direct import DirectEngine
from media_bot_v2.engines.instagram import (
    InstagramDownloadError,
    InstagramEngine,
    extract_instagram_id,
    matches_instagram_url,
)
from media_bot_v2.engines.tiktok import TikTokDownloadError, TikTokEngine
from media_bot_v2.engines.youtube import (
    YouTubeDownloadError,
    YouTubeEngine,
    extract_video_id,
    fetch_title_duration,
    is_playlist_url,
)
from media_bot_v2.pipeline import DownloadPipeline
from media_bot_v2.providers.health import ProviderHealthTracker
from media_bot_v2.providers.registry import ProviderRegistry
from media_bot_v2.queue.limiter import ConcurrencyLimiter
from media_bot_v2.telegram import settings_menu, texts
from media_bot_v2.telegram.callback_data import decode, encode
from media_bot_v2.telegram.delivery import DeliveryOptions
from media_bot_v2.telegram.flood_wait import (
    FLOOD_WAIT_ERRORS,
    call_with_flood_retry,
    get_flood_wait_seconds,
)
from media_bot_v2.telegram.progress import MessageProgressReporter
from media_bot_v2.telegram.quality_menu import QualitySelectionStore, build_quality_markup
from media_bot_v2.telegram.retry import (
    RetryStore,
    build_retry_markup,
    is_hopeless_failure,
)
from media_bot_v2.telegram.uploader import TelethonUploader

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://\S+")
YOUTUBE_HOSTS = ("youtube.com", "youtu.be")
TIKTOK_HOSTS = ("tiktok.com", "douyin.com")
INSTAGRAM_HOSTS = ("instagram.com", "instagr.am")

# The quality menu waits at most this long for the real title/duration; past
# it the menu goes out with its placeholders (see fetch_title_duration).
MENU_LOOKUP_TIMEOUT_SECONDS = 8.0
MENU_STATUS_TIMEOUT_SECONDS = 12.0
_MARKDOWN_SPECIALS = str.maketrans({"*": " ", "_": " ", "`": "'", "[": "(", "]": ")"})


def _host_matches(url: str, hosts: tuple[str, ...]) -> bool:
    try:
        host = urlparse(url).netloc.split(":")[0].lower()
    except (ValueError, AttributeError):
        return False
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in hosts)


def _sender_info(event) -> tuple[str | None, str | None]:
    sender = event.sender
    return getattr(sender, "first_name", None), getattr(sender, "username", None)


def _display_name(first_name: str | None, username: str | None) -> str:
    name = (first_name or "").strip()
    if username:
        name = f"{name} @{username}".strip()
    return name


def _contact_buttons() -> list[list[Button]]:
    return [[Button.url(texts.CREDITS_BUTTON, texts.CONTACT_URL)]]


CANCEL_CALLBACK_DATA = encode("cancel")


def _cancel_markup() -> list[list[Button]]:
    return [[Button.inline(texts.CANCEL_BUTTON, CANCEL_CALLBACK_DATA)]]


@dataclass
class _ActiveRequest:
    """One in-flight, cancellable `pipeline.run()` call: the task the ❌
    button can `.cancel()`, who is allowed to press it, and the progress
    message so the button press can confirm the cancellation immediately
    instead of waiting for the task's own unwind to get there."""

    task: asyncio.Task
    owner_id: int
    progress: MessageProgressReporter


async def _run_cancellable(
    active: dict[tuple[int, int], _ActiveRequest],
    *,
    chat_id: int,
    message: Any,
    owner_id: int,
    progress: MessageProgressReporter,
    coro: Awaitable[None],
) -> None:
    """Runs `coro` (a `pipeline.run(...)` call) as a task registered under
    `message` for the ❌ cancel button, so a button press can interrupt it via
    `task.cancel()`. The pipeline's own `except asyncio.CancelledError`
    handler (media_bot_v2/pipeline.py) takes care of stopping the
    download/upload, charging for whatever was already delivered, and
    deleting local files - this just wires the button to that existing path.

    Degrades to a plain `await coro` (no cancel button support) if `message`
    has no `id` - the last-resort fallback in quality_pick_handler when even
    `event.respond` failed can leave `message` as the event itself.
    """
    message_id = getattr(message, "id", None)
    if message_id is None:
        await coro
        return

    key = (chat_id, message_id)
    task: asyncio.Task = asyncio.create_task(coro)
    active[key] = _ActiveRequest(task=task, owner_id=owner_id, progress=progress)
    try:
        await task
    except asyncio.CancelledError:
        # Cancelled via the button (nothing else cancels this task): treat it
        # as a handled outcome, not a failure to propagate as an error.
        if not progress.is_terminal_completed:
            try:
                await progress.update(texts.REQUEST_CANCELLED, is_terminal=True)
            except Exception:
                logger.debug("Failed to update progress after cancellation", exc_info=True)
    finally:
        active.pop(key, None)


async def _safe_answer_callback(
    event: events.CallbackQuery.Event,
    *args: Any,
    max_wait_seconds: float = 10.0,
    sleep_func: Callable[[float], Awaitable[None]] | None = None,
    **kwargs: Any,
) -> None:
    deadline = time.monotonic() + max_wait_seconds
    remaining = max_wait_seconds
    sleeper = sleep_func or asyncio.sleep
    attempts = 0
    while True:
        try:
            await event.answer(*args, **kwargs)
            return
        except FLOOD_WAIT_ERRORS as exc:
            attempts += 1
            wait_seconds = get_flood_wait_seconds(exc)
            remaining = min(remaining, deadline - time.monotonic())
            if attempts > 5 or wait_seconds > remaining or remaining <= 0:
                logger.debug(
                    "Telegram flood wait on event.answer (%s: %ss) exceeded total limit (remaining: %.1fs); proceeding",
                    type(exc).__name__,
                    wait_seconds,
                    remaining,
                )
                return
            await sleeper(wait_seconds)
            remaining -= wait_seconds
        except Exception:
            logger.debug("Failed to answer callback query (ignored so flow continues)", exc_info=True)
            return


async def _report_quota_error(progress: MessageProgressReporter, exc: Exception) -> None:
    """Out of credits / out of daily bandwidth get the contact button; a
    blocked user just gets the message."""
    if isinstance(exc, CreditsExhaustedException):
        await progress.update(texts.CREDITS_EXHAUSTED, buttons=_contact_buttons(), is_terminal=True)
    elif isinstance(exc, BandwidthExhaustedException):
        await progress.update(str(exc), buttons=_contact_buttons(), is_terminal=True)
    else:
        await progress.update(str(exc), is_terminal=True)


def register_handlers(
    client: TelegramClient,
    *,
    session_factory: sessionmaker,
    credits_service: CreditsService,
    free_download: int,
    pipeline: DownloadPipeline,
    archive_channel: str | None,
    upload_workers: int = 1,
    upload_connections: int = 1,
    max_download_size: int,
    limiter: ConcurrencyLimiter | None = None,
    force_ipv4: bool = False,
    youtube_cookies_file: str | None = None,
    potoken: str | None = None,
    video_cache_store: VideoCacheStore | None = None,
    youtube_player_client: str | None = None,
    youtube_js_runtimes: str | None = None,
    youtube_remote_components: str | None = None,
    health_tracker: ProviderHealthTracker | None = None,
    registry: ProviderRegistry | None = None,
    tiktok_cookies_file: str | None = None,
    instagram_cookies_file: str | None = None,
    retry_store: RetryStore | None = None,
) -> None:
    quality_store = QualitySelectionStore()
    if retry_store is None:
        retry_store = RetryStore()
    # Keyed by (chat_id, progress message id) - the same pair a CallbackQuery
    # event reports for the message its button sits under, so the cancel
    # handler needs no id encoded into callback_data at all.
    active_cancellations: dict[tuple[int, int], _ActiveRequest] = {}
    direct_engine = DirectEngine(max_download_size=max_download_size)
    if limiter is None:
        limiter = ConcurrencyLimiter(global_limit=100, per_user_limit=2)
    if video_cache_store is None:
        video_cache_store = VideoCacheStore(session_factory)
    if health_tracker is None:
        health_tracker = ProviderHealthTracker(session_factory)
    if registry is None:
        # An empty registry has no providers registered for any platform, so
        # get_providers_for_platform() always returns [] and TikTok/YouTube
        # fallback silently never leaves local yt-dlp. That is fine for tests
        # that don't exercise provider fallback, but it must never happen
        # quietly in a real deployment - see README "Provider registry
        # default" for the intentional-vs-accidental distinction.
        logger.warning(
            "register_handlers() called without a registry - external extraction "
            "providers (TikWM, ytmp3, cobalt, etc.) are DISABLED for this process; "
            "TikTok/YouTube downloads will only use local yt-dlp. Pass "
            "registry=build_provider_registry(settings) to enable provider fallback."
        )
        registry = ProviderRegistry()

    def settings_text(user_id: int, setting: Setting) -> str:
        # Read the balance from the DB on every render (the service keeps no
        # cache), so it reflects the latest charge. Hidden when credits do not
        # apply to this user (ENABLE_VIP off, or an owner): the balance is inf.
        body = settings_menu.describe_settings(setting)
        remaining = credits_service.get_total_credits(user_id)
        if not math.isfinite(remaining):
            return body
        return body + "\n\n" + texts.SETTINGS_CREDITS.format(credits=int(remaining))

    def load_delivery(user_id: int, *, first_name: str | None, username: str | None) -> DeliveryOptions:
        """Create the user row if needed and snapshot their saved settings."""
        with session_scope(session_factory) as session:
            user = settings_menu.get_or_create_user(
                session, user_id, first_name=first_name, username=username, free_download=free_download
            )
            return DeliveryOptions.from_setting(
                user.settings, user_display=_display_name(user.first_name, user.username)
            )

    def build_youtube_engine(quality: str, progress=None, **overrides) -> YouTubeEngine:
        return YouTubeEngine(
            quality=quality,
            max_download_size=max_download_size,
            progress=progress,
            force_ipv4=force_ipv4,
            cookies_file=youtube_cookies_file,
            po_token=potoken,
            player_client=youtube_player_client,
            js_runtimes=youtube_js_runtimes,
            remote_components=youtube_remote_components,
            registry=registry,
            health_tracker=health_tracker,
            **overrides,
        )

    def make_progress(
        message: Any,
        *,
        user_id: int,
        chat_id: int,
        url: str,
        platform: str,
        quality: str | None = None,
    ) -> MessageProgressReporter:
        def get_failure_buttons(text: str) -> Any:
            if is_hopeless_failure(text=text):
                return None
            msg = getattr(progress, "_message", message) if progress is not None else message
            msg_id = getattr(msg, "id", None)
            if msg_id is None:
                msg_id = getattr(message, "id", None)
            if msg_id is None:
                return None
            ctx = retry_store.put(
                user_id=user_id,
                chat_id=chat_id,
                message_id=msg_id,
                url=url,
                platform=platform,
                quality=quality,
            )
            return build_retry_markup(ctx.retry_id)

        progress: MessageProgressReporter | None = None
        progress = MessageProgressReporter(
            message,
            clear_buttons_on_terminal=True,
            get_failure_buttons=get_failure_buttons,
        )
        return progress

    async def _execute_download(
        *,
        job_user_id: int,
        job_chat_id: int,
        job_url: str,
        job_platform: str,
        job_quality: str | None = None,
        message: Any,
        progress: MessageProgressReporter,
        delivery: DeliveryOptions | None = None,
    ) -> None:
        try:
            if delivery is None:
                delivery = load_delivery(job_user_id, first_name=None, username=None)
            total_credits = credits_service.get_total_credits(job_user_id)

            if job_platform == "youtube":
                is_playlist = is_playlist_url(job_url)
                if is_playlist and math.isfinite(total_credits) and total_credits <= 0:
                    raise CreditsExhaustedException(texts.CREDITS_EXHAUSTED)
                playlist_limit = (
                    int(total_credits) if is_playlist and math.isfinite(total_credits) else None
                )
                engine = build_youtube_engine(
                    job_quality or "720",
                    progress=progress,
                    is_playlist=is_playlist,
                    playlist_item_limit=playlist_limit,
                    subtitles=delivery.subtitles,
                )
                media_ref = extract_video_id(job_url) or job_url
                cache_key = compute_cache_key(media_ref, job_quality or "720", delivery.send_as, delivery.subtitles)
            elif job_platform == "tiktok":
                engine = TikTokEngine(
                    registry=registry,
                    health_tracker=health_tracker,
                    max_download_size=max_download_size,
                    cookies_file=tiktok_cookies_file,
                    progress=progress,
                )
                cache_key = compute_cache_key(job_url, "tiktok", delivery.send_as)
            elif job_platform == "instagram":
                engine = InstagramEngine(
                    max_download_size=max_download_size,
                    cookies_file=instagram_cookies_file,
                    force_ipv4=force_ipv4,
                    progress=progress,
                )
                cache_key = compute_cache_key(
                    extract_instagram_id(job_url) or job_url, "instagram", delivery.send_as
                )
            else:  # direct
                engine = direct_engine
                cache_key = compute_cache_key(job_url, "direct", delivery.send_as)

            uploader = TelethonUploader(
                client, chat_id=job_chat_id, archive_channel=archive_channel,
                workers=upload_workers, connections=upload_connections,
                adaptive=True,
                on_flood=progress.handle_flood_wait,
            )

            async def on_wait() -> None:
                if progress is not None:
                    await progress.update(texts.YOUTUBE_QUEUE_WAIT, is_terminal=False)

            async with limiter.slot(job_user_id, on_wait=on_wait):
                await _run_cancellable(
                    active_cancellations,
                    chat_id=job_chat_id,
                    message=message,
                    owner_id=job_user_id,
                    progress=progress,
                    coro=pipeline.run(
                        user_id=job_user_id,
                        url=job_url,
                        engine=engine,
                        uploader=uploader,
                        progress=progress,
                        cache=video_cache_store,
                        cache_key=cache_key,
                        archive_channel=archive_channel,
                        delivery=delivery,
                        audio_only=(job_quality == "audio"),
                    ),
                )
        except (CreditsExhaustedException, BandwidthExhaustedException, UserBlockedException) as exc:
            msg_id = getattr(getattr(progress, "_message", message), "id", None)
            if msg_id is not None:
                retry_store.remove((job_chat_id, msg_id))
            if progress is not None:
                await _report_quota_error(progress, exc)
        except (YouTubeDownloadError, TikTokDownloadError, InstagramDownloadError, DownloadTooLargeError, UnsupportedUrlError) as exc:
            if not getattr(progress, "is_terminal_completed", False):
                await progress.update(str(exc), is_terminal=True)
        except FLOOD_WAIT_ERRORS as exc:
            wait_seconds = get_flood_wait_seconds(exc)
            logger.warning("Download aborted due to %s (%ss) for url=%s", type(exc).__name__, wait_seconds, job_url)
            if not getattr(progress, "is_terminal_completed", False):
                await progress.update(texts.FLOOD_WAIT_FAILED, is_terminal=True)
        except TimeoutError:
            if not getattr(progress, "is_terminal_completed", False):
                try:
                    await progress.update(texts.REQUEST_TIMEOUT_EXCEEDED, is_terminal=True)
                except Exception:
                    logger.debug("Failed to update progress on timeout", exc_info=True)
        except Exception:
            logger.exception("Download failed for url=%s", job_url)
            if not getattr(progress, "is_terminal_completed", False):
                try:
                    await progress.update(texts.DOWNLOAD_FAILED, is_terminal=True)
                except Exception:
                    logger.debug("Failed to update progress on general failure", exc_info=True)

    @client.on(events.NewMessage(pattern="/start"))
    async def start_handler(event: events.NewMessage.Event) -> None:
        first_name, username = _sender_info(event)
        with session_scope(session_factory) as session:
            settings_menu.get_or_create_user(
                session, event.sender_id, first_name=first_name, username=username, free_download=free_download
            )
        await call_with_flood_retry(event.respond, texts.START, link_preview=False)

    @client.on(events.NewMessage(pattern="/help"))
    async def help_handler(event: events.NewMessage.Event) -> None:
        await call_with_flood_retry(
            event.respond,
            texts.HELP,
            link_preview=False,
            buttons=[[Button.url(texts.CONTACT_BUTTON, texts.CONTACT_URL)]],
        )

    @client.on(events.NewMessage(pattern="/about"))
    async def about_handler(event: events.NewMessage.Event) -> None:
        await call_with_flood_retry(event.respond, texts.ABOUT)

    @client.on(events.NewMessage(pattern="/ping"))
    async def ping_handler(event: events.NewMessage.Event) -> None:
        start = time.monotonic()
        message = await call_with_flood_retry(event.respond, texts.PING_MESSAGE)
        elapsed_ms = round((time.monotonic() - start) * 1000, 2)
        await call_with_flood_retry(message.edit, texts.PING_RESULT.format(ms=elapsed_ms))

    @client.on(events.NewMessage(pattern="/settings"))
    async def settings_handler(event: events.NewMessage.Event) -> None:
        first_name, username = _sender_info(event)
        with session_scope(session_factory) as session:
            user = settings_menu.get_or_create_user(
                session, event.sender_id, first_name=first_name, username=username, free_download=free_download
            )
            buttons = settings_menu.build_settings_buttons(user.settings)
            text = settings_text(event.sender_id, user.settings)
        await call_with_flood_retry(event.respond, text, buttons=buttons)

    @client.on(events.CallbackQuery(pattern=rb"^toggle_"))
    async def toggle_handler(event: events.CallbackQuery.Event) -> None:
        toggle_key = decode(event.data)[0]
        with session_scope(session_factory) as session:
            user = settings_menu.get_or_create_user(
                session, event.sender_id, first_name=None, username=None, free_download=free_download
            )
            answer = settings_menu.apply_toggle(user.settings, toggle_key)
            buttons = settings_menu.build_settings_buttons(user.settings)
            text = settings_text(event.sender_id, user.settings)
        # The old bot popped an alert for the title-length toggle since its
        # answer text explains a behavior change (separate message/Telegraph
        # link), not just a value flip - a toast is easy to miss for that.
        alert = toggle_key == settings_menu.TOGGLE_TITLE_LEN
        await _safe_answer_callback(event, answer, alert=alert)
        try:
            await call_with_flood_retry(event.edit, text, buttons=buttons)
        except MessageNotModifiedError:
            pass  # content unchanged (e.g. same toggle value) - nothing to surface

    @client.on(events.CallbackQuery(pattern=rb"^ytq:"))
    async def quality_pick_handler(event: events.CallbackQuery.Event) -> None:
        parts = decode(event.data)
        if len(parts) != 3 or parts[0] != "ytq":
            await _safe_answer_callback(event)
            return

        quality = parts[1]
        url_hash = parts[2]
        url = quality_store.get(url_hash)
        if not url:
            await _safe_answer_callback(event, texts.YOUTUBE_LINK_EXPIRED, alert=True)
            return

        progress: MessageProgressReporter | None = None
        try:
            delivery = load_delivery(event.sender_id, first_name=None, username=None)

            # Edit the menu message itself into the progress message (one message
            # per download, no new one), and confirm with a short toast.
            quality_name = texts.QUALITY_NAMES.get(quality, quality)
            if quality == "audio":
                toast = texts.QUALITY_TOAST_AUDIO
                status_text = texts.DOWNLOADING_AUDIO
            else:
                toast = texts.QUALITY_TOAST.format(name=quality_name)
                status_text = texts.DOWNLOADING_QUALITY.format(name=quality_name)
            await _safe_answer_callback(event, toast)

            menu_deadline = time.monotonic() + MENU_STATUS_TIMEOUT_SECONDS
            message = None
            rem = max(0.0, menu_deadline - time.monotonic())
            try:
                message = await call_with_flood_retry(
                    event.edit, status_text, buttons=_cancel_markup(), max_wait_seconds=rem, max_total_wait_seconds=rem
                )
            except MessageNotModifiedError:
                try:
                    rem = max(0.0, menu_deadline - time.monotonic())
                    message = await call_with_flood_retry(
                        event.get_message, max_wait_seconds=rem, max_total_wait_seconds=rem
                    )
                except (RPCError, ConnectionError, TimeoutError, OSError):
                    logger.debug("Failed to fetch message on MessageNotModifiedError", exc_info=True)
                    message = getattr(event, "message", None)
            except (RPCError, ConnectionError, TimeoutError, OSError):
                logger.debug("Failed to edit the quality menu message", exc_info=True)
            if message is None:
                rem = max(0.0, menu_deadline - time.monotonic())
                if rem > 0:
                    try:
                        message = await call_with_flood_retry(
                            event.respond,
                            status_text,
                            buttons=_cancel_markup(),
                            max_wait_seconds=rem,
                            max_total_wait_seconds=rem,
                        )
                    except Exception:
                        logger.debug("Failed to send new status message after edit failure", exc_info=True)
                        message = getattr(event, "message", None) or event
                else:
                    message = getattr(event, "message", None) or event
            progress = make_progress(
                message,
                user_id=event.sender_id,
                chat_id=event.chat_id,
                url=url,
                platform="youtube",
                quality=quality,
            )
            await _execute_download(
                job_user_id=event.sender_id,
                job_chat_id=event.chat_id,
                job_url=url,
                job_platform="youtube",
                job_quality=quality,
                message=message,
                progress=progress,
                delivery=delivery,
            )
        except Exception:
            logger.exception("Early failure in quality pick for url=%s", url)
            if progress is not None:
                if not getattr(progress, "is_terminal_completed", False):
                    try:
                        await progress.update(texts.DOWNLOAD_FAILED, is_terminal=True)
                    except Exception:
                        logger.debug("Failed to update progress on general failure", exc_info=True)
            else:
                await _safe_answer_callback(event, texts.DOWNLOAD_FAILED, alert=True)
                try:
                    await call_with_flood_retry(event.edit, texts.DOWNLOAD_FAILED, buttons=None)
                except (RPCError, ConnectionError, TimeoutError, OSError):
                    try:
                        await call_with_flood_retry(event.respond, texts.DOWNLOAD_FAILED)
                    except (RPCError, ConnectionError, TimeoutError, OSError):
                        logger.debug("Failed to report error after early failure in quality pick", exc_info=True)

    @client.on(events.CallbackQuery(pattern=rb"^cancel$"))
    async def cancel_handler(event: events.CallbackQuery.Event) -> None:
        active = active_cancellations.get((event.chat_id, event.message_id))
        if active is None:
            await _safe_answer_callback(event, texts.CANCEL_NOT_ACTIVE, alert=True)
            return
        if active.owner_id != event.sender_id:
            await _safe_answer_callback(event, texts.CANCEL_NOT_OWNER, alert=True)
            return
        active.task.cancel()
        await _safe_answer_callback(event, texts.CANCEL_TOAST)
        if not active.progress.is_terminal_completed:
            try:
                await active.progress.update(texts.REQUEST_CANCELLED, is_terminal=True)
            except Exception:
                logger.debug("Failed to update progress on cancel button press", exc_info=True)

    @client.on(events.CallbackQuery(pattern=rb"^retry(:|$)"))
    async def retry_handler(event: events.CallbackQuery.Event) -> None:
        if getattr(event, "is_private", None) is False:
            return

        parts = decode(event.data)
        retry_id = parts[1] if len(parts) > 1 else None
        retry_ctx = retry_store.get(retry_id) if retry_id else None
        if retry_ctx is None:
            msg_id = getattr(event, "message_id", None) or getattr(getattr(event, "message", None), "id", None)
            if msg_id is not None:
                retry_ctx = retry_store.get((event.chat_id, msg_id))

        if retry_ctx is None:
            await _safe_answer_callback(event, texts.RETRY_NOT_AVAILABLE, alert=True)
            return

        if retry_ctx.user_id != event.sender_id:
            await _safe_answer_callback(event, texts.RETRY_NOT_OWNER, alert=True)
            return

        if retry_ctx.is_running:
            await _safe_answer_callback(event, texts.RETRY_ALREADY_RUNNING, alert=True)
            return

        retry_ctx.is_running = True
        await _safe_answer_callback(event)

        message = None
        try:
            message = await call_with_flood_retry(
                event.edit, texts.DOWNLOAD_STARTED, buttons=_cancel_markup()
            )
        except MessageNotModifiedError:
            try:
                message = await call_with_flood_retry(event.get_message)
            except (RPCError, ConnectionError, TimeoutError, OSError):
                message = getattr(event, "message", None)
        except (RPCError, ConnectionError, TimeoutError, OSError):
            logger.debug("Failed to edit failure message on retry", exc_info=True)

        if message is None:
            try:
                await call_with_flood_retry(event.edit, buttons=None)
            except (RPCError, ConnectionError, TimeoutError, OSError):
                logger.debug("Failed to remove buttons on retry message", exc_info=True)
            try:
                message = await call_with_flood_retry(
                    event.respond, texts.DOWNLOAD_STARTED, buttons=_cancel_markup()
                )
            except (RPCError, ConnectionError, TimeoutError, OSError):
                logger.debug("Failed to send replacement status message on retry", exc_info=True)
                message = getattr(event, "message", None) or event

        old_id = retry_ctx.retry_id
        old_url = retry_ctx.url
        old_platform = retry_ctx.platform
        old_quality = retry_ctx.quality
        old_user_id = retry_ctx.user_id
        old_chat_id = retry_ctx.chat_id

        progress = make_progress(
            message,
            user_id=old_user_id,
            chat_id=old_chat_id,
            url=old_url,
            platform=old_platform,
            quality=old_quality,
        )

        try:
            await _execute_download(
                job_user_id=old_user_id,
                job_chat_id=old_chat_id,
                job_url=old_url,
                job_platform=old_platform,
                job_quality=old_quality,
                message=message,
                progress=progress,
            )
        finally:
            retry_store.remove(old_id)

    # Private chats only, like the old bot: a link posted in a group is ignored.
    @client.on(events.NewMessage(func=lambda e: bool(getattr(e, "is_private", False))))
    async def url_handler(event: events.NewMessage.Event) -> None:
        raw_text = event.raw_text or ""
        if raw_text.startswith("/"):
            return
        scheme_match = re.match(r"^([a-zA-Z0-9+.-]+)://", raw_text.strip())
        if scheme_match and scheme_match.group(1).lower() not in ("http", "https"):
            await call_with_flood_retry(event.respond, texts.UNSUPPORTED_URL)
            return
        match = URL_RE.search(raw_text)
        if not match:
            return
        url = match.group(0)

        progress: MessageProgressReporter | None = None
        try:
            first_name, username = _sender_info(event)
            delivery = load_delivery(event.sender_id, first_name=first_name, username=username)

            if _host_matches(url, YOUTUBE_HOSTS):
                url_hash = quality_store.put(url)
                probe_engine = build_youtube_engine("720")
                title, duration = await fetch_title_duration(
                    url, opts=probe_engine.info_opts(url), timeout=MENU_LOOKUP_TIMEOUT_SECONDS
                )
                await call_with_flood_retry(
                    event.respond,
                    texts.YOUTUBE_QUALITY_SELECT.format(
                        title=(title or "סרטון יוטיוב").translate(_MARKDOWN_SPECIALS),
                        duration=duration or "לא ידוע",
                    ),
                    buttons=build_quality_markup(url_hash, default=delivery.default_quality),
                )
                return
            if _host_matches(url, TIKTOK_HOSTS):
                message = await call_with_flood_retry(
                    event.respond, texts.DOWNLOAD_STARTED, buttons=_cancel_markup()
                )
                progress = make_progress(
                    message,
                    user_id=event.sender_id,
                    chat_id=event.chat_id,
                    url=url,
                    platform="tiktok",
                )
                await _execute_download(
                    job_user_id=event.sender_id,
                    job_chat_id=event.chat_id,
                    job_url=url,
                    job_platform="tiktok",
                    message=message,
                    progress=progress,
                )
                return
            if _host_matches(url, INSTAGRAM_HOSTS):
                if not matches_instagram_url(url):
                    await call_with_flood_retry(event.respond, texts.UNSUPPORTED_URL)
                    return

                message = await call_with_flood_retry(
                    event.respond, texts.DOWNLOAD_STARTED, buttons=_cancel_markup()
                )
                progress = make_progress(
                    message,
                    user_id=event.sender_id,
                    chat_id=event.chat_id,
                    url=url,
                    platform="instagram",
                )
                await _execute_download(
                    job_user_id=event.sender_id,
                    job_chat_id=event.chat_id,
                    job_url=url,
                    job_platform="instagram",
                    message=message,
                    progress=progress,
                )
                return

            if not direct_engine.matches(url):
                await call_with_flood_retry(event.respond, texts.UNSUPPORTED_URL)
                return

            message = await call_with_flood_retry(
                event.respond, texts.DOWNLOAD_STARTED, buttons=_cancel_markup()
            )
            progress = make_progress(
                message,
                user_id=event.sender_id,
                chat_id=event.chat_id,
                url=url,
                platform="direct",
            )
            await _execute_download(
                job_user_id=event.sender_id,
                job_chat_id=event.chat_id,
                job_url=url,
                job_platform="direct",
                message=message,
                progress=progress,
            )
        except Exception:
            logger.exception("Handler failed for url=%s", url)
            if progress is not None:
                if not getattr(progress, "is_terminal_completed", False):
                    try:
                        await progress.update(texts.DOWNLOAD_FAILED, is_terminal=True)
                    except Exception:
                        logger.debug("Failed to update progress on general handler failure", exc_info=True)
            else:
                try:
                    await call_with_flood_retry(event.respond, texts.DOWNLOAD_FAILED)
                except Exception:
                    logger.debug("Failed to send error response on handler failure", exc_info=True)
