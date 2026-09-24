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
from media_bot_v2.engines.tiktok import TikTokDownloadError, TikTokEngine
from media_bot_v2.engines.youtube import (
    YouTubeDownloadError,
    YouTubeEngine,
    extract_video_id,
    is_playlist_url,
)
from media_bot_v2.pipeline import DownloadPipeline
from media_bot_v2.providers.health import ProviderHealthTracker
from media_bot_v2.providers.registry import ProviderRegistry
from media_bot_v2.queue.limiter import ConcurrencyLimiter
from media_bot_v2.telegram import settings_menu, texts
from media_bot_v2.telegram.callback_data import decode
from media_bot_v2.telegram.progress import MessageProgressReporter
from media_bot_v2.telegram.quality_menu import QualitySelectionStore, build_quality_markup
from media_bot_v2.telegram.uploader import TelethonUploader

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://\S+")
YOUTUBE_HOSTS = ("youtube.com", "youtu.be")
TIKTOK_HOSTS = ("tiktok.com",)
INSTAGRAM_HOSTS = ("instagram.com",)


def _host_matches(url: str, hosts: tuple[str, ...]) -> bool:
    return any(host in url.lower() for host in hosts)


def _sender_info(event) -> tuple[str | None, str | None]:
    sender = event.sender
    return getattr(sender, "first_name", None), getattr(sender, "username", None)


