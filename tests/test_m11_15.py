"""Regression tests for M11.15 (production incident 2026-10-01):

(A) A direct-link WebP photo (`picture.ashx`, no useful extension) was
    treated as a video: ffprobe reports a single-frame WebP as a "video"
    stream (codec_name=webp), so `ensure_streamable` tried an H.264
    re-encode, which failed ("width not divisible by 2") and the raw file
    was then sent with no extension, no caption, no preview.
(B) `ReplyMarkupInvalidError` on the progress message's final edit, on
    essentially every successful download - `_CLEAR_BUTTONS` was an empty
    `ReplyInlineMarkup([])`, which live Telegram rejects outright.
(C) A ytmp3 API key hardcoded as the config default, appearing in plaintext
    in provider-failure log lines (`...api_key=9b0ed5dab31616027ad7154140b0272d...`).

(B) has its own dedicated tests in tests/test_m11_9.py; this file covers (A)
and (C), plus the odd-dimension re-encode fix that rides along with (A).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Base, User
from media_bot_v2.engines.base import BaseEngine, DownloadResult
from media_bot_v2.engines.youtube import YouTubeDownloadError, YouTubeEngine
from media_bot_v2.providers.base import BaseProvider, ProviderResult, mask_secrets
from media_bot_v2.providers.health import ProviderHealthTracker
from media_bot_v2.providers.registry import ProviderRegistry
from media_bot_v2.providers.ytmp3 import YTmp3Provider
from media_bot_v2.telegram.delivery import SEND_AS_DOCUMENT, DeliveryOptions
from media_bot_v2.telegram.uploader import TelethonUploader
from media_bot_v2.upload.media_probe import (
    KIND_PHOTO,
    correct_extension,
    is_animated_webp,
    probe,
)
from media_bot_v2.upload.streamable import convert_animated_webp, ensure_streamable
from tests.fakes_telegram import FakeTelegramClient

pytestmark_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not installed"
)

REAL_WEBP = Path("/home/vm/.hermes/cache/documents/doc_76d1e952b8e7_picture.ashx")

# ==============================================================================
# A.1 - the real production file: a static WebP photo with a meaningless
# ".ashx" extension, exactly as it reached the bot.
# ==============================================================================


@pytest.mark.skipif(not REAL_WEBP.exists(), reason="sample file not present on this machine")
def test_real_webp_sample_is_identified_as_photo_not_video():
    info = probe(REAL_WEBP)
    assert info.kind == KIND_PHOTO


@pytest.mark.skipif(not REAL_WEBP.exists(), reason="sample file not present on this machine")
def test_real_webp_sample_ffprobe_reports_it_as_a_video_stream():
    """The actual trap: ffprobe alone (without the filetype.guess check that
    `probe()`/`ensure_streamable()` now do first) calls this a video."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(REAL_WEBP)],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    streams = json.loads(out.stdout)["streams"]
    assert streams == [{"codec_type": "video", "codec_name": "webp"}]


@pytest.mark.skipif(not REAL_WEBP.exists(), reason="sample file not present on this machine")
def test_real_webp_sample_is_not_detected_as_animated():
    assert is_animated_webp(REAL_WEBP) is False


@pytestmark_ffmpeg
@pytest.mark.skipif(not REAL_WEBP.exists(), reason="sample file not present on this machine")
def test_ensure_streamable_short_circuits_on_the_real_webp_sample_without_touching_it(tmp_path, caplog):
    """The backstop in ensure_streamable itself (defense in depth even though
    the pipeline now never calls it for a photo): no H.264 attempt, no
    traceback, original bytes untouched."""
    copy = tmp_path / "picture.ashx"
    copy.write_bytes(REAL_WEBP.read_bytes())
    original_bytes = copy.read_bytes()

    import logging

    with caplog.at_level(logging.WARNING):
        result = ensure_streamable(copy)

    assert result == copy
    assert copy.read_bytes() == original_bytes
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)
    assert not any(r.exc_info for r in caplog.records)


# ==============================================================================
# A.2 - extension correction: a file downloaded under a meaningless name gets
# renamed to match what it actually is.
# ==============================================================================


