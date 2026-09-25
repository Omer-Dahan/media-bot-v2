"""M9.1: conversion timeout never breaks delivery, split parts stream, and
pre-fix cache rows are retired."""

import asyncio
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from media_bot_v2.cache.video_cache import VideoCacheStore, compute_cache_key
from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Base, User
from media_bot_v2.engines.base import BaseEngine, DownloadResult
from media_bot_v2.pipeline import DownloadPipeline
from media_bot_v2.telegram import texts
from media_bot_v2.upload import splitter, streamable
from media_bot_v2.upload.streamable import moov_before_mdat
from tests.test_pipeline import _FakeProgress, _FakeUploader

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not installed"
)


def _make_mpeg4_mp4(path: Path, seconds: int = 2) -> Path:
    """A file that needs re-encoding (MPEG-4 part 2, not H.264)."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration={seconds}",
         "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-g", "5", str(path)],
        check=True,
    )  # fmt: skip
    return path


class _VideoEngine(BaseEngine):
    def __init__(self, calls: list[str] | None = None):
        self.calls = calls if calls is not None else []

    def matches(self, url: str) -> bool:
        return True

    async def download(self, url: str, *, dest_dir: Path) -> DownloadResult:
        self.calls.append(url)
        dest_dir.mkdir(parents=True, exist_ok=True)
        return DownloadResult(file_paths=[str(_make_mpeg4_mp4(dest_dir / "clip.mp4"))], title="clip")


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(User(user_id=1, free=3, paid=0, bandwidth_used=0, total_bandwidth=0, is_blocked=0))
        session.commit()
    return factory


@pytest.fixture
def credits_service(session_factory):
    return CreditsService(session_factory, enable_vip=True, owner_ids=[], free_bandwidth=2_000_000_000)


# --------------------------------------------------------------------------
# 1. conversion timeout -> original is sent, request succeeds
# --------------------------------------------------------------------------
async def test_conversion_timeout_sends_the_original_and_does_not_fail_the_request(
    session_factory, credits_service, tmp_path, monkeypatch
):
    marker = "31.415"  # unique sleep duration so we can look for orphans
    monkeypatch.setattr(streamable, "build_fix_command", lambda *a, **k: ["sleep", marker])

    # Conversion budget (0.8s) < upload budget (2s): the hung "ffmpeg" is
    # killed at 0.8s and the upload still gets its full 2s. Before M9.1 the
    # conversion ran inside the upload budget.
    pipeline = DownloadPipeline(
        credits_service=credits_service, download_dir=tmp_path, upload_timeout=2.0, convert_timeout=0.8
    )
    uploader = _FakeUploader()
    progress = _FakeProgress()

    await pipeline.run(user_id=1, url="http://x", engine=_VideoEngine(), uploader=uploader, progress=progress)

    assert [p.name for p in uploader.sent] == ["clip.mp4"]  # the original, unconverted
    assert texts.REQUEST_TIMEOUT_EXCEEDED not in progress.updates
    assert progress.updates[-1] == texts.DOWNLOAD_DONE
    with session_factory() as session:
        assert session.query(User).filter(User.user_id == 1).one().free == 2  # delivered -> charged
    orphans = await asyncio.to_thread(
        subprocess.run, ["pgrep", "-f", f"sleep {marker}"], capture_output=True, text=True, check=False
    )
    assert orphans.stdout.strip() == ""  # the child was killed, not orphaned


async def test_conversion_budget_is_smaller_than_the_upload_budget_by_default(credits_service, tmp_path):
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path, upload_timeout=600.0)
    assert pipeline._fix_timeout() < 600.0
    pipeline = DownloadPipeline(
        credits_service=credits_service, download_dir=tmp_path, upload_timeout=600.0, convert_timeout=90.0
    )
    assert pipeline._fix_timeout() == 90.0


async def test_unexpected_conversion_error_still_delivers(session_factory, credits_service, tmp_path, monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("media_bot_v2.pipeline.ensure_streamable", explode)
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    uploader = _FakeUploader()
    await pipeline.run(user_id=1, url="http://x", engine=_VideoEngine(), uploader=uploader, progress=_FakeProgress())
    assert [p.name for p in uploader.sent] == ["clip.mp4"]


async def test_real_conversion_still_happens_within_budget(credits_service, tmp_path):
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path, convert_timeout=60.0)
    uploader = _FakeUploader()
    await pipeline.run(user_id=1, url="http://x", engine=_VideoEngine(), uploader=uploader, progress=_FakeProgress())
    assert len(uploader.sent) == 1  # (file is deleted after delivery; conversion covered in test_streamable)


# --------------------------------------------------------------------------
# 2. split parts stream
# --------------------------------------------------------------------------
def test_every_split_part_has_moov_before_mdat(tmp_path):
    src = tmp_path / "big.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=12",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "5", "-keyint_min", "5", "-movflags", "+faststart", str(src)],
        check=True,
    )  # fmt: skip
    limit = src.stat().st_size // 3

    parts = splitter.split_file(src, limit=limit)

    assert len(parts) > 1
    for part in parts:
        assert part.suffix == ".mp4"
        assert moov_before_mdat(part), f"{part.name} has moov after mdat"
        subprocess.run(["ffprobe", "-v", "error", str(part)], check=True)


def test_split_without_the_flag_would_have_moov_at_the_end(tmp_path):
    """Guards the test above against passing vacuously."""
    src = tmp_path / "x.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "5", str(src)],
        check=True,
    )  # fmt: skip
    out = tmp_path / "seg%d.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-c", "copy", "-f", "segment", "-segment_time", "2", str(out)],
        check=True,
    )  # fmt: skip
    assert not moov_before_mdat(tmp_path / "seg0.mp4")


# --------------------------------------------------------------------------
# 3. cache key version
# --------------------------------------------------------------------------
def test_pre_fix_cache_key_is_no_longer_produced():
    legacy = hashlib.md5(b"abc123:720", usedforsecurity=False).hexdigest()
    assert compute_cache_key("abc123", "720") != legacy
    assert compute_cache_key("abc123", "720", "document") != hashlib.md5(b"abc123:720:document").hexdigest()


async def test_old_cache_row_is_not_served_and_a_fresh_download_is_billed_then_cached(
    session_factory, credits_service, tmp_path
):
    store = VideoCacheStore(session_factory)
    legacy = hashlib.md5(b"abc123:720", usedforsecurity=False).hexdigest()
    store.put(legacy, archive_chat="@archive", message_ids=[7], title="old AV1 upload")
    key = compute_cache_key("abc123", "720")
    assert store.get(key) is None
    assert store.get(legacy) is not None  # nothing was deleted

    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    engine_calls: list[str] = []
    uploader = _FakeUploader()
    await pipeline.run(
        user_id=1, url="http://x", engine=_VideoEngine(engine_calls), uploader=uploader,
        progress=_FakeProgress(), cache=store, cache_key=key, archive_channel="@archive",
    )  # fmt: skip
    assert engine_calls == ["http://x"] and uploader.cached_sends == []
    assert len(uploader.sent) == 1
    with session_factory() as session:
        assert session.query(User).filter(User.user_id == 1).one().free == 2  # real download is billed
    assert store.get(key) is not None  # new row under the new key

    # asking again is a free cache delivery
    await pipeline.run(
        user_id=1, url="http://x", engine=_VideoEngine(engine_calls), uploader=uploader,
        progress=_FakeProgress(), cache=store, cache_key=key, archive_channel="@archive",
    )  # fmt: skip
    assert engine_calls == ["http://x"] and len(uploader.cached_sends) == 1
    with session_factory() as session:
        assert session.query(User).filter(User.user_id == 1).one().free == 2
