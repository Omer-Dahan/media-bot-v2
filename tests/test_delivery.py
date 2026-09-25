"""What the bot actually sends to Telegram (M7a: send style, settings, no
"Forwarded from").

Everything here runs the real `DownloadPipeline` + real `TelethonUploader`
against `FakeTelegramClient`, which records every call with its full
keyword arguments. Assertions are on those recorded arguments - the media
type, `supports_streaming`, `DocumentAttributeVideo` fields, thumbnails,
captions, part labels, edits - never just "the function ran".
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telethon.errors import MediaInvalidError, RPCError
from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo

from media_bot_v2.cache.video_cache import CachedItem, VideoCacheStore, compute_cache_key
from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Base, User
from media_bot_v2.engines.base import BaseEngine, DownloadResult
from media_bot_v2.pipeline import DownloadPipeline
from media_bot_v2.telegram import captions
from media_bot_v2.telegram.delivery import DeliveryOptions
from media_bot_v2.telegram.uploader import TelethonUploader
from media_bot_v2.upload.media_probe import KIND_AUDIO, KIND_VIDEO, MediaInfo
from tests.fakes_telegram import FakeMedia, FakeMessage, FakeTelegramClient

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

USER_CHAT = 1
ARCHIVE = "@archive"
URL = "https://www.youtube.com/watch?v=abc123"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
def _make_video(path: Path, seconds: int, size: str = "320x240") -> Path:
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc=size={size}:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440",
            "-t", str(seconds), "-pix_fmt", "yuv420p", "-shortest", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


def _make_audio(path: Path, seconds: int) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(seconds), str(path)],
        check=True,
    )
    return path


@pytest.fixture(scope="module")
def media_dir(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("media")
    _make_video(root / "v2s.mp4", 2)
    _make_video(root / "v1s.mp4", 1)
    _make_video(root / "v3s.mp4", 3)
    _make_audio(root / "a2s.mp3", 2)
    return root


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(User(user_id=USER_CHAT, free=50, paid=0, bandwidth_used=0, total_bandwidth=0, is_blocked=0))
        session.commit()
    return factory


@pytest.fixture
def credits_service(session_factory):
    return CreditsService(session_factory, enable_vip=True, owner_ids=[], free_bandwidth=10**12)


@pytest.fixture
def client():
    return FakeTelegramClient()


@pytest.fixture
def uploader(client):
    return TelethonUploader(client, chat_id=USER_CHAT, archive_channel=ARCHIVE)


class _Progress:
    def __init__(self) -> None:
        self.updates: list[str] = []

    async def update(self, text: str, **kwargs) -> None:
        self.updates.append(text)


class _Engine(BaseEngine):
    """Writes the given files (copied from the shared media dir) into dest_dir."""

    def __init__(self, files, *, title="My Video", description=None, subtitles=()):
        self._files = files  # list of (dest_name, source Path)
        self._title = title
        self._description = description
        self._subtitles = subtitles

    def matches(self, url: str) -> bool:
        return True

    async def download(self, url, *, dest_dir, cancel_token=None) -> DownloadResult:
        dest_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for name, source in self._files:
            shutil.copy(source, dest_dir / name)
            paths.append(str(dest_dir / name))
        subs = []
        for name in self._subtitles:
            (dest_dir / name).write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
            subs.append(str(dest_dir / name))
        return DownloadResult(
            file_paths=paths, title=self._title, description=self._description, subtitle_paths=subs
        )


def _pipeline(credits_service, tmp_path, **kwargs) -> DownloadPipeline:
    return DownloadPipeline(credits_service=credits_service, download_dir=tmp_path / "dl", **kwargs)


async def _run(pipeline, engine, uploader, *, delivery=None, cache=None, cache_key=None, url=URL, progress=None):
    progress = progress or _Progress()
    await pipeline.run(
        user_id=USER_CHAT,
        url=url,
        engine=engine,
        uploader=uploader,
        progress=progress,
        cache=cache,
        cache_key=cache_key,
        archive_channel=ARCHIVE,
        delivery=delivery,
    )
    return progress


def _credits_left(session_factory) -> int:
    with session_factory() as session:
        user = session.query(User).filter(User.user_id == USER_CHAT).one()
        return user.free + user.paid


# --------------------------------------------------------------------------
# no "Forwarded from": forward_messages must never be used
# --------------------------------------------------------------------------
async def test_delivery_never_uses_forward_messages(client, uploader, credits_service, tmp_path, media_dir):
    """Archive + cache were `forward_messages`, which shows the user
    "Forwarded from <archive channel>". FakeTelegramClient raises if it is
    called, and we also assert it was not even attempted."""
    store_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(store_engine)
    cache = VideoCacheStore(sessionmaker(bind=store_engine))
    engine = _Engine([("clip.mp4", media_dir / "v2s.mp4")])
    key = compute_cache_key("abc123", "720")

    await _run(_pipeline(credits_service, tmp_path), engine, uploader, cache=cache, cache_key=key)
    await _run(_pipeline(credits_service, tmp_path), engine, uploader, cache=cache, cache_key=key)  # cache hit

    assert client.calls_to("forward_messages") == []


async def test_archive_copy_is_a_media_resend_with_operator_caption(client, uploader, credits_service, tmp_path, media_dir):
    await _run(
        _pipeline(credits_service, tmp_path),
        _Engine([("clip.mp4", media_dir / "v2s.mp4")]),
        uploader,
        delivery=DeliveryOptions(user_display="Dana @dana"),
    )

    to_user, to_archive = client.send_files(USER_CHAT)[0], client.send_files(ARCHIVE)[0]
    sent_message = client.stored[(USER_CHAT, 1)]
    # the archive gets the very same media object, not a re-upload of a path
    assert to_archive.args[1] is sent_message.media
    assert to_archive.kwargs["parse_mode"] == "html"
    archive_caption = to_archive.kwargs["caption"]
    assert "👤 משתמש: Dana @dana" in archive_caption
    assert f"🆔 {USER_CHAT}" in archive_caption
    assert "📁 קובץ: clip.mp4" in archive_caption
    assert f"<blockquote expandable>🔗 קישור: {URL}</blockquote>" in archive_caption
    assert to_user.args[0] == USER_CHAT


async def test_cache_hit_resends_media_with_the_requesting_users_caption(
    client, uploader, credits_service, session_factory, tmp_path, media_dir
):
    store_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(store_engine)
    cache = VideoCacheStore(sessionmaker(bind=store_engine))
    key = compute_cache_key("abc123", "720")
    engine = _Engine([("clip.mp4", media_dir / "v2s.mp4")], title="Cached & <Title>")

    await _run(
        _pipeline(credits_service, tmp_path), engine, uploader,
        delivery=DeliveryOptions(user_display="Original Owner"), cache=cache, cache_key=key,
    )  # fmt: skip
    credits_after_first = _credits_left(session_factory)
    client.calls.clear()

    progress = await _run(
        _pipeline(credits_service, tmp_path), _Engine([]), uploader,
        url="https://youtu.be/abc123", cache=cache, cache_key=key,
        delivery=DeliveryOptions(user_display="Someone Else"),
    )  # fmt: skip

    assert [c.method for c in client.calls] == ["get_messages", "send_file"]
    get_call, send_call = client.calls
    assert get_call.args == (ARCHIVE,)
    resent = send_call.kwargs["caption"]
    assert send_call.args[0] == USER_CHAT
    assert send_call.kwargs["parse_mode"] == "html"
    assert "Cached &amp; &lt;Title&gt;" in resent
    assert "🔗 מקור: https://youtu.be/abc123" in resent  # the *requesting* URL
    assert "📐 רזולוציה: 320x240" in resent and "⏱️ אורך: 0:02 דקות" in resent
    assert "👤" not in resent and "Original Owner" not in resent  # never the archive's caption
    assert _credits_left(session_factory) == credits_after_first  # a cache hit is free
    assert progress.updates[-1] == "הושלם ✅"


async def test_cache_hit_with_a_deleted_archive_message_falls_back_to_a_fresh_download(
    client, uploader, credits_service, tmp_path, media_dir
):
    store_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(store_engine)
    cache = VideoCacheStore(sessionmaker(bind=store_engine))
    key = compute_cache_key("abc123", "720")
    engine = _Engine([("clip.mp4", media_dir / "v2s.mp4")])
    await _run(_pipeline(credits_service, tmp_path), engine, uploader, cache=cache, cache_key=key)
    client.stored.pop((ARCHIVE, 2))  # someone deleted the archived message
    client.calls.clear()

    await _run(_pipeline(credits_service, tmp_path), engine, uploader, cache=cache, cache_key=key)

    # get_messages found nothing -> real download + upload happened again
    assert [c.method for c in client.calls][:2] == ["get_messages", "send_file"]
    assert any(isinstance(c.args[1], str) for c in client.send_files(USER_CHAT))  # a real file upload


def test_cache_key_includes_the_delivery_format_and_subtitles():
    video = compute_cache_key("abc123", "720", "video")
    assert compute_cache_key("abc123", "720", "document") != video
    assert compute_cache_key("abc123", "720", "video", True) != video
    assert compute_cache_key("abc123", "720", "document", True) != compute_cache_key("abc123", "720", "document")
    assert compute_cache_key("abc123", "1080", "video") != video
    # defaults add nothing beyond the version component; pre-M9.1 rows
    # (unversioned key) are deliberately retired - see test_m9_1.py
    assert video == hashlib.md5(b"abc123:720:v2", usedforsecurity=False).hexdigest()


async def test_switching_from_video_to_file_does_not_serve_the_cached_video(
    client, uploader, credits_service, tmp_path, media_dir
):
    store_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(store_engine)
    cache = VideoCacheStore(sessionmaker(bind=store_engine))
    engine = _Engine([("clip.mp4", media_dir / "v2s.mp4")])

    def key(delivery):
        return compute_cache_key("abc123", "720", delivery.send_as, delivery.subtitles)

    as_video, as_file = DeliveryOptions(send_as="video"), DeliveryOptions(send_as="document")
    await _run(_pipeline(credits_service, tmp_path), engine, uploader, delivery=as_video, cache=cache, cache_key=key(as_video))
    client.calls.clear()

    await _run(_pipeline(credits_service, tmp_path), engine, uploader, delivery=as_file, cache=cache, cache_key=key(as_file))

    assert client.calls_to("get_messages") == []  # no cache hit for the other format
    user_send = client.send_files(USER_CHAT)[0]
    assert user_send.kwargs["force_document"] is True  # ...it was downloaded and sent as a file

    # and the "file" delivery is now cached separately: a repeat hits the cache
    client.calls.clear()
    await _run(_pipeline(credits_service, tmp_path), engine, uploader, delivery=as_file, cache=cache, cache_key=key(as_file))
    assert [c.method for c in client.calls] == ["get_messages", "send_file"]


# --------------------------------------------------------------------------
# send style: video vs document
# --------------------------------------------------------------------------
async def test_video_is_sent_streamable_with_duration_resolution_and_thumbnail(
    client, uploader, credits_service, tmp_path, media_dir
):
    await _run(_pipeline(credits_service, tmp_path), _Engine([("clip.mp4", media_dir / "v2s.mp4")]), uploader)

    call = client.send_files(USER_CHAT)[0]
    kwargs = call.kwargs
    assert kwargs["supports_streaming"] is True
    assert kwargs["force_document"] is False
    assert kwargs["parse_mode"] == "html"
    (attribute,) = kwargs["attributes"]
    assert isinstance(attribute, DocumentAttributeVideo)
    assert (attribute.duration, attribute.w, attribute.h, attribute.supports_streaming) == (2, 320, 240, True)
    assert kwargs["thumb"].endswith(".jpg")
    assert call.args[1].endswith("clip.mp4")


async def test_thumbnail_is_a_real_small_jpeg_when_sent(uploader, client, credits_service, tmp_path, media_dir):
    seen: dict = {}
    original = client.send_file

    async def spy(entity, file, **kwargs):
        thumb = kwargs.get("thumb")
        if thumb and entity == USER_CHAT:
            data = Path(thumb).read_bytes()
            seen.update(size=len(data), magic=data[:3])
        return await original(entity, file, **kwargs)

    client.send_file = spy
    await _run(_pipeline(credits_service, tmp_path), _Engine([("clip.mp4", media_dir / "v2s.mp4")]), uploader)

    assert seen["magic"] == b"\xff\xd8\xff"  # JPEG
    assert 100 < seen["size"] <= 200 * 1024  # Telegram's thumbnail limits


async def test_file_mode_sends_a_document_without_streaming(client, uploader, credits_service, tmp_path, media_dir):
    await _run(
        _pipeline(credits_service, tmp_path),
        _Engine([("clip.mp4", media_dir / "v2s.mp4")]),
        uploader,
        delivery=DeliveryOptions(send_as="document"),
    )

    kwargs = client.send_files(USER_CHAT)[0].kwargs
    assert kwargs["force_document"] is True
    assert "supports_streaming" not in kwargs
    assert "attributes" not in kwargs
    assert kwargs["parse_mode"] == "html"
    assert kwargs["thumb"].endswith(".jpg")  # a document still gets a preview image


async def test_audio_is_sent_with_audio_attributes_and_audio_caption(client, uploader, credits_service, tmp_path, media_dir):
    await _run(
        _pipeline(credits_service, tmp_path),
        _Engine([("song.mp3", media_dir / "a2s.mp3")], title="My Song"),
        uploader,
    )

    kwargs = client.send_files(USER_CHAT)[0].kwargs
    (attribute,) = kwargs["attributes"]
    assert isinstance(attribute, DocumentAttributeAudio)
    assert attribute.duration == 2 and attribute.title == "My Song"
    assert kwargs["caption"].startswith("🎵 <b>My Song</b>")
    assert "supports_streaming" not in kwargs


async def test_photo_is_always_a_photo_even_in_file_mode(client, uploader, credits_service, tmp_path):
    import base64

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )
    source = tmp_path / "pic.png"
    source.write_bytes(png)
    await _run(
        _pipeline(credits_service, tmp_path),
        _Engine([("pic.png", source)]),
        uploader,
        delivery=DeliveryOptions(send_as="document"),
    )
    assert client.send_files(USER_CHAT)[0].kwargs["force_document"] is False


async def test_video_rejected_by_telegram_is_retried_once_as_a_document(client, uploader, credits_service, tmp_path, media_dir):
    client.fail_send_file[1] = MediaInvalidError(request=None)

    await _run(_pipeline(credits_service, tmp_path), _Engine([("clip.mp4", media_dir / "v2s.mp4")]), uploader)

    user_sends = client.send_files(USER_CHAT)
    assert len(user_sends) == 2  # exactly one retry, no chain of fallbacks
    assert user_sends[0].kwargs["force_document"] is False
    assert user_sends[1].kwargs["force_document"] is True
    assert "supports_streaming" not in user_sends[1].kwargs


async def test_other_upload_errors_are_not_retried(client, uploader, credits_service, tmp_path, media_dir):
    client.fail_send_file[1] = RPCError(request=None, message="boom", code=500)

    with pytest.raises(RPCError):
        await _run(_pipeline(credits_service, tmp_path), _Engine([("clip.mp4", media_dir / "v2s.mp4")]), uploader)

    assert len(client.send_files(USER_CHAT)) == 1


# --------------------------------------------------------------------------
# signatures
# --------------------------------------------------------------------------
async def test_video_signature_matches_the_old_bots_template(client, uploader, credits_service, tmp_path, media_dir):
    await _run(
        _pipeline(credits_service, tmp_path),
        _Engine([("clip.mp4", media_dir / "v2s.mp4")], title="A <b> & Title"),
        uploader,
        url="https://www.youtube.com/watch?v=abc123&t=5",
    )

    assert client.send_files(USER_CHAT)[0].kwargs["caption"] == (
        "🎬 <b>A &lt;b&gt; &amp; Title</b>\n\n"
        "🔗 מקור: https://www.youtube.com/watch?v=abc123&amp;t=5\n"
        "📐 רזולוציה: 320x240\n"
        "⏱️ אורך: 0:02 דקות\n"
        "\n⬇️ הקובץ מוכן לצפייה והורדה\n"
        "צפייה מהנה 👀✨"
    )


async def test_signature_never_shows_credits(client, uploader, credits_service, tmp_path, media_dir):
    await _run(_pipeline(credits_service, tmp_path), _Engine([("clip.mp4", media_dir / "v2s.mp4")]), uploader)
    for call in client.calls_to("send_file"):
        assert "קרדיטים" not in (call.kwargs.get("caption") or "").split("קישור")[0]
        if call.args[0] == USER_CHAT:
            assert "קרדיטים" not in call.kwargs["caption"] and "💳" not in call.kwargs["caption"]
    for call in client.calls_to("send_message"):
        assert "קרדיטים" not in call.args[1]


def test_audio_signature_matches_the_old_bots_template():
    assert captions.build_user_caption(kind=KIND_AUDIO, title="Song", url="http://x/y", duration=125) == (
        "🎵 <b>Song</b>\n\n"
        "🔗 מקור: http://x/y\n"
        "⏱️ אורך: 2:05 דקות\n"
        "⬇️ הקובץ מוכן להורדה\n"
        "שמיעה מהנה 🎧✨"
    )


@pytest.mark.parametrize(
    ("title_length", "kept"), [(100, 100), (250, 250), (500, 500), (1000, 750), (4000, 750), (0, 750)]
)
def test_description_length_setting_cuts_the_title(title_length, kept):
    caption = captions.build_user_caption(
        kind=KIND_VIDEO, title="z" * 2000, url="http://u", width=1, height=1, duration=1, title_length=title_length
    )
    assert caption.count("z") == kept


def test_caption_always_fits_telegrams_limit_even_with_huge_title_and_url():
    long_url = "https://example.com/" + "a" * 1200
    for title_length in (100, 500, 1000, 4000, 0):
        caption = captions.build_user_caption(
            kind=KIND_VIDEO, title="😀" * 3000, url=long_url, width=1920, height=1080, duration=3600, title_length=title_length
        )
        assert captions.utf16_units(caption) <= 1024
        assert "<b>" in caption and caption.rstrip().endswith("צפייה מהנה 👀✨")  # template text intact


def test_reserved_room_for_a_part_label_keeps_the_combined_caption_in_the_limit():
    label = captions.part_label(2, 3)
    full = captions.build_user_caption(
        kind=KIND_VIDEO, title="y" * 3000, url="http://u", width=1, height=1, duration=1,
        title_length=1000, reserved_units=captions.utf16_units(label) + 2,
    )  # fmt: skip
    assert captions.utf16_units(captions.with_part_label(label, full)) <= 1024


def test_description_message_is_an_expandable_quote_and_escaped():
    text = captions.build_description_message("T <1>", "line & more")
    assert text == "📋 <b>תיאור מלא:</b>\n\n<blockquote expandable>T &lt;1&gt;\n\nline &amp; more</blockquote>"
    assert captions.build_description_message(None, None) is None


# --------------------------------------------------------------------------
# split files: part labels + the signature moving to the last part
# --------------------------------------------------------------------------
def _patch_split(monkeypatch, parts_by_name: dict[str, list[str]]):
    """Make `split_file` return pre-made parts (and delete the source, as the
    real splitter does) so split behaviour is testable without a 2GB file."""

    def fake_split(file_path, **kwargs):
        made = []
        for name in parts_by_name[file_path.name]:
            made.append(file_path.parent / name)
        file_path.unlink()
        return made

    monkeypatch.setattr("media_bot_v2.pipeline.splitter.split_file", fake_split)


class _SplitEngine(_Engine):
    """Downloads one source file plus the ready-made parts a split would yield."""

    def __init__(self, parts: list[tuple[str, Path]], **kwargs):
        super().__init__([("big.mp4", parts[0][1])], **kwargs)
        self._parts = parts

    async def download(self, url, *, dest_dir, cancel_token=None):
        result = await super().download(url, dest_dir=dest_dir)
        for name, source in self._parts:
            shutil.copy(source, dest_dir / name)
        return result


async def test_split_video_parts_are_labelled_and_the_full_signature_moves_to_the_last(
    client, uploader, credits_service, tmp_path, media_dir, monkeypatch
):
    parts = [("big.part000.mp4", media_dir / "v1s.mp4"), ("big.part001.mp4", media_dir / "v2s.mp4"), ("big.part002.mp4", media_dir / "v3s.mp4")]
    _patch_split(monkeypatch, {"big.mp4": [name for name, _ in parts]})

    await _run(_pipeline(credits_service, tmp_path), _SplitEngine(parts, title="Big Movie"), uploader)

    sends = client.send_files(USER_CHAT)
    assert len(sends) == 3
    for index, send in enumerate(sends, start=1):
        assert send.kwargs["caption"].startswith(f"📎 חלק {index}/3\n\n🎬 <b>Big Movie</b>")
        assert send.kwargs["supports_streaming"] is True
        assert send.kwargs["force_document"] is False
    assert "📐 רזולוציה: 320x240" in sends[0].kwargs["caption"]

    # every part has its own real duration; resolution + thumbnail come from the source
    assert [s.kwargs["attributes"][0].duration for s in sends] == [1, 2, 3]
    assert {(s.kwargs["attributes"][0].w, s.kwargs["attributes"][0].h) for s in sends} == {(320, 240)}
    assert len({s.kwargs["thumb"] for s in sends}) == 1

    # parts 1 and 2 were edited down to their bare label after the next part arrived; part 3 was not
    edits = client.calls_to("edit_message")
    # (message ids interleave with the archive copies: user 1, archive 2, user 3, ...)
    user_part_ids = [m.id for (chat, _), m in client.stored.items() if chat == USER_CHAT]
    assert [(e.args[1].id, e.args[2]) for e in edits] == [(user_part_ids[0], "📎 חלק 1/3"), (user_part_ids[1], "📎 חלק 2/3")]
    assert all(e.kwargs["parse_mode"] == "html" for e in edits)


async def test_raw_byte_part_in_a_video_split_is_sent_as_a_document_with_a_filename_label(
    client, uploader, credits_service, tmp_path, media_dir, monkeypatch
):
    raw = tmp_path / "raw.bin"
    raw.write_bytes(b"\x00not a video\x01" * 50)
    parts = [("big.part000.mp4", media_dir / "v1s.mp4"), ("big.mp4.part001", raw)]
    _patch_split(monkeypatch, {"big.mp4": [name for name, _ in parts]})

    await _run(_pipeline(credits_service, tmp_path), _SplitEngine(parts), uploader)

    first, second = client.send_files(USER_CHAT)
    assert first.kwargs["force_document"] is False
    assert second.kwargs["force_document"] is True
    assert "attributes" not in second.kwargs and "supports_streaming" not in second.kwargs
    assert second.kwargs["caption"].startswith("📎 חלק 2/2: big.mp4.part001\n\n🎬 <b>")
    # part 1 lost its full signature once part 2 arrived
    assert [(e.args[1].id, e.args[2]) for e in client.calls_to("edit_message")] == [(1, "📎 חלק 1/2")]


async def test_document_mode_split_uses_filename_labels(client, uploader, credits_service, tmp_path, media_dir, monkeypatch):
    parts = [("big.part000.mp4", media_dir / "v1s.mp4"), ("big.part001.mp4", media_dir / "v2s.mp4")]
    _patch_split(monkeypatch, {"big.mp4": [name for name, _ in parts]})

    await _run(_pipeline(credits_service, tmp_path), _SplitEngine(parts), uploader, delivery=DeliveryOptions(send_as="document"))

    first, second = client.send_files(USER_CHAT)
    assert first.kwargs["force_document"] is second.kwargs["force_document"] is True
    assert first.kwargs["caption"].startswith("📎 חלק 1/2: big.part000.mp4\n\n")
    assert [(e.args[1].id, e.args[2]) for e in client.calls_to("edit_message")] == [(1, "📎 חלק 1/2: big.part000.mp4")]


async def test_part_two_failing_keeps_part_one_signed_and_charged_and_reports_the_error(
    client, uploader, credits_service, session_factory, tmp_path, media_dir, monkeypatch
):
    parts = [("big.part000.mp4", media_dir / "v1s.mp4"), ("big.part001.mp4", media_dir / "v2s.mp4"), ("big.part002.mp4", media_dir / "v3s.mp4")]
    _patch_split(monkeypatch, {"big.mp4": [name for name, _ in parts]})
    client.fail_send_file[3] = RuntimeError("network dropped")  # send_file #3 = user part 2 (archive copy is #2)
    progress = _Progress()

    with pytest.raises(RuntimeError):
        await _run(_pipeline(credits_service, tmp_path), _SplitEngine(parts), uploader, progress=progress)

    assert len([c for c in client.send_files(USER_CHAT)]) == 2  # part 1 sent, part 2 attempted, part 3 never
    assert client.calls_to("edit_message") == []  # part 1 still carries its full signature
    assert _credits_left(session_factory) == 49  # charged for the one delivered part only
    assert progress.updates[-1] == "❌ ההורדה נכשלה. נסה שוב או שלח קישור אחר."  # not left as "done"


# --------------------------------------------------------------------------
# subtitles + description length
# --------------------------------------------------------------------------
async def test_subtitles_on_sends_uncharged_documents_after_the_media(
    client, uploader, credits_service, session_factory, tmp_path, media_dir
):
    engine = _Engine([("clip.mp4", media_dir / "v2s.mp4")], subtitles=["clip.en.srt"])

    await _run(_pipeline(credits_service, tmp_path), engine, uploader, delivery=DeliveryOptions(subtitles=True))

    user_sends = client.send_files(USER_CHAT)
    assert [s.kwargs["caption"] for s in user_sends][1:] == ["📝 כתוביות: clip.en.srt"]
    assert user_sends[1].kwargs["force_document"] is True
    assert user_sends[1].args[1].endswith("clip.en.srt")
    assert user_sends[0].args[1].endswith("clip.mp4")  # media first, subtitles after
    assert _credits_left(session_factory) == 49  # one credit for the video, none for subtitles


async def test_subtitles_off_sends_no_subtitle_document(client, uploader, credits_service, tmp_path, media_dir):
    engine = _Engine([("clip.mp4", media_dir / "v2s.mp4")], subtitles=["clip.en.srt"])

    await _run(_pipeline(credits_service, tmp_path), engine, uploader, delivery=DeliveryOptions(subtitles=False))

    assert len(client.send_files(USER_CHAT)) == 1
    assert not any("srt" in str(c.args[1]) for c in client.calls_to("send_file"))


async def test_a_failing_subtitle_send_does_not_fail_the_download(client, uploader, credits_service, tmp_path, media_dir):
    client.fail_send_file[3] = RuntimeError("subtitle upload failed")  # 1 user media, 2 archive copy, 3 subtitle
    engine = _Engine([("clip.mp4", media_dir / "v2s.mp4")], subtitles=["clip.en.srt"])

    progress = await _run(_pipeline(credits_service, tmp_path), engine, uploader, delivery=DeliveryOptions(subtitles=True))

    assert progress.updates[-1] == "הושלם ✅"


async def test_cached_subtitles_are_resent_only_when_the_user_wants_them(
    client, uploader, credits_service, tmp_path, media_dir
):
    store_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(store_engine)
    cache = VideoCacheStore(sessionmaker(bind=store_engine))
    key = compute_cache_key("abc123", "720", "video", True)
    engine = _Engine([("clip.mp4", media_dir / "v2s.mp4")], subtitles=["clip.en.srt"])
    wants = DeliveryOptions(subtitles=True)
    await _run(_pipeline(credits_service, tmp_path), engine, uploader, delivery=wants, cache=cache, cache_key=key)
    client.calls.clear()

    await _run(_pipeline(credits_service, tmp_path), _Engine([]), uploader, delivery=wants, cache=cache, cache_key=key)

    captions_sent = [c.kwargs["caption"] for c in client.send_files(USER_CHAT)]
    assert captions_sent[1] == "📝 כתוביות: clip.en.srt"


async def test_full_description_setting_sends_a_reply_with_an_expandable_quote(client, uploader, credits_service, tmp_path, media_dir):
    engine = _Engine([("clip.mp4", media_dir / "v2s.mp4")], title="T", description="Long <description> text")

    await _run(_pipeline(credits_service, tmp_path), engine, uploader, delivery=DeliveryOptions(title_length=4000))

    (message_call,) = client.calls_to("send_message")
    media_message_id = 1
    assert message_call.args[0] == USER_CHAT
    assert message_call.kwargs["reply_to"].id == media_message_id
    assert message_call.kwargs["parse_mode"] == "html"
    assert message_call.args[1] == (
        "📋 <b>תיאור מלא:</b>\n\n<blockquote expandable>T\n\nLong &lt;description&gt; text</blockquote>"
    )


async def test_description_message_not_sent_for_other_lengths(client, uploader, credits_service, tmp_path, media_dir):
    engine = _Engine([("clip.mp4", media_dir / "v2s.mp4")], title="T", description="Some description")

    await _run(_pipeline(credits_service, tmp_path), engine, uploader, delivery=DeliveryOptions(title_length=500))

    assert client.calls_to("send_message") == []


async def test_title_length_setting_shortens_the_signature_title(client, uploader, credits_service, tmp_path, media_dir):
    engine = _Engine([("clip.mp4", media_dir / "v2s.mp4")], title="z" * 400)

    await _run(_pipeline(credits_service, tmp_path), engine, uploader, delivery=DeliveryOptions(title_length=100))

    assert client.send_files(USER_CHAT)[0].kwargs["caption"].count("z") == 100


# --------------------------------------------------------------------------
# uploader unit behaviour
# --------------------------------------------------------------------------
async def test_uploader_send_kwargs_for_a_video_without_metadata_still_streams(client, uploader, tmp_path):
    path = tmp_path / "x.mp4"
    path.write_bytes(b"0")

    await uploader.send_file(path, caption="c", media=MediaInfo(kind=KIND_VIDEO))

    kwargs = client.send_files(USER_CHAT)[0].kwargs
    assert kwargs["supports_streaming"] is True and "attributes" not in kwargs


async def test_uploader_resend_of_archived_media_needs_every_message(client, uploader):
    client.stored[(ARCHIVE, 7)] = FakeMessage(7, ARCHIVE, FakeMedia("a"))

    with pytest.raises(LookupError):
        await uploader.send_cached(ARCHIVE, [7, 8], captions=["a", "b"])

    assert client.send_files() == []  # nothing was sent when one was missing


def test_old_cache_rows_without_metadata_still_decode(session_factory):
    from media_bot_v2.db.models import VideoCache

    with session_factory() as session:
        session.add(
            VideoCache(
                cache_key="k",
                file_id='{"archive_chat": "@archive", "message_ids": [5]}',
                meta='{"title": "Old Title"}',
            )
        )
        session.commit()

    entry = VideoCacheStore(session_factory).get("k")
    assert entry is not None and entry.title == "Old Title"
    assert entry.items == [] and entry.subtitle_ids == []


async def test_cache_hit_for_an_old_row_gets_a_basic_caption(client, uploader, credits_service, session_factory, tmp_path):
    from media_bot_v2.db.models import VideoCache

    client.stored[(ARCHIVE, 5)] = FakeMessage(5, ARCHIVE, FakeMedia("old"))
    with session_factory() as session:
        session.add(
            VideoCache(cache_key="k", file_id='{"archive_chat": "@archive", "message_ids": [5]}', meta='{"title": "Old Title"}')
        )
        session.commit()

    await _run(_pipeline(credits_service, tmp_path), _Engine([]), uploader, cache=VideoCacheStore(session_factory), cache_key="k")

    caption = client.send_files(USER_CHAT)[0].kwargs["caption"]
    assert caption.startswith("🎬 <b>Old Title</b>") and f"🔗 מקור: {URL}" in caption
    assert "📐" not in caption  # unknown resolution is left out, not shown as 0x0


def test_cached_item_round_trips_through_the_store(session_factory):
    store = VideoCacheStore(session_factory)
    item = CachedItem(kind="video", duration=9, width=640, height=360, part_index=2, part_total=3, part_label="📎 חלק 2/3")
    store.put("k2", archive_chat="@a", message_ids=[1], title="t", items=[item], description="d", subtitle_ids=[4], subtitle_names=["s.srt"])

    entry = store.get("k2")
    assert entry.items == [item]
    assert (entry.description, entry.subtitle_ids, entry.subtitle_names) == ("d", [4], ["s.srt"])