@pytest.mark.skipif(not REAL_WEBP.exists(), reason="sample file not present on this machine")
def test_correct_extension_renames_ashx_to_webp(tmp_path):
    copy = tmp_path / "picture.ashx"
    copy.write_bytes(REAL_WEBP.read_bytes())

    fixed = correct_extension(copy)

    assert fixed.name == "picture.webp"
    assert fixed.exists()
    assert not copy.exists()
    assert fixed.read_bytes() == REAL_WEBP.read_bytes()


def test_correct_extension_is_a_noop_when_already_correct(tmp_path):
    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32)
    before = mp4.read_bytes()

    assert correct_extension(mp4) == mp4
    assert mp4.read_bytes() == before


def test_correct_extension_is_a_noop_when_detection_fails(tmp_path):
    junk = tmp_path / "file.ashx"
    junk.write_bytes(b"not a known filetype at all")

    assert correct_extension(junk) == junk
    assert junk.exists()


def test_correct_extension_does_not_clobber_an_existing_target(tmp_path):
    webp_bytes = REAL_WEBP.read_bytes() if REAL_WEBP.exists() else None
    if webp_bytes is None:
        pytest.skip("sample file not present on this machine")
    collide = tmp_path / "picture.ashx"
    collide.write_bytes(webp_bytes)
    (tmp_path / "picture.webp").write_bytes(b"already here")

    result = correct_extension(collide)

    assert result == collide  # left untouched rather than overwriting
    assert (tmp_path / "picture.webp").read_bytes() == b"already here"


# ==============================================================================
# A.3 - odd-dimension video re-encode (the general bug behind the specific
# "width not divisible by 2" ffmpeg failure): a scale filter rounds the
# dimensions down to even before libx264 ever sees them.
# ==============================================================================


def _encoders() -> str:
    return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, check=False).stdout


def _make_odd(path: Path, *, size: str) -> Path:
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=size={size}:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440",
        "-t", "1", "-shortest", "-c:v", "libvpx-vp9", "-c:a", "libopus",
        str(path),
    ]  # fmt: skip
    subprocess.run(cmd, check=True)
    return path


def _streams(path: Path) -> dict[str, str]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    return json.loads(out.stdout)["streams"]


@pytestmark_ffmpeg
@pytest.mark.skipif("libvpx-vp9" not in _encoders() or "libopus" not in _encoders(), reason="vp9/opus missing")
def test_odd_dimension_video_is_reencoded_successfully_not_failed(tmp_path):
    """The exact production shape: 343x274 (odd width) in a non-H.264
    container. Before the scale filter this failed with libx264's
    "width not divisible by 2" and the file was returned untouched/not
    streamable; now it must succeed and come out even-dimensioned."""
    bad = _make_odd(tmp_path / "odd.webm", size="343x274")

    fixed = ensure_streamable(bad)

    streams = _streams(fixed)
    video = next(s for s in streams if s["codec_type"] == "video")
    assert video["codec_name"] == "h264"
    assert video["width"] % 2 == 0 and video["height"] % 2 == 0
    # Rounded down from 343, not stretched/cropped to an unrelated size.
    assert video["width"] == 342 and video["height"] == 274
    assert fixed.suffix == ".mp4"


# ==============================================================================
# A.4 - animated WebP: converted to MP4 when possible, otherwise sent as a
# document with its real extension - never crashes, never sent as a "photo".
# ==============================================================================


@pytestmark_ffmpeg
def test_animated_webp_is_detected_via_the_anim_chunk(tmp_path):
    anim = tmp_path / "anim.webp"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=5",
        "-t", "1", "-pix_fmt", "yuva420p", "-loop", "0", "-c:v", "libwebp_anim", str(anim),
    ]  # fmt: skip
    subprocess.run(cmd, check=True)
    assert is_animated_webp(anim) is True