def register_handlers(
    client: TelegramClient,
    *,
    session_factory: sessionmaker,
    credits_service: CreditsService,
    free_download: int,
    pipeline: DownloadPipeline,
    archive_channel: str | None,
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

    @client.on(events.NewMessage(pattern="/start"))
    async def start_handler(event: events.NewMessage.Event) -> None:
        first_name, username = _sender_info(event)
        with session_scope(session_factory) as session:
            settings_menu.get_or_create_user(
                session, event.sender_id, first_name=first_name, username=username, free_download=free_download
            )
        await event.respond(texts.START, link_preview=False)

    @client.on(events.NewMessage(pattern="/help"))
    async def help_handler(event: events.NewMessage.Event) -> None:
        await event.respond(
            texts.HELP,
            link_preview=False,
            buttons=[[Button.url("לצ'אט איתי 💬", "https://t.me/YD_IL")]],
        )

    @client.on(events.NewMessage(pattern="/about"))
    async def about_handler(event: events.NewMessage.Event) -> None:
        await event.respond(texts.ABOUT)

    @client.on(events.NewMessage(pattern="/ping"))
    async def ping_handler(event: events.NewMessage.Event) -> None:
        start = time.monotonic()
        message = await event.respond(texts.PING_MESSAGE)
        elapsed_ms = round((time.monotonic() - start) * 1000, 2)
        await message.edit(texts.PING_RESULT.format(ms=elapsed_ms))

    @client.on(events.NewMessage(pattern="/settings"))
    async def settings_handler(event: events.NewMessage.Event) -> None:
        first_name, username = _sender_info(event)
        with session_scope(session_factory) as session:
            user = settings_menu.get_or_create_user(
                session, event.sender_id, first_name=first_name, username=username, free_download=free_download
            )
            buttons = settings_menu.build_settings_buttons(user.settings)
        await event.respond(texts.SETTINGS, buttons=buttons)

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
        await event.answer(answer, alert=alert)
        try:
            await event.edit(texts.SETTINGS, buttons=buttons)
        except MessageNotModifiedError:
            pass  # content unchanged (e.g. same toggle value) - nothing to surface

    @client.on(events.CallbackQuery(pattern=rb"^ytq:"))
    async def quality_pick_handler(event: events.CallbackQuery.Event) -> None:
        parts = decode(event.data)
        if len(parts) != 3 or parts[0] != "ytq":
            await event.answer()
            return

        quality = parts[1]
        url_hash = parts[2]
        url = quality_store.get(url_hash)
        if not url:
            await event.answer(texts.YOUTUBE_LINK_EXPIRED, alert=True)
            return

        with session_scope(session_factory) as session:
            settings_menu.get_or_create_user(
                session, event.sender_id, first_name=None, username=None, free_download=free_download
            )

        await event.answer()
        try:
            await event.edit(buttons=None)
        except MessageNotModifiedError:
            pass
        except (RPCError, ConnectionError, TimeoutError, OSError):
            logger.debug("Failed to clear quality buttons", exc_info=True)

        message = await event.respond(texts.DOWNLOAD_STARTED)
        progress = MessageProgressReporter(message)
        uploader = TelethonUploader(client, chat_id=event.chat_id, archive_channel=archive_channel)

        try:
            is_playlist = is_playlist_url(url)
            total_credits = credits_service.get_total_credits(event.sender_id)
            if is_playlist and math.isfinite(total_credits) and total_credits <= 0:
                raise CreditsExhaustedException("הקרדיטים שלך נגמרו.")

            playlist_limit = (
                int(total_credits) if is_playlist and math.isfinite(total_credits) else None
            )

            engine = YouTubeEngine(
                quality=quality,
                max_download_size=max_download_size,
                progress=progress,
                force_ipv4=force_ipv4,
                cookies_file=youtube_cookies_file,
                po_token=potoken,
                is_playlist=is_playlist,
                playlist_item_limit=playlist_limit,
                player_client=youtube_player_client,
                js_runtimes=youtube_js_runtimes,
                remote_components=youtube_remote_components,
                registry=registry,
                health_tracker=health_tracker,
            )

            media_ref = extract_video_id(url) or url
            cache_key = compute_cache_key(media_ref, quality)

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
                )
        except (CreditsExhaustedException, BandwidthExhaustedException, UserBlockedException) as exc:
            await progress.update(str(exc))
        except (YouTubeDownloadError, DownloadTooLargeError, UnsupportedUrlError) as exc:
            await progress.update(str(exc))
        except TimeoutError:
            pass
        except Exception:
            logger.exception("YouTube download failed for url=%s", url)

    @client.on(events.NewMessage())
    async def url_handler(event: events.NewMessage.Event) -> None:
        raw_text = event.raw_text or ""
        if raw_text.startswith("/"):
            return
        scheme_match = re.match(r"^([a-zA-Z0-9+.-]+)://", raw_text.strip())
        if scheme_match and scheme_match.group(1).lower() not in ("http", "https"):
            await event.respond(texts.UNSUPPORTED_URL)
            return
        match = URL_RE.search(raw_text)
        if not match:
            return
        url = match.group(0)

        first_name, username = _sender_info(event)
        with session_scope(session_factory) as session:
            settings_menu.get_or_create_user(
                session, event.sender_id, first_name=first_name, username=username, free_download=free_download
            )

        if _host_matches(url, YOUTUBE_HOSTS):
            url_hash = quality_store.put(url)
            await event.respond(
                texts.YOUTUBE_QUALITY_SELECT.format(title="סרטון יוטיוב", duration="לא ידוע"),
                buttons=build_quality_markup(url_hash),
            )
            return
        if _host_matches(url, TIKTOK_HOSTS):
            message = await event.respond(texts.DOWNLOAD_STARTED)
            progress = MessageProgressReporter(message)
            uploader = TelethonUploader(client, chat_id=event.chat_id, archive_channel=archive_channel)

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
                        cache_key=compute_cache_key(url, "tiktok"),
                        archive_channel=archive_channel,
                    )
            except (CreditsExhaustedException, BandwidthExhaustedException, UserBlockedException) as exc:
                await progress.update(str(exc))
            except (TikTokDownloadError, DownloadTooLargeError, UnsupportedUrlError) as exc:
                await progress.update(str(exc))
            except TimeoutError:
                pass
            except Exception:
                logger.exception("TikTok download failed for url=%s", url)
            return
        if _host_matches(url, INSTAGRAM_HOSTS):
            await event.respond(texts.INSTAGRAM_NOT_YET_IMPLEMENTED)
            return

        if not direct_engine.matches(url):
            await event.respond(texts.UNSUPPORTED_URL)
            return

        message = await event.respond(texts.DOWNLOAD_STARTED)
        progress = MessageProgressReporter(message)
        uploader = TelethonUploader(client, chat_id=event.chat_id, archive_channel=archive_channel)

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
                    cache_key=compute_cache_key(url, "direct"),
                    archive_channel=archive_channel,
                )
        except (CreditsExhaustedException, BandwidthExhaustedException, UserBlockedException) as exc:
            await progress.update(str(exc))
        except (DownloadTooLargeError, UnsupportedUrlError) as exc:
            await progress.update(str(exc))
        except TimeoutError:
            pass
        except Exception:
            logger.exception("Direct download failed for url=%s", url)
