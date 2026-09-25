"""Request router: Telethon event handlers wired to commands, the settings
menu, the YouTube quality-select menu (UI only - the YouTube engine itself
lands in M2), and the direct-link download pipeline, which is the one
engine implemented end-to-end in M1 (spec/SPEC.md M1 item 7).
"""

from __future__ import annotations

import logging
import math
import re
import time
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
from media_bot_v2.telegram.callback_data import decode
from media_bot_v2.telegram.delivery import DeliveryOptions
from media_bot_v2.telegram.flood_wait import (
    FLOOD_WAIT_ERRORS,
    call_with_flood_retry,
    get_flood_wait_seconds,
)
from media_bot_v2.telegram.progress import MessageProgressReporter
from media_bot_v2.telegram.quality_menu import QualitySelectionStore, build_quality_markup
from media_bot_v2.telegram.uploader import TelethonUploader

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://\S+")
YOUTUBE_HOSTS = ("youtube.com", "youtu.be")
TIKTOK_HOSTS = ("tiktok.com", "douyin.com")
INSTAGRAM_HOSTS = ("instagram.com", "instagr.am")

# The quality menu waits at most this long for the real title/duration; past
# it the menu goes out with its placeholders (see fetch_title_duration).
MENU_LOOKUP_TIMEOUT_SECONDS = 8.0
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