@pytestmark_ffmpeg
def test_convert_animated_webp_falls_back_to_none_without_raising_when_undecodable(tmp_path, caplog):
    """On this host's ffmpeg build the bundled WebP decoder does not support
    ANIM/ANMF chunks at all ("skipping unsupported chunk") - convert_animated_webp
    must report that as None, never raise, and never leave a temp file behind."""
    anim = tmp_path / "anim.webp"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=5",
        "-t", "1", "-pix_fmt", "yuva420p", "-loop", "0", "-c:v", "libwebp_anim", str(anim),
    ]  # fmt: skip
    subprocess.run(cmd, check=True)

    result = convert_animated_webp(anim)

    assert result is None
    assert [p.name for p in tmp_path.iterdir()] == ["anim.webp"]  # no leftover temp file


def test_is_animated_webp_false_for_junk_and_missing_files(tmp_path):
    junk = tmp_path / "not-a-webp.bin"
    junk.write_bytes(b"whatever")
    assert is_animated_webp(junk) is False
    assert is_animated_webp(tmp_path / "missing.webp") is False


# ==============================================================================
# A.5 - end to end through the pipeline: what actually reaches "Telegram".
# ==============================================================================


class _WebpEngine(BaseEngine):
    def __init__(self, source: Path, *, filename: str = "picture.ashx") -> None:
        self._source = source
        self._filename = filename

    def matches(self, url: str) -> bool:
        return True

    async def download(self, url, *, dest_dir: Path, cancel_token=None) -> DownloadResult:
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / self._filename
        shutil.copy(self._source, target)
        return DownloadResult(file_paths=[str(target)], title="t")


class _Progress:
    def __init__(self) -> None:
        self.updates: list[str] = []

    async def update(self, text: str, **kwargs) -> None:
        self.updates.append(text)


@pytest.fixture
def credits_service():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(User(user_id=1, free=50, paid=0, bandwidth_used=0, total_bandwidth=0, is_blocked=0))
        session.commit()
    return CreditsService(factory, enable_vip=True, owner_ids=[], free_bandwidth=10**12)


async def _deliver(credits_service, tmp_path, source: Path, *, filename="picture.ashx", delivery=None):
    from media_bot_v2.pipeline import DownloadPipeline

    client = FakeTelegramClient()
    uploader = TelethonUploader(client, chat_id=1, archive_channel="@archive")
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path / "dl")
    progress = _Progress()
    await pipeline.run(
        user_id=1,
        url="https://www.zbeng.co.il/picture.ashx?customerId=681784&number=3",
        engine=_WebpEngine(source, filename=filename),
        uploader=uploader,
        progress=progress,
        archive_channel="@archive",
        delivery=delivery,
    )
    return client, progress


@pytestmark_ffmpeg
@pytest.mark.skipif(not REAL_WEBP.exists(), reason="sample file not present on this machine")
async def test_real_webp_download_reaches_telegram_as_a_photo_with_the_right_extension(credits_service, tmp_path):
    client, _progress = await _deliver(credits_service, tmp_path, REAL_WEBP)

    sent = client.send_files(1)[0]
    assert sent.kwargs["force_document"] is False  # Telegram "photo", not raw bytes
    assert sent.args[1].endswith("picture.webp")  # renamed from .ashx, caption/preview can work
    assert "supports_streaming" not in sent.kwargs  # never routed through the video path
    assert "attributes" not in sent.kwargs or not any(
        type(a).__name__ == "DocumentAttributeVideo" for a in sent.kwargs.get("attributes", [])
    )


@pytestmark_ffmpeg
@pytest.mark.skipif(not REAL_WEBP.exists(), reason="sample file not present on this machine")
async def test_real_webp_download_as_document_setting_still_sends_the_photo_bytes(credits_service, tmp_path):
    """Static photos are sent as photos regardless of the "send as file"
    setting (pre-existing, unrelated-to-this-bug behavior) - just confirming
    the new routing didn't change that."""
    client, _ = await _deliver(
        credits_service, tmp_path, REAL_WEBP, delivery=DeliveryOptions(send_as=SEND_AS_DOCUMENT)
    )
    sent = client.send_files(1)[0]
    assert sent.kwargs["force_document"] is False


# ==============================================================================
# C - ytmp3 API key: no hardcoded default, provider skipped when unconfigured,
# secrets masked out of logs.
# ==============================================================================


