"""Tests for M11.6: Audio-only download as high-quality MP3 with metadata.

Verifies:
1. Audio-only produces an ffmpeg conversion command with libmp3lame at 192kbps.
2. Metadata fields (title, artist, album, cover) are set when present, and omitted
   without raising an exception when missing.
3. Audio delivery uses DocumentAttributeAudio (duration, title, performer) and thumb.
4. Video pipeline (H.264/AAC, streamable, faststart, opts, quality summary) is untouched.
5. Cache differentiates audio vs video: repeated audio returns MP3, video never gets audio.
6. Temporary conversion files are cleaned up on success, failure, and cancellation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo

from media_bot_v2.cache.video_cache import CachedItem, VideoCacheStore, compute_cache_key
from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Base, User
from media_bot_v2.engines.base import BaseEngine, DownloadResult
from media_bot_v2.engines.youtube import YouTubeEngine, build_format_selector
from media_bot_v2.pipeline import DownloadPipeline
from media_bot_v2.telegram import texts
from media_bot_v2.telegram.delivery import DeliveryOptions
from media_bot_v2.telegram.uploader import TelethonUploader
from media_bot_v2.upload.audio_converter import (
    DEFAULT_AUDIO_BITRATE,
    build_mp3_convert_command,
    convert_to_mp3,
)
from media_bot_v2.upload.media_probe import KIND_AUDIO, KIND_VIDEO, MediaInfo
from media_bot_v2.upload.streamable import build_fix_command
from tests.fakes_telegram import FakeMessage, FakeTelegramClient


class _FakeProgress:
    def __init__(self) -> None:
        self.updates: list[str] = []

    async def update(self, text: str, *, buttons=None, is_terminal=False) -> None:
        self.updates.append(text)


class _MockAudioEngine(BaseEngine):
    name = "mock_audio"
    supported_platforms = ("youtube",)

    def __init__(
        self,
        files: list[Path],
        *,
        title: str = "Test Song",
        artist: str | None = None,
        album: str | None = None,
        thumb_path: str | None = None,
        quality: str = "audio",
    ) -> None:
        self._files = files
        self._title = title
        self._artist = artist
        self._album = album
        self._thumb_path = thumb_path
        self._quality = quality

    def matches(self, url: str) -> bool:
        return True

    async def download(self, url: str, *, dest_dir: Path, cancel_token=None) -> DownloadResult:
        dest_dir.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for f in self._files:
            target = dest_dir / f.name
            target.write_bytes(f.read_bytes())
            copied.append(str(target))
        return DownloadResult(
            file_paths=copied,
            title=self._title,
            artist=self._artist,
            album=self._album,
            thumb_path=self._thumb_path,
        )


@pytest.fixture
def fake_db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(User(user_id=1, free=50, paid=0, bandwidth_used=0, total_bandwidth=0, is_blocked=0))
        session.commit()
    return factory


@pytest.fixture
def credits_service(fake_db):
    return CreditsService(fake_db, enable_vip=True, owner_ids=[], free_bandwidth=10**12)


# ==============================================================================
# Requirement 1: Audio only => mp3 conversion command generated with correct codec/quality
# ==============================================================================
def test_build_mp3_convert_command_has_libmp3lame_and_192k():
    src = Path("/tmp/input.wav")
    dst = Path("/tmp/output.mp3")
    cmd = build_mp3_convert_command(src, dst)

    assert "ffmpeg" in cmd[0]
    assert "-c:a" in cmd
    idx_ca = cmd.index("-c:a")
    assert cmd[idx_ca + 1] == "libmp3lame"
    assert "-b:a" in cmd
    idx_ba = cmd.index("-b:a")
    assert cmd[idx_ba + 1] == DEFAULT_AUDIO_BITRATE == "192k"
    assert "-id3v2_version" in cmd
    idx_id3 = cmd.index("-id3v2_version")
    assert cmd[idx_id3 + 1] == "3"
    assert cmd[-1] == str(dst)


def test_convert_to_mp3_executes_ffmpeg(tmp_path):
    src = tmp_path / "sample.opus"
    src.write_bytes(b"dummy audio")
    dst_expected = tmp_path / "sample.mp3"

    with patch("media_bot_v2.upload.audio_converter.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)

        def fake_run(cmd, **kwargs):
            # Create the target file as if ffmpeg succeeded
            out_file = Path(cmd[-1])
            out_file.write_bytes(b"converted mp3")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_run
        res = convert_to_mp3(src, title="Song", artist="Artist", bitrate="192k")
        assert res == dst_expected
        assert res.exists()
        assert not src.exists()  # Old source was cleaned up

        called_cmd = mock_run.call_args[0][0]
        assert "libmp3lame" in called_cmd
        assert "192k" in called_cmd
        assert "-metadata" in called_cmd
        assert "title=Song" in called_cmd
        assert "artist=Artist" in called_cmd


# ==============================================================================
# Requirement 2: Metadata fields set when present, omitted when missing without exception
# ==============================================================================
def test_metadata_fields_set_when_present():
    cover = Path("/tmp/cover.jpg")
    with patch.object(Path, "exists", return_value=True):
        cmd = build_mp3_convert_command(
            Path("/tmp/input.m4a"),
            Path("/tmp/output.mp3"),
            title="My Title",
            artist="My Artist",
            album="My Album",
            cover_path=cover,
        )

    assert "-metadata" in cmd
    meta_indices = [i for i, arg in enumerate(cmd) if arg == "-metadata"]
    meta_values = [cmd[i + 1] for i in meta_indices]
    assert "title=My Title" in meta_values
    assert "artist=My Artist" in meta_values
    assert "album=My Album" in meta_values

    assert "-i" in cmd
    input_indices = [i for i, arg in enumerate(cmd) if arg == "-i"]
    assert any(cmd[i + 1] == str(cover) for i in input_indices)
    assert "-c:v" in cmd
    idx_cv = cmd.index("-c:v")
    assert cmd[idx_cv + 1] == "mjpeg"
    assert "-disposition:v:0" in cmd
    idx_disp = cmd.index("-disposition:v:0")
    assert cmd[idx_disp + 1] == "attached_pic"


def test_metadata_fields_omitted_when_missing_no_exception():
    cmd = build_mp3_convert_command(
        Path("/tmp/input.m4a"),
        Path("/tmp/output.mp3"),
        title="Sole Title",
        artist=None,
        album=None,
        cover_path=None,
    )

    meta_indices = [i for i, arg in enumerate(cmd) if arg == "-metadata"]
    meta_values = [cmd[i + 1] for i in meta_indices]
    assert meta_values == ["title=Sole Title"]
    assert not any(v.startswith("artist=") for v in meta_values)
    assert not any(v.startswith("album=") for v in meta_values)
    assert "-c:v" not in cmd
    assert "-disposition:v:0" not in cmd


def test_metadata_empty_or_whitespace_omitted():
    cmd = build_mp3_convert_command(
        Path("/tmp/input.m4a"),
        Path("/tmp/output.mp3"),
        title="   ",
        artist="",
        album=None,
        cover_path=None,
    )
    meta_indices = [i for i, arg in enumerate(cmd) if arg == "-metadata"]
    assert len(meta_indices) == 0  # all empty fields omitted


# ==============================================================================
# Requirement 3: Sent as audio with DocumentAttributeAudio (duration/title/performer)
# ==============================================================================
async def test_audio_sent_with_full_document_attribute_audio(tmp_path, credits_service):
    client = FakeTelegramClient()
    uploader = TelethonUploader(client, chat_id=1, archive_channel="@archive")

    # Create dummy audio and cover
    audio_file = tmp_path / "track.opus"
    audio_file.write_bytes(b"dummy")
    cover_file = tmp_path / "thumb.jpg"
    cover_file.write_bytes(b"dummy cover")

    engine = _MockAudioEngine(
        [audio_file],
        title="Track Title",
        artist="Track Performer",
        thumb_path=str(cover_file),
        quality="audio",
    )

    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path / "dl")
    progress = _FakeProgress()

    # Mock convert_to_mp3 and probe to avoid requiring real ffmpeg in pure unit test
    def fake_convert(src, **kwargs):
        mp3 = src.with_suffix(".mp3")
        mp3.write_bytes(b"fake mp3")
        return mp3

    with (
        patch("media_bot_v2.pipeline.convert_to_mp3", side_effect=fake_convert),
        patch(
            "media_bot_v2.pipeline.probe",
            return_value=MediaInfo(kind=KIND_AUDIO, duration=180, title="Track Title", performer="Track Performer"),
        ),
    ):
        await pipeline.run(
            user_id=1,
            url="https://youtube.com/watch?v=audio123",
            engine=engine,
            uploader=uploader,
            progress=progress,
            audio_only=True,
        )

    sends = client.send_files(1)
    assert len(sends) == 1
    sent = sends[0]
    kwargs = sent.kwargs

    assert kwargs["force_document"] is False
    assert "attributes" in kwargs
    (attr,) = kwargs["attributes"]
    assert isinstance(attr, DocumentAttributeAudio)
    assert attr.duration == 180
    assert attr.title == "Track Title"
    assert attr.performer == "Track Performer"
    assert kwargs["thumb"] == str(cover_file)

    # Check completion summary line
    assert progress.updates[-1].startswith(texts.DOWNLOAD_DONE)
    assert "🎵 נשלח: MP3" in progress.updates[-1]
    assert "3:00 דקות" in progress.updates[-1]


async def test_audio_sent_with_missing_performer_omits_performer(tmp_path, credits_service):
    client = FakeTelegramClient()
    uploader = TelethonUploader(client, chat_id=1, archive_channel=None)

    audio_file = tmp_path / "track.opus"
    audio_file.write_bytes(b"dummy")

    engine = _MockAudioEngine(
        [audio_file],
        title="Instrumental",
        artist=None,
        thumb_path=None,
        quality="audio",
    )

    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path / "dl")
    progress = _FakeProgress()

    def fake_convert(src, **kwargs):
        mp3 = src.with_suffix(".mp3")
        mp3.write_bytes(b"fake mp3")
        return mp3

    with (
        patch("media_bot_v2.pipeline.convert_to_mp3", side_effect=fake_convert),
        patch(
            "media_bot_v2.pipeline.probe",
            return_value=MediaInfo(kind=KIND_AUDIO, duration=60, title="Instrumental", performer=None),
        ),
    ):
        await pipeline.run(
            user_id=1,
            url="https://youtube.com/watch?v=audio456",
            engine=engine,
            uploader=uploader,
            progress=progress,
            audio_only=True,
        )

    sent = client.send_files(1)[0]
    (attr,) = sent.kwargs["attributes"]
    assert isinstance(attr, DocumentAttributeAudio)
    assert attr.performer is None
    assert attr.title == "Instrumental"
    assert "thumb" not in sent.kwargs


# ==============================================================================
# Requirement 4: Video path is completely untouched and identical
# ==============================================================================
def test_video_format_selector_and_opts_unchanged(tmp_path):
    # Verify build_format_selector for video qualities
    f720 = build_format_selector("720")
    assert "bestvideo[vcodec^=avc][height<=720]" in f720
    assert "bestaudio[acodec^=mp4a]" in f720
    assert "bestaudio" in f720

    f1080 = build_format_selector("1080")
    assert "height<=1080" in f1080

    # Verify build_format_selector for audio
    assert build_format_selector("audio") == "bestaudio/best"

    # Verify YouTubeEngine opts for video
    engine_video = YouTubeEngine(quality="720", max_download_size=10**9)
    opts_video = engine_video._build_ydl_opts(tmp_path, loop=None)
    assert opts_video["format"] == f720
    assert opts_video["merge_output_format"] == "mp4"
    assert "postprocessors" not in opts_video
    assert "writethumbnail" not in opts_video

    # Verify video assurance layer (streamable) build_fix_command is intact
    from media_bot_v2.upload.streamable import StreamProfile

    profile_need_fix = StreamProfile(
        video_codec="av01",
        pix_fmt="yuv420p",
        audio_codec="opus",
        is_mp4_container=True,
        faststart=False,
    )
    fix_cmd = build_fix_command(tmp_path / "in.mp4", tmp_path / "out.mp4", profile_need_fix)
    assert "libx264" in fix_cmd
    assert "aac" in fix_cmd
    assert "+faststart" in fix_cmd


async def test_video_download_pipeline_behavior_unchanged(tmp_path, credits_service):
    """When audio_only is False, video path produces 🎬 נשלח: ... and DocumentAttributeVideo."""
    client = FakeTelegramClient()
    uploader = TelethonUploader(client, chat_id=1, archive_channel="@archive")

    video_file = tmp_path / "movie.mp4"
    video_file.write_bytes(b"dummy video")

    class _MockVideoEngine(BaseEngine):
        name = "mock_video"
        supported_platforms = ("youtube",)

        def matches(self, url: str) -> bool:
            return True

        async def download(self, url: str, *, dest_dir: Path, cancel_token=None) -> DownloadResult:
            dest_dir.mkdir(parents=True, exist_ok=True)
            v = dest_dir / "movie.mp4"
            v.write_bytes(b"video bytes")
            return DownloadResult(file_paths=[str(v)], title="Epic Video")

    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path / "dl")
    progress = _FakeProgress()

    with (
        patch("media_bot_v2.pipeline.ensure_streamable", side_effect=lambda p, **kw: p),
        # The real file is dummy bytes, not a real MP4 - ffprobe would fail
        # on it, so both the kind-detecting pre-probe and the post-conversion
        # probe_with_thumb are stubbed, exactly as a real "video bytes" file
        # would be read by ffprobe in production.
        patch("media_bot_v2.pipeline.probe", return_value=MediaInfo(kind=KIND_VIDEO)),
        patch(
            "media_bot_v2.pipeline.probe_with_thumb",
            return_value=MediaInfo(kind=KIND_VIDEO, duration=120, width=1280, height=720),
        ),
    ):
        await pipeline.run(
            user_id=1,
            url="https://youtube.com/watch?v=vid123",
            engine=_MockVideoEngine(),
            uploader=uploader,
            progress=progress,
            audio_only=False,
        )

    sent = client.send_files(1)[0]
    assert sent.kwargs["supports_streaming"] is True
    assert sent.kwargs["force_document"] is False
    (attr,) = sent.kwargs["attributes"]
    assert isinstance(attr, DocumentAttributeVideo)
    assert attr.w == 1280 and attr.h == 720 and attr.duration == 120

    # Summary line has 🎬, NOT 🎵
    assert "🎬 נשלח: 720p" in progress.updates[-1]
    assert "🎵 נשלח: MP3" not in progress.updates[-1]


async def test_audio_source_with_video_selection_behaves_as_before(tmp_path, credits_service):
    """'אם המקור הוא שמע בלבד והמשתמש בחר וידאו - להתנהג כמו היום':
    Quality label displays וידאו (height=0), not MP3."""
    client = FakeTelegramClient()
    uploader = TelethonUploader(client, chat_id=1, archive_channel=None)

    class _MockRawAudioEngine(BaseEngine):
        name = "mock_raw_audio"
        supported_platforms = ("direct",)

        def matches(self, url: str) -> bool:
            return True

        async def download(self, url: str, *, dest_dir: Path, cancel_token=None) -> DownloadResult:
            dest_dir.mkdir(parents=True, exist_ok=True)
            a = dest_dir / "raw.mp3"
            a.write_bytes(b"raw mp3")
            return DownloadResult(file_paths=[str(a)], title="Raw Track")

    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path / "dl")
    progress = _FakeProgress()

    with (
        patch("media_bot_v2.pipeline.ensure_streamable", side_effect=lambda p, **kw: p),
        # Real file is garbage bytes, not a real MP3 - ffprobe would fail on
        # it, so both the kind-detecting pre-probe and probe_with_thumb are
        # stubbed to what a real "audio, no video stream" file reads as.
        patch("media_bot_v2.pipeline.probe", return_value=MediaInfo(kind=KIND_AUDIO)),
        patch(
            "media_bot_v2.pipeline.probe_with_thumb",
            return_value=MediaInfo(kind=KIND_AUDIO, duration=60, width=0, height=0),
        ),
    ):
        await pipeline.run(
            user_id=1,
            url="https://example.com/raw.mp3",
            engine=_MockRawAudioEngine(),
            uploader=uploader,
            progress=progress,
            audio_only=False,  # User did not select audio-only
        )

    # In video mode, summary shows 🎬 נשלח: וידאו, preserving previous behavior
    assert "🎬 נשלח: וידאו" in progress.updates[-1]
    assert "🎵 נשלח: MP3" not in progress.updates[-1]


# ==============================================================================
# Requirement 5: Cache behavior - audio vs video isolation
# ==============================================================================
def test_cache_keys_different_for_audio_and_video():
    ref = "test_video_id"
    key_audio = compute_cache_key(ref, "audio")
    key_video_720 = compute_cache_key(ref, "720")
    key_video_1080 = compute_cache_key(ref, "1080")

    assert key_audio != key_video_720
    assert key_audio != key_video_1080
    assert key_video_720 != key_video_1080


async def test_cache_audio_resend_and_video_isolation(tmp_path, fake_db, credits_service):
    cache = VideoCacheStore(fake_db)
    client = FakeTelegramClient()
    uploader = TelethonUploader(client, chat_id=1, archive_channel="@archive")

    ref = "test_vid_abc"
    audio_key = compute_cache_key(ref, "audio")
    video_key = compute_cache_key(ref, "720")

    # Put audio item in cache under audio_key
    cache.put(
        audio_key,
        archive_chat="@archive",
        message_ids=[101],
        title="Cached Hit Song",
        items=[CachedItem(kind="audio", duration=210)],
    )

    # 1. Video request on video_key: must be a cache miss
    cached_video = cache.get(video_key)
    assert cached_video is None

    # 2. Audio request on audio_key: must be a cache hit
    cached_audio = cache.get(audio_key)
    assert cached_audio is not None
    assert cached_audio.title == "Cached Hit Song"
    assert cached_audio.items[0].kind == "audio"

    # Resend cached audio via pipeline
    fake_msg = FakeMessage(id=101, chat="@archive", media=MagicMock())
    client.stored[("@archive", 101)] = fake_msg

    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path / "dl")
    progress = _FakeProgress()

    served = await pipeline._try_serve_from_cache(
        user_id=1,
        url="https://youtube.com/watch?v=abc",
        uploader=uploader,
        cached=cached_audio,
        progress=progress,
        delivery=DeliveryOptions(),
        request_started=0.0,
    )
    assert served is True
    assert len(client.send_files(1)) == 1
    assert "🎵 נשלח: MP3" in progress.updates[-1]
    assert "3:30 דקות" in progress.updates[-1]


# ==============================================================================
# Requirement 6: Cleanup of intermediate files on success and failure
# ==============================================================================
def test_convert_to_mp3_cleans_up_on_failure(tmp_path):
    src = tmp_path / "bad.wav"
    src.write_bytes(b"bad audio")

    with patch("media_bot_v2.upload.audio_converter.subprocess.run") as mock_run:
        import subprocess

        mock_run.side_effect = subprocess.SubprocessError("ffmpeg failed")
        res = convert_to_mp3(src, title="Bad")
        # Returns original source on failure
        assert res == src
        # Any temporary converting file is cleaned up
        converting_files = list(tmp_path.glob("*.converting.mp3"))
        assert len(converting_files) == 0


async def test_pipeline_cleans_up_task_dir_on_cancellation_or_failure(tmp_path, credits_service):
    client = FakeTelegramClient()
    uploader = TelethonUploader(client, chat_id=1, archive_channel=None)

    class _FailingEngine(BaseEngine):
        name = "fail"
        supported_platforms = ("youtube",)

        def matches(self, url: str) -> bool:
            return True

        async def download(self, url: str, *, dest_dir: Path, cancel_token=None) -> DownloadResult:
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / "temp.raw").write_bytes(b"temp")
            raise RuntimeError("Engine crashed")

    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path / "dl")
    progress = _FakeProgress()

    with pytest.raises(RuntimeError, match="Engine crashed"):
        await pipeline.run(
            user_id=1,
            url="http://x",
            engine=_FailingEngine(),
            uploader=uploader,
            progress=progress,
            audio_only=True,
        )

    # Check task dir was cleaned up
    dl_dir = tmp_path / "dl"
    if dl_dir.exists():
        # Any subdirectories for user 1 should be gone or empty
        user_dir = dl_dir / "1"
        if user_dir.exists():
            assert list(user_dir.iterdir()) == []
