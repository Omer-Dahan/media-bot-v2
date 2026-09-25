"""Regression tests for "the application cannot play this video
(ERROR_CODE_IO_UNSPECIFIED)" in Plus Messenger.

Root cause: the YouTube format selector let yt-dlp pick AV1/VP9 + Opus, which
it muxes into an `.mp4` Telegram accepts but clients cannot play. These tests
build real files with ffmpeg and check what is *in* the file that would be
sent (codecs, container, moov position) and the metadata sent alongside it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telethon.tl.types import DocumentAttributeVideo

from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Base, User
from media_bot_v2.engines.base import BaseEngine, DownloadResult
from media_bot_v2.engines.youtube import build_format_selector
from media_bot_v2.pipeline import DownloadPipeline
from media_bot_v2.telegram.delivery import SEND_AS_DOCUMENT, DeliveryOptions
from media_bot_v2.telegram.uploader import TelethonUploader
from media_bot_v2.upload.media_probe import probe
from media_bot_v2.upload.streamable import (
    build_fix_command,
    ensure_streamable,
    inspect_streams,
    moov_before_mdat,
)
from tests.fakes_telegram import FakeTelegramClient

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not installed"
)


def _encoders() -> str:
    return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, check=False).stdout


needs_vp9 = pytest.mark.skipif("libvpx-vp9" not in _encoders(), reason="libvpx-vp9 missing")
needs_opus = pytest.mark.skipif("libopus" not in _encoders(), reason="libopus missing")


def _make(path: Path, *, vcodec: str, acodec: str, extra: list[str] | None = None, size: str = "320x240") -> Path:
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=size={size}:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440",
        "-t", "2", "-shortest", "-c:v", vcodec, "-c:a", acodec, "-pix_fmt", "yuv420p",
        *(extra or []), str(path),
    ]  # fmt: skip
    subprocess.run(cmd, check=True)
    return path


def _streams(path: Path) -> dict[str, str]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    return {s["codec_type"]: s["codec_name"] for s in json.loads(out.stdout)["streams"]}


def _assert_playable(path: Path) -> None:
    assert _streams(path) == {"video": "h264", "audio": "aac"}
    assert path.suffix == ".mp4"
    assert moov_before_mdat(path)


# --------------------------------------------------------------------------
# the format selector (the actual root cause)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("quality", ["1080", "720", "480", "360"])
def test_selector_only_reaches_unrestricted_codecs_as_a_last_resort(quality):
    choices = build_format_selector(quality).split("/")
    restricted = [c for c in choices if "vcodec^=avc" in c]
    assert choices[: len(restricted)] == restricted and len(restricted) == 3
    assert "acodec^=mp4a" in choices[0] and "acodec^=mp4a" in choices[1]
    # nothing in the chain ever *asks* for av1/vp9/opus
    assert not any(bad in build_format_selector(quality) for bad in ("av01", "vp09", "vp9", "opus"))


# --------------------------------------------------------------------------
# moov detection
# --------------------------------------------------------------------------
def test_moov_position_is_detected_from_the_atoms(tmp_path):
    slow = _make(tmp_path / "slow.mp4", vcodec="libx264", acodec="aac")  # ffmpeg default: moov at the end
    fast = tmp_path / "fast.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(slow), "-c", "copy", "-movflags", "+faststart", str(fast)],
        check=True,
    )
    assert moov_before_mdat(slow) is False
    assert moov_before_mdat(fast) is True
    assert moov_before_mdat(tmp_path / "missing.mp4") is False


# --------------------------------------------------------------------------
# ensure_streamable
# --------------------------------------------------------------------------
def test_good_file_is_left_alone(tmp_path):
    good = _make(tmp_path / "good.mp4", vcodec="libx264", acodec="aac", extra=["-movflags", "+faststart"])
    before = good.read_bytes()
    assert ensure_streamable(good) == good
    assert good.read_bytes() == before


def test_moov_at_the_end_is_fixed_by_remux_without_reencoding(tmp_path):
    slow = _make(tmp_path / "slow.mp4", vcodec="libx264", acodec="aac")
    profile = inspect_streams(slow)
    assert profile is not None and profile.video_ok and profile.audio_ok and not profile.faststart
    cmd = build_fix_command(slow, tmp_path / "out.mp4", profile)
    assert cmd[cmd.index("-c:v") + 1] == "copy" and cmd[cmd.index("-c:a") + 1] == "copy"
    assert "+faststart" in cmd

    fixed = ensure_streamable(slow)
    _assert_playable(fixed)


@needs_vp9
@needs_opus
def test_vp9_opus_mp4_is_converted_to_h264_aac(tmp_path):
    """The exact shape of the bug: an .mp4 holding VP9 + Opus."""
    bad = _make(tmp_path / "bad.mp4", vcodec="libvpx-vp9", acodec="libopus", extra=["-movflags", "+faststart"])
    assert _streams(bad) == {"video": "vp9", "audio": "opus"}

    fixed = ensure_streamable(bad)
    _assert_playable(fixed)
    assert fixed == bad  # same name, replaced in place
    assert [p.name for p in tmp_path.iterdir()] == ["bad.mp4"]  # no temp files left behind


@needs_opus
def test_only_the_bad_stream_is_reencoded(tmp_path):
    """H.264 video with Opus audio in an mkv: the video is copied, the audio becomes AAC."""
    mkv = _make(tmp_path / "clip.mkv", vcodec="libx264", acodec="libopus")
    profile = inspect_streams(mkv)
    assert profile is not None and profile.video_ok and not profile.audio_ok
    cmd = build_fix_command(mkv, tmp_path / "out.mp4", profile)
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert cmd[cmd.index("-c:a") + 1] == "aac"

    fixed = ensure_streamable(mkv)
    _assert_playable(fixed)
    assert fixed.name == "clip.mp4" and not mkv.exists()


@needs_vp9
def test_webm_becomes_an_mp4(tmp_path):
    webm = _make(tmp_path / "clip.webm", vcodec="libvpx-vp9", acodec="libopus")
    fixed = ensure_streamable(webm)
    _assert_playable(fixed)


def test_high_bit_depth_h264_is_reencoded_because_phones_cannot_decode_it(tmp_path):
    ten_bit = _make(tmp_path / "ten.mp4", vcodec="libx264", acodec="aac", extra=["-pix_fmt", "yuv444p"])
    profile = inspect_streams(ten_bit)
    assert profile is not None and not profile.video_ok
    fixed = ensure_streamable(ten_bit)
    assert inspect_streams(fixed).pix_fmt == "yuv420p"  # type: ignore[union-attr]
    _assert_playable(fixed)


def test_audio_and_unreadable_files_are_returned_untouched(tmp_path):
    mp3 = tmp_path / "a.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1", str(mp3)],
        check=True,
    )
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video")
    assert ensure_streamable(mp3) == mp3
    assert ensure_streamable(junk) == junk
    assert junk.read_bytes() == b"not a video"


def test_a_failed_conversion_keeps_the_original(tmp_path, monkeypatch):
    slow = _make(tmp_path / "slow.mp4", vcodec="libx264", acodec="aac")
    original = slow.read_bytes()

    def boom(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "ffmpeg")

    real_run = subprocess.run
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, *a, **k: boom() if cmd[0] == "ffmpeg" else real_run(cmd, *a, **k)
    )
    assert ensure_streamable(slow) == slow
    assert slow.read_bytes() == original
    assert [p.name for p in tmp_path.iterdir()] == ["slow.mp4"]


# --------------------------------------------------------------------------
# displayed size of rotated phone footage
# --------------------------------------------------------------------------
def test_probe_reports_displayed_dimensions_of_rotated_video(tmp_path):
    src = _make(tmp_path / "land.mp4", vcodec="libx264", acodec="aac", size="320x240")
    rotated = tmp_path / "rot.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-display_rotation", "90", "-i", str(src), "-c", "copy", str(rotated)],
        check=True,
    )
    info = probe(rotated)
    assert (info.width, info.height) == (240, 320)


# --------------------------------------------------------------------------
# end to end: what the pipeline actually hands to Telegram
# --------------------------------------------------------------------------
class _SnapshotClient(FakeTelegramClient):
    """Records what is inside the file at the moment it is 'uploaded'."""

    def __init__(self) -> None:
        super().__init__()
        self.uploaded: list[dict] = []

    async def send_file(self, entity, file, **kwargs):
        if isinstance(file, str) and entity == 1:
            path = Path(file)
            self.uploaded.append(
                {
                    "streams": _streams(path),
                    "moov_first": moov_before_mdat(path),
                    "suffix": path.suffix,
                    "probe": probe(path),
                }
            )
        return await super().send_file(entity, file, **kwargs)


class _Engine(BaseEngine):
    def __init__(self, source: Path) -> None:
        self._source = source

    def matches(self, url: str) -> bool:
        return True

    async def download(self, url, *, dest_dir, cancel_token=None) -> DownloadResult:
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / self._source.name
        shutil.copy(self._source, target)
        return DownloadResult(file_paths=[str(target)], title="t")


class _Progress:
    async def update(self, text: str, **kwargs) -> None:
        pass


@pytest.fixture
def credits_service():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(User(user_id=1, free=50, paid=0, bandwidth_used=0, total_bandwidth=0, is_blocked=0))
        session.commit()
    return CreditsService(factory, enable_vip=True, owner_ids=[], free_bandwidth=10**12)


async def _deliver(credits_service, tmp_path, source: Path, delivery: DeliveryOptions | None = None):
    client = _SnapshotClient()
    uploader = TelethonUploader(client, chat_id=1, archive_channel="@archive")
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path / "dl")
    await pipeline.run(
        user_id=1,
        url="https://youtu.be/x",
        engine=_Engine(source),
        uploader=uploader,
        progress=_Progress(),
        archive_channel="@archive",
        delivery=delivery,
    )
    return client


@needs_vp9
@needs_opus
async def test_a_vp9_opus_download_reaches_telegram_as_playable_h264_aac(credits_service, tmp_path):
    bad = _make(tmp_path / "bad.mp4", vcodec="libvpx-vp9", acodec="libopus", extra=["-movflags", "+faststart"])
    client = await _deliver(credits_service, tmp_path, bad)

    (uploaded,) = client.uploaded
    assert uploaded["streams"] == {"video": "h264", "audio": "aac"}
    assert uploaded["moov_first"] is True
    assert uploaded["suffix"] == ".mp4"

    kwargs = client.send_files(1)[0].kwargs
    assert kwargs["supports_streaming"] is True and kwargs["force_document"] is False
    (attribute,) = kwargs["attributes"]
    assert isinstance(attribute, DocumentAttributeVideo)
    probed = uploaded["probe"]  # ffprobe of the very file that was sent
    assert (attribute.duration, attribute.w, attribute.h) == (probed.duration, probed.width, probed.height)
    assert (attribute.w, attribute.h) == (320, 240) and attribute.duration in (1, 2)
    assert attribute.supports_streaming is True


async def test_a_moov_at_the_end_download_reaches_telegram_with_moov_first(credits_service, tmp_path):
    slow = _make(tmp_path / "slow.mp4", vcodec="libx264", acodec="aac")
    client = await _deliver(credits_service, tmp_path, slow)
    (uploaded,) = client.uploaded
    assert uploaded["streams"] == {"video": "h264", "audio": "aac"}
    assert uploaded["moov_first"] is True


@needs_vp9
@needs_opus
async def test_file_mode_still_delivers_the_bytes_untouched(credits_service, tmp_path):
    bad = _make(tmp_path / "bad.mp4", vcodec="libvpx-vp9", acodec="libopus")
    client = await _deliver(credits_service, tmp_path, bad, DeliveryOptions(send_as=SEND_AS_DOCUMENT))
    (uploaded,) = client.uploaded
    assert uploaded["streams"] == {"video": "vp9", "audio": "opus"}
    assert client.send_files(1)[0].kwargs["force_document"] is True