async def _report_quota_error(progress: MessageProgressReporter, exc: Exception) -> None:
    """Out of credits / out of daily bandwidth get the contact button; a
    blocked user just gets the message."""
    if isinstance(exc, CreditsExhaustedException):
        await progress.update(texts.CREDITS_EXHAUSTED, buttons=_contact_buttons())
    elif isinstance(exc, BandwidthExhaustedException):
        await progress.update(str(exc), buttons=_contact_buttons())
    else:
        await progress.update(str(exc))


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
) -> None:
    quality_store = QualitySelectionStore()
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

    def settings_text(user_id: int) -> str:
        # Read the balance from the DB on every render (the service keeps no
        # cache), so it reflects the latest charge. Hidden when credits do not
        # apply to this user (ENABLE_VIP off, or an owner): the balance is inf.
        remaining = credits_service.get_total_credits(user_id)
        if not math.isfinite(remaining):
            return texts.SETTINGS
        return texts.SETTINGS + texts.SETTINGS_CREDITS.format(credits=int(remaining))

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
        await call_with_flood_retry(event.respond, settings_text(event.sender_id), buttons=buttons)

    @client.on(events.CallbackQuery(pattern=rb"^toggle_"))
    async def toggle_handler(event: events.CallbackQuery.Event) -> None:
        toggle_key = decode(event.data)[0]
        with session_scope(session_factory) as session:
            user = settings_menu.get_or_create_user(
                session, event.sender_id, first_name=None, username=None, free_download=free_download
            )
            answer = settings_menu.apply_toggle(user.settings, toggle_key)
            buttons = settings_menu.build_settings_buttons(user.settings)
        # The old bot popped an alert for the title-length toggle since its
        # answer text explains a behavior change (separate message/Telegraph
        # link), not just a value flip - a toast is easy to miss for that.
        alert = toggle_key == settings_menu.TOGGLE_TITLE_LEN
        await call_with_flood_retry(event.answer, answer, alert=alert)
        try:
            await call_with_flood_retry(event.edit, settings_text(event.sender_id), buttons=buttons)
        except MessageNotModifiedError:
            pass  # content unchanged (e.g. same toggle value) - nothing to surface

    @client.on(events.CallbackQuery(pattern=rb"^ytq:"))
    async def quality_pick_handler(event: events.CallbackQuery.Event) -> None:
        parts = decode(event.data)
        if len(parts) != 3 or parts[0] != "ytq":
            await call_with_flood_retry(event.answer)
            return

        quality = parts[1]
        url_hash = parts[2]
        url = quality_store.get(url_hash)
        if not url:
            await call_with_flood_retry(event.answer, texts.YOUTUBE_LINK_EXPIRED, alert=True)
            return

        delivery = load_delivery(event.sender_id, first_name=None, username=None)

        # Edit the menu message itself into the progress message (one message
        # per download, no new one), and confirm with a short toast.
        quality_name = texts.QUALITY_NAMES.get(quality, quality)
        if quality == "audio":
            await call_with_flood_retry(event.answer, texts.QUALITY_TOAST_AUDIO)
            status_text = texts.DOWNLOADING_AUDIO
        else:
            await call_with_flood_retry(event.answer, texts.QUALITY_TOAST.format(name=quality_name))
            status_text = texts.DOWNLOADING_QUALITY.format(name=quality_name)
        message = None
        try:
            message = await call_with_flood_retry(event.edit, status_text, buttons=None)
        except MessageNotModifiedError:
            message = await event.get_message()
        except (RPCError, ConnectionError, TimeoutError, OSError):
            logger.debug("Failed to edit the quality menu message", exc_info=True)
        if message is None:
            message = await call_with_flood_retry(event.respond, status_text)
        progress = MessageProgressReporter(message)
        uploader = TelethonUploader(
            client, chat_id=event.chat_id, archive_channel=archive_channel,
            workers=upload_workers, connections=upload_connections,
            adaptive=True,
        )

        try:
            is_playlist = is_playlist_url(url)
            total_credits = credits_service.get_total_credits(event.sender_id)
            if is_playlist and math.isfinite(total_credits) and total_credits <= 0:
                raise CreditsExhaustedException(texts.CREDITS_EXHAUSTED)

            playlist_limit = (
                int(total_credits) if is_playlist and math.isfinite(total_credits) else None
            )

            engine = build_youtube_engine(
                quality,
                progress=progress,
                is_playlist=is_playlist,
                playlist_item_limit=playlist_limit,
                subtitles=delivery.subtitles,
            )

            media_ref = extract_video_id(url) or url
            cache_key = compute_cache_key(media_ref, quality, delivery.send_as, delivery.subtitles)

            async def on_wait() -> None:
                await progress.update(texts.YOUTUBE_QUEUE_WAIT)

            async with limiter.slot(event.sender_id, on_wait=on_wait):
                await pipeline.run(
                    user_id=event.sender_id,
                    url=url,
                    engine=engine,
                    uploader=uploader,
                    progress=progress,
                    cache=video_cache_store,
                    cache_key=cache_key,
                    archive_channel=archive_channel,
                    delivery=delivery,
                )
        except (CreditsExhaustedException, BandwidthExhaustedException, UserBlockedException) as exc:
            await _report_quota_error(progress, exc)
        except (YouTubeDownloadError, DownloadTooLargeError, UnsupportedUrlError) as exc:
            await progress.update(str(exc))
        except FLOOD_WAIT_ERRORS as exc:
            wait_seconds = get_flood_wait_seconds(exc)
            logger.warning("YouTube download aborted due to %s (%ss) for url=%s", type(exc).__name__, wait_seconds, url)
            await progress.update(texts.FLOOD_WAIT_FAILED)
        except TimeoutError:
            pass
        except Exception:
            logger.exception("YouTube download failed for url=%s", url)

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
            message = await call_with_flood_retry(event.respond, texts.DOWNLOAD_STARTED)
            progress = MessageProgressReporter(message)
            uploader = TelethonUploader(
                client, chat_id=event.chat_id, archive_channel=archive_channel,
                workers=upload_workers, connections=upload_connections,
                adaptive=True,
            )

            async def on_wait() -> None:
                await progress.update(texts.YOUTUBE_QUEUE_WAIT)

            tiktok_engine = TikTokEngine(
                registry=registry,
                health_tracker=health_tracker,
                max_download_size=max_download_size,
                cookies_file=tiktok_cookies_file,
                progress=progress,
            )

            try:
                async with limiter.slot(event.sender_id, on_wait=on_wait):
                    await pipeline.run(
                        user_id=event.sender_id,
                        url=url,
                        engine=tiktok_engine,
                        uploader=uploader,
                        progress=progress,
                        cache=video_cache_store,
                        cache_key=compute_cache_key(url, "tiktok", delivery.send_as),
                        archive_channel=archive_channel,
                        delivery=delivery,
                    )
            except (CreditsExhaustedException, BandwidthExhaustedException, UserBlockedException) as exc:
                await _report_quota_error(progress, exc)
            except (TikTokDownloadError, DownloadTooLargeError, UnsupportedUrlError) as exc:
                await progress.update(str(exc))
            except FLOOD_WAIT_ERRORS as exc:
                wait_seconds = get_flood_wait_seconds(exc)
                logger.warning("TikTok download aborted due to %s (%ss) for url=%s", type(exc).__name__, wait_seconds, url)
                await progress.update(texts.FLOOD_WAIT_FAILED)
            except TimeoutError:
                pass
            except Exception:
                logger.exception("TikTok download failed for url=%s", url)
            return
        if _host_matches(url, INSTAGRAM_HOSTS):
            if not matches_instagram_url(url):
                await call_with_flood_retry(event.respond, texts.UNSUPPORTED_URL)
                return

            message = await call_with_flood_retry(event.respond, texts.DOWNLOAD_STARTED)
            progress = MessageProgressReporter(message)
            uploader = TelethonUploader(
                client, chat_id=event.chat_id, archive_channel=archive_channel,
                workers=upload_workers, connections=upload_connections,
                adaptive=True,
            )

            async def on_wait() -> None:
                await progress.update(texts.YOUTUBE_QUEUE_WAIT)

            instagram_engine = InstagramEngine(
                max_download_size=max_download_size,
                cookies_file=instagram_cookies_file,
                force_ipv4=force_ipv4,
                progress=progress,
            )

            try:
                async with limiter.slot(event.sender_id, on_wait=on_wait):
                    await pipeline.run(
                        user_id=event.sender_id,
                        url=url,
                        engine=instagram_engine,
                        uploader=uploader,
                        progress=progress,
                        cache=video_cache_store,
                        cache_key=compute_cache_key(extract_instagram_id(url) or url, "instagram", delivery.send_as),
                        archive_channel=archive_channel,
                        delivery=delivery,
                    )
            except (CreditsExhaustedException, BandwidthExhaustedException, UserBlockedException) as exc:
                await _report_quota_error(progress, exc)
            except (InstagramDownloadError, DownloadTooLargeError, UnsupportedUrlError) as exc:
                await progress.update(str(exc))
            except FLOOD_WAIT_ERRORS as exc:
                wait_seconds = get_flood_wait_seconds(exc)
                logger.warning("Instagram download aborted due to %s (%ss) for url=%s", type(exc).__name__, wait_seconds, url)
                await progress.update(texts.FLOOD_WAIT_FAILED)
            except TimeoutError:
                pass
            except Exception:
                logger.exception("Instagram download failed for url=%s", url)
            return

        if not direct_engine.matches(url):
            await call_with_flood_retry(event.respond, texts.UNSUPPORTED_URL)
            return

        message = await call_with_flood_retry(event.respond, texts.DOWNLOAD_STARTED)
        progress = MessageProgressReporter(message)
        uploader = TelethonUploader(
            client, chat_id=event.chat_id, archive_channel=archive_channel,
            workers=upload_workers, connections=upload_connections,
            adaptive=True,
        )

        async def on_wait() -> None:
            await progress.update(texts.YOUTUBE_QUEUE_WAIT)

        try:
            async with limiter.slot(event.sender_id, on_wait=on_wait):
                await pipeline.run(
                    user_id=event.sender_id,
                    url=url,
                    engine=direct_engine,
                    uploader=uploader,
                    progress=progress,
                    cache=video_cache_store,
                    cache_key=compute_cache_key(url, "direct", delivery.send_as),
                    archive_channel=archive_channel,
                    delivery=delivery,
                )
        except (CreditsExhaustedException, BandwidthExhaustedException, UserBlockedException) as exc:
            await _report_quota_error(progress, exc)
        except (DownloadTooLargeError, UnsupportedUrlError) as exc:
            await progress.update(str(exc))
        except FLOOD_WAIT_ERRORS as exc:
            wait_seconds = get_flood_wait_seconds(exc)
            logger.warning("Direct download aborted due to %s (%ss) for url=%s", type(exc).__name__, wait_seconds, url)
            await progress.update(texts.FLOOD_WAIT_FAILED)
        except TimeoutError:
            pass
        except Exception:
            logger.exception("Direct download failed for url=%s", url)