def test_ytmp3_has_no_hardcoded_default_key():
    assert YTmp3Provider().api_key is None
    assert YTmp3Provider().is_configured is False
    assert YTmp3Provider(api_key="x").is_configured is True


async def test_ytmp3_without_api_key_raises_before_any_network_call():
    provider = YTmp3Provider(api_key=None)
    with patch("media_bot_v2.providers.ytmp3.requests.get") as mock_get, pytest.raises(Exception, match="not configured"):
        await provider.fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    mock_get.assert_not_called()


def test_registry_never_attempts_ytmp3_without_a_key():
    """Mirrors item 2 of the brief precisely: no key -> not in the candidate
    list at all, so the provider-fallback loop in youtube.py never calls
    .fetch() on it - no request, no failure, no cooldown, no log line."""
    registry = ProviderRegistry(youtube_order=["ytmp3", "cobalt"])
    registry.register(YTmp3Provider(api_key=None))
    assert [p.name for p in registry.get_providers_for_platform("youtube")] == []


# -- secret masking -----------------------------------------------------------


def test_mask_secrets_redacts_the_real_production_log_line():
    """The exact log line from the production incident."""
    real_line = (
        "ytmp3 auth request failed: 403 Client Error: Forbidden for url: "
        "https://gamma.gammacloud.net/api/v1/auth?api_key=9b0ed5dab31616027ad7154140b0272d&_=1727701234567"
    )
    masked = mask_secrets(real_line)

    assert "9b0ed5dab31616027ad7154140b0272d" not in masked
    assert "api_key=***" in masked
    assert "https://gamma.gammacloud.net/api/v1/auth" in masked  # non-secret context preserved
    assert "_=1727701234567" in masked  # non-sensitive params untouched


@pytest.mark.parametrize(
    "param",
    ["api_key", "API_KEY", "apikey", "token", "signature", "sig", "password", "auth"],
)
def test_mask_secrets_covers_every_listed_sensitive_param(param):
    masked = mask_secrets(f"https://x.example/path?{param}=SECRETVALUE&other=1")
    assert "SECRETVALUE" not in masked
    assert "other=1" in masked


def test_mask_secrets_redacts_bearer_tokens():
    masked = mask_secrets("Authorization: Bearer abc123.def456-ghi")
    assert "abc123.def456-ghi" not in masked
    assert "Bearer ***" in masked


def test_mask_secrets_noop_on_empty_and_clean_text():
    assert mask_secrets("") == ""
    assert mask_secrets("plain error, nothing sensitive here") == "plain error, nothing sensitive here"


async def test_youtube_provider_fallback_logs_masked_url_not_the_real_key(caplog):
    """End-to-end through the actual call site: a provider that fails with a
    requests exception embedding the real URL+key must never put the raw key
    into the logger.warning call or provider_health.last_error."""
    import logging

    class _LeakyProvider(BaseProvider):
        name = "leaky"
        supported_platforms = ("youtube",)

        def matches(self, url: str) -> bool:
            return True

        async def fetch(self, url: str) -> ProviderResult:
            raise requests.exceptions.HTTPError(
                "403 Client Error: Forbidden for url: "
                "https://gamma.gammacloud.net/api/v1/auth?api_key=9b0ed5dab31616027ad7154140b0272d&_=123"
            )

    registry = ProviderRegistry(youtube_order=["leaky"])
    registry.register(_LeakyProvider())
    db_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(db_engine)
    factory = sessionmaker(bind=db_engine)
    tracker = ProviderHealthTracker(factory)

    engine = YouTubeEngine(
        quality="720", max_download_size=10**9, registry=registry, health_tracker=tracker
    )

    with caplog.at_level(logging.WARNING), pytest.raises(YouTubeDownloadError):
        await engine._try_fallback_providers(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ", Path("/tmp")
        )

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "9b0ed5dab31616027ad7154140b0272d" not in log_text

    from media_bot_v2.db.models import ProviderHealth
    from media_bot_v2.db.session import session_scope

    with session_scope(factory) as session:
        record = session.query(ProviderHealth).filter_by(provider="leaky", platform="youtube").one()
        assert "9b0ed5dab31616027ad7154140b0272d" not in (record.last_error or "")
