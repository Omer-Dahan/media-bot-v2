"""The four saved settings must change what the bot does (M7a step 3), and
the owner's router decisions (private chats only, contact button, quality
menu edited in place with the real title and duration) must hold.

Router tests drive the real handlers with fake events and assert on what the
handlers pass to the pipeline / say to the user."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telethon import events

from media_bot_v2.cache.video_cache import compute_cache_key
from media_bot_v2.credits.exceptions import BandwidthExhaustedException
from media_bot_v2.db.models import Base
from media_bot_v2.engines.youtube import YouTubeEngine, _result_from_info
from media_bot_v2.telegram import settings_menu, texts
from media_bot_v2.telegram.delivery import DeliveryOptions
from media_bot_v2.telegram.quality_menu import build_quality_markup
from tests.test_router import (
    _FakeCallbackEvent,
    _FakeEvent,
    _find_url_handler,
    _find_ytq_handler,
    _make_router,
    _QualityEvent,
)

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _save_settings(session_factory, user_id=1, **fields):
    with session_factory() as session:
        user = settings_menu.get_or_create_user(
            session, user_id, first_name="Test User", username="testuser", free_download=50
        )
        for name, value in fields.items():
            setattr(user.settings, name, value)
        session.commit()


# --------------------------------------------------------------------------
# settings reach the pipeline
# --------------------------------------------------------------------------
async def test_direct_link_carries_the_saved_settings_and_a_format_aware_cache_key(session_factory):
    _save_settings(session_factory, format="document", subtitles=1, title_length=4000, quality="medium")
    pipeline = AsyncMock()
    client = _make_router(pipeline=pipeline, session_factory=session_factory, archive_channel="@a")
    url = "http://files.example.org/data.bin"

    await _find_url_handler(client)(_FakeEvent(url, 1))

    kwargs = pipeline.run.call_args.kwargs
    assert kwargs["delivery"] == DeliveryOptions(
        send_as="document", subtitles=True, title_length=4000, user_display="Test User @testuser", default_quality="720"
    )
    assert kwargs["cache_key"] == compute_cache_key(url, "direct", "document")
    assert kwargs["cache_key"] != compute_cache_key(url, "direct", "video")


async def test_default_settings_keep_the_legacy_cache_key(session_factory):
    pipeline = AsyncMock()
    client = _make_router(pipeline=pipeline, session_factory=session_factory)
    url = "http://files.example.org/data.bin"

    await _find_url_handler(client)(_FakeEvent(url, 1))

    kwargs = pipeline.run.call_args.kwargs
    assert kwargs["delivery"].send_as == "video" and kwargs["delivery"].subtitles is False
    assert kwargs["delivery"].title_length == 500
    assert kwargs["cache_key"] == compute_cache_key(url, "direct")


async def test_tiktok_and_instagram_links_also_honour_file_mode(session_factory):
    _save_settings(session_factory, format="document")
    for url, ref, platform in [
        ("https://www.tiktok.com/@u/video/7123456789", "https://www.tiktok.com/@u/video/7123456789", "tiktok"),
        ("https://www.instagram.com/p/DFxyz123/", "DFxyz123", "instagram"),
    ]:
        pipeline = AsyncMock()
        client = _make_router(pipeline=pipeline, session_factory=session_factory)
        await _find_url_handler(client)(_FakeEvent(url, 1))
        kwargs = pipeline.run.call_args.kwargs
        assert kwargs["delivery"].send_as == "document"
        assert kwargs["cache_key"] == compute_cache_key(ref, platform, "document")


async def test_youtube_file_mode_and_subtitles_apply_to_the_youtube_flow(session_factory):
    """Owner decision: "send as file" applies to YouTube too (the old bot
    overrode it to video there)."""
    _save_settings(session_factory, format="document", subtitles=1)
    pipeline = AsyncMock()
    client = _make_router(pipeline=pipeline, session_factory=session_factory)
    menu = _QualityEvent(YOUTUBE_URL, 1)
    await _find_url_handler(client)(menu)

    cb = _FakeCallbackEvent(menu.buttons[0][1].type.data, sender_id=1)  # 720p
    await _find_ytq_handler(client)(cb)

    kwargs = pipeline.run.call_args.kwargs
    assert kwargs["delivery"].send_as == "document" and kwargs["delivery"].subtitles is True
    assert kwargs["engine"]._subtitles is True and kwargs["engine"]._quality == "720"
    assert kwargs["cache_key"] == compute_cache_key("dQw4w9WgXcQ", "720", "document", True)


async def test_youtube_default_settings_do_not_request_subtitles(session_factory):
    pipeline = AsyncMock()
    client = _make_router(pipeline=pipeline, session_factory=session_factory)
    menu = _QualityEvent(YOUTUBE_URL, 1)
    await _find_url_handler(client)(menu)

    await _find_ytq_handler(client)(_FakeCallbackEvent(menu.buttons[0][0].type.data, sender_id=1))

    kwargs = pipeline.run.call_args.kwargs
    assert kwargs["engine"]._subtitles is False
    assert kwargs["cache_key"] == compute_cache_key("dQw4w9WgXcQ", "1080")


@pytest.mark.parametrize(("saved", "marked"), [("high", "🎬 1080p HD ✅"), ("medium", "🎬 720p ✅"), ("low", "🎬 480p ✅")])
async def test_quality_setting_marks_the_default_button_in_the_menu(session_factory, saved, marked):
    _save_settings(session_factory, quality=saved)
    client = _make_router(session_factory=session_factory)
    menu = _QualityEvent(YOUTUBE_URL, 1)

    await _find_url_handler(client)(menu)

    labels = [b.text for row in menu.buttons for b in row]
    assert [label for label in labels if "✅" in label] == [marked]
    assert len(labels) == 5


def test_menu_without_a_default_has_no_marker():
    assert not any("✅" in b.text for row in build_quality_markup("h") for b in row)


# --------------------------------------------------------------------------
# owner decisions in the router
# --------------------------------------------------------------------------
def test_url_handler_only_listens_in_private_chats():
    client = _make_router()
    url_handler = _find_url_handler(client)
    builders = [b for cb, b in client.list_event_handlers() if cb is url_handler]
    assert len(builders) == 1
    accepts = builders[0].func

    class _Ev:
        def __init__(self, is_private):
            self.is_private = is_private

    assert accepts(_Ev(True)) is True
    assert accepts(_Ev(False)) is False  # a link posted in a group is ignored
    assert isinstance(builders[0], events.NewMessage)


async def test_picking_a_quality_edits_the_menu_message_in_place(session_factory):
    pipeline = AsyncMock()
    client = _make_router(pipeline=pipeline, session_factory=session_factory)
    menu = _QualityEvent(YOUTUBE_URL, 1)
    await _find_url_handler(client)(menu)
    cb = _FakeCallbackEvent(menu.buttons[0][1].type.data, sender_id=1)  # 720p

    await _find_ytq_handler(client)(cb)

    assert cb.respond_calls == 0  # no new message
    assert cb.answer_calls == [("⏳ מתחיל הורדה באיכות 720p...", False)]
    args, kwargs = cb.edit_calls[0]
    assert args == ("🔄 מוריד באיכות 720p...",) and kwargs == {"buttons": None}
    assert len(cb.messages) == 1
    # the pipeline reports into that same message
    progress = pipeline.run.call_args.kwargs["progress"]
    await progress.update("x")
    assert cb.menu_message.edits[-1] == "x"


async def test_picking_audio_uses_the_audio_wording(session_factory):
    client = _make_router(pipeline=AsyncMock(), session_factory=session_factory)
    menu = _QualityEvent(YOUTUBE_URL, 1)
    await _find_url_handler(client)(menu)
    cb = _FakeCallbackEvent(menu.buttons[2][0].type.data, sender_id=1)

    await _find_ytq_handler(client)(cb)

    assert cb.answer_calls == [("⏳ מתחיל להוריד שמע...", False)]
    assert cb.edit_calls[0][0] == ("🔄 מוריד שמע...",)


async def test_out_of_credits_message_carries_the_contact_button(session_factory):
    client = _make_router(pipeline=AsyncMock(), session_factory=session_factory, free_download=0)
    menu = _QualityEvent("https://www.youtube.com/playlist?list=PLx", 1)
    await _find_url_handler(client)(menu)
    cb = _FakeCallbackEvent(menu.buttons[0][0].type.data, sender_id=1)

    await _find_ytq_handler(client)(cb)

    message = cb.menu_message
    assert message.edits[-1] == "❌ הקרדיטים שלך נגמרו.\nלרכישת קרדיטים נוספים, צור קשר עם יוצר הבוט. 👇"
    (row,) = message.edit_kwargs[-1]["buttons"]
    assert (row[0].text, row[0].type.url) == ("💬 לרכישת קרדיטים", "https://t.me/YD_IL")


async def test_bandwidth_limit_message_points_to_the_contact_button_not_buy(session_factory):
    pipeline = AsyncMock()
    pipeline.run.side_effect = BandwidthExhaustedException(texts.BANDWIDTH_EXHAUSTED)
    client = _make_router(pipeline=pipeline, session_factory=session_factory)
    event = _FakeEvent("http://files.example.org/data.bin", 1)

    await _find_url_handler(client)(event)

    message = event.messages[0]
    assert "/buy" not in message.edits[-1] and "צרו קשר" in message.edits[-1]
    (row,) = message.edit_kwargs[-1]["buttons"]
    assert row[0].type.url == "https://t.me/YD_IL"


def test_no_user_facing_text_mentions_buy():
    from media_bot_v2.credits import service  # noqa: F401 - import guard for the message source

    for name in (n for n in dir(texts) if n.isupper()):
        value = getattr(texts, name)
        if isinstance(value, str):
            assert "/buy" not in value, name


# --------------------------------------------------------------------------
# quality menu: real title and duration
# --------------------------------------------------------------------------
async def test_quality_menu_shows_the_real_title_and_duration(monkeypatch, session_factory):
    seen: dict = {}

    async def fake_lookup(url, *, opts, timeout):
        seen.update(url=url, timeout=timeout, opts=opts)
        return "My *great* [video]", "3:45"

    monkeypatch.setattr("media_bot_v2.telegram.router.fetch_title_duration", fake_lookup)
    responses: list[str] = []

    class _Ev(_QualityEvent):
        async def respond(self, text, *args, **kwargs):
            responses.append(text)
            return await super().respond(text, *args, **kwargs)

    client = _make_router(session_factory=session_factory)
    await _find_url_handler(client)(_Ev(YOUTUBE_URL, 1))

    assert "📹 **My  great  (video)**" in responses[0]  # markdown specials neutralised
    assert "⏱️ משך: 3:45" in responses[0]
    assert seen["url"] == YOUTUBE_URL and seen["timeout"] == 8.0
    assert seen["opts"]["skip_download"] is True and seen["opts"]["noplaylist"] is True


async def test_quality_menu_falls_back_to_placeholders_when_the_lookup_finds_nothing(session_factory):
    responses: list[str] = []

    class _Ev(_QualityEvent):
        async def respond(self, text, *args, **kwargs):
            responses.append(text)
            return await super().respond(text, *args, **kwargs)

    client = _make_router(session_factory=session_factory)
    await _find_url_handler(client)(_Ev(YOUTUBE_URL, 1))

    assert "📹 **סרטון יוטיוב**" in responses[0] and "⏱️ משך: לא ידוע" in responses[0]


async def test_a_slow_lookup_is_cut_off_by_the_timeout():
    from media_bot_v2.engines import youtube

    class _SlowYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            import time

            time.sleep(1.0)
            return {"title": "late", "duration": 5}

    from unittest import mock

    with mock.patch.object(youtube.yt_dlp, "YoutubeDL", _SlowYDL):
        started = asyncio.get_running_loop().time()
        result = await youtube.fetch_title_duration("u", opts={}, timeout=0.05)
        assert result == (None, None)
        assert asyncio.get_running_loop().time() - started < 0.5

        ok = await youtube.fetch_title_duration("u", opts={}, timeout=5)
        assert ok == ("late", "0:05")


# --------------------------------------------------------------------------
# the YouTube engine honours the settings
# --------------------------------------------------------------------------
def _opts(tmp_path, **kwargs):
    engine = YouTubeEngine(max_download_size=10**9, **{"quality": "720", **kwargs})
    return engine._build_ydl_opts(tmp_path, loop=None)


def test_subtitle_options_are_present_only_when_subtitles_are_enabled(tmp_path):
    off = _opts(tmp_path)
    assert not {"writesubtitles", "writeautomaticsub", "subtitleslangs", "subtitlesformat"} & set(off)

    on = _opts(tmp_path, subtitles=True)
    assert on["writesubtitles"] is True and on["writeautomaticsub"] is True
    assert on["subtitleslangs"] == ["en", "en-orig", "en-US", "en-GB"]
    assert on["subtitlesformat"] == "srt/best"
    assert {"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"} in on["postprocessors"]


def test_mp3_conversion_is_added_only_for_audio_and_audio_takes_no_subtitles(tmp_path):
    assert "postprocessors" not in _opts(tmp_path, quality="720")
    audio = _opts(tmp_path, quality="audio", subtitles=True)
    assert audio["postprocessors"] == [
        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
    ]
    assert "writesubtitles" not in audio


def test_playlists_never_download_subtitles(tmp_path):
    assert "writesubtitles" not in _opts(tmp_path, subtitles=True, is_playlist=True)


def test_result_carries_description_and_subtitle_files_but_not_as_media(tmp_path):
    video = tmp_path / "v.mp4"
    srt = tmp_path / "v.en.srt"
    video.write_bytes(b"v")
    srt.write_text("1\n")
    info = {
        "title": "T",
        "description": "the description",
        "requested_downloads": [{"filepath": str(video)}],
        "requested_subtitles": {"en": {"filepath": str(srt), "ext": "srt"}, "fr": {"filepath": str(tmp_path / "gone.srt")}},
    }

    result = _result_from_info(info)

    assert result.file_paths == [str(video)]
    assert result.subtitle_paths == [str(srt)]  # missing files are dropped
    assert result.description == "the description"


def test_setting_display_knows_the_legacy_audio_format():
    assert settings_menu.FORMAT_DISPLAY.get("audio") == "שמע"


def test_delivery_options_from_a_setting_row():
    from media_bot_v2.db.models import Setting

    setting = Setting(quality="low", format="document", subtitles=1, title_length=250)
    options = DeliveryOptions.from_setting(setting, user_display="N")
    assert (options.send_as, options.subtitles, options.title_length, options.default_quality) == ("document", True, 250, "480")
    legacy = DeliveryOptions.from_setting(Setting(quality="high", format="audio", subtitles=0, title_length=0))
    assert legacy.send_as == "video" and legacy.title_length == 0


