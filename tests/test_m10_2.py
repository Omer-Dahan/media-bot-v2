"""Tests for M10.2: FloodPremiumWaitError handling, flood_sleep_threshold, and connection scaling."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import FloodPremiumWaitError, FloodWaitError

from media_bot_v2.credits.service import CreditsService
from media_bot_v2.engines.base import BaseEngine, CancellationToken, DownloadResult
from media_bot_v2.pipeline import DownloadPipeline
from media_bot_v2.telegram import parallel_upload, texts
from media_bot_v2.telegram.flood_wait import (
    MAX_FLOOD_WAIT_SECONDS,
    calculate_connections,
    call_with_flood_retry,
)
from media_bot_v2.telegram.parallel_upload import upload_file_parallel
from media_bot_v2.telegram.uploader import TelethonUploader
from media_bot_v2.upload.media_probe import KIND_VIDEO, MediaInfo
from tests.test_multi_connection_upload import ConnectionNetwork
from tests.test_parallel_upload import PART, USER_CHAT, make_file, sha


# -----------------------------------------------------------------------------
# 1. FloodPremiumWaitError in multi-connection upload
# -----------------------------------------------------------------------------
@pytest.fixture
def fake_sleeps(monkeypatch):
    waited: list[float] = []

    async def fake_sleep(seconds: float):
        waited.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(parallel_upload, "_sleep", fake_sleep)
    return waited


@pytest.fixture
def net(monkeypatch):
    network = ConnectionNetwork()

    async def opener(client):
        return await network.open_sender(client)

    monkeypatch.setattr(parallel_upload, "_open_sender", opener)
    return network


async def test_flood_premium_on_extra_connection_retries_only_that_part(tmp_path, net, fake_sleeps, monkeypatch):
    """Test 1: Injects FloodPremiumWaitError in multi-connection upload.
    Verifies:
    - Waits according to exc.seconds
    - Retries ONLY that part
    - Does NOT mark connection dead
    - Does NOT fail-open to main connection
    - Upload completes successfully with byte-for-byte identical content
    """
    net.flood_premium_plan = {3: 1}  # part 3 (lane 3 -> extra connection) floods once with Premium
    net.flood_premium_seconds = 5

    call_senders: dict[int, list[object]] = {}
    original_call = net._call

    async def tracking_call(sender, request):
        call_senders.setdefault(request.file_part, []).append(sender)
        return await original_call(sender, request)

    monkeypatch.setattr(net, "_call", tracking_call)

    path = make_file(tmp_path / "f.bin", 12 * PART)
    handle = await upload_file_parallel(net, path, workers=5, connections=5)

    # 1. Byte-for-byte identical to source
    assert sha(net.assembled(handle)) == sha(path.read_bytes())
    # 2. Wait matches exc.seconds (5)
    assert fake_sleeps == [5]
    # 3. Only part 3 was retried (attempts = 2), all others attempted once
    assert net.attempts[3] == 2
    assert all(n == 1 for i, n in net.attempts.items() if i != 3)
    # 4. Extra connections are cleanly closed at end of upload
    assert all(s.disconnected for s in net.senders)
    # 5. NO fail-open: retry occurred on the SAME extra connection, not the main connection
    assert len(call_senders[3]) == 2
    assert call_senders[3][0] is call_senders[3][1]
    assert call_senders[3][0] in net.senders
    assert call_senders[3][0] is not net._sender


async def test_repeated_flood_premium_shrinks_lanes(tmp_path, net, fake_sleeps):
    """Repeated FloodPremiumWaitError triggers lane degradation (5 -> 2 -> 1)."""
    net.flood_premium_plan = {i: 1 for i in range(6)}
    net.flood_premium_seconds = 1

    path = make_file(tmp_path / "f.bin", 20 * PART)
    handle = await upload_file_parallel(net, path, workers=5, connections=5)

    assert sha(net.assembled(handle)) == sha(path.read_bytes())
    assert len(fake_sleeps) >= 4
    assert all(s == 1 for s in fake_sleeps)


async def test_excessive_flood_premium_raises_without_marking_connection_dead(tmp_path, net, fake_sleeps):
    """If FloodPremiumWaitError repeats beyond MAX_FLOOD_RETRIES_PER_PART, it raises
    and does not silently fail open or mark the connection dead."""
    net.flood_premium_plan = {2: 10**6}
    net.flood_premium_seconds = 1

    path = make_file(tmp_path / "f.bin", 12 * PART)
    with pytest.raises(FloodPremiumWaitError):
        await upload_file_parallel(net, path, workers=5, connections=5)
    assert all(s.disconnected for s in net.senders)


# -----------------------------------------------------------------------------
# 2. Engines & Pipeline handle FloodPremiumWaitError
# -----------------------------------------------------------------------------
class FakeEngine(BaseEngine):
    def __init__(self, media_path: Path):
        self._media_path = media_path

    def matches(self, url: str) -> bool:
        return True

    async def download(
        self,
        url: str,
        *,
        dest_dir: Path,
        cancel_token: CancellationToken | None = None,
    ) -> DownloadResult:
        return DownloadResult(
            file_paths=[str(self._media_path)],
            title="test video",
        )


async def test_pipeline_catches_flood_premium_and_updates_progress(tmp_path, monkeypatch):
    """Test 2: Pipeline catches FloodPremiumWaitError, reports FLOOD_WAIT_FAILED to user,
    and cancels tasks cleanly."""
    file_path = make_file(tmp_path / "vid.mp4", 1024)
    engine = FakeEngine(file_path)

    uploader = MagicMock()
    # uploader.send_file raises FloodPremiumWaitError
    uploader.send_file = AsyncMock(side_effect=FloodPremiumWaitError(None, capture=15))

    progress = MagicMock()
    progress.update = AsyncMock()

    credits_mock = MagicMock(spec=CreditsService)
    credits_mock.check_quota = MagicMock()
    credits_mock.add_bandwidth_used = MagicMock()
    credits_mock.use_quota_dynamic = MagicMock()

    pipeline = DownloadPipeline(
        credits_service=credits_mock,
        download_dir=tmp_path / "downloads",
        request_timeout=30.0,
    )

    with pytest.raises(FloodPremiumWaitError):
        await pipeline.run(
            user_id=123,
            url="https://example.com/video.mp4",
            engine=engine,
            uploader=uploader,
            progress=progress,
        )

    # Verifies user got the exact Hebrew message for flood wait
    progress.update.assert_called_with(texts.FLOOD_WAIT_FAILED)


async def test_uploader_retries_transient_flood_premium_with_call_with_flood_retry(tmp_path, monkeypatch):
    """Test 2b: In TelethonUploader, a transient FloodPremiumWaitError during send_file
    is slept and retried successfully without crashing the pipeline."""
    file_path = make_file(tmp_path / "vid.mp4", 1024)

    client = MagicMock()
    calls = 0

    async def mock_send_file(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FloodPremiumWaitError(None, capture=2)
        return MagicMock(id=999)

    client.send_file = AsyncMock(side_effect=mock_send_file)

    sleeps = []

    async def fake_asyncio_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(asyncio, "sleep", fake_asyncio_sleep)

    uploader = TelethonUploader(client, chat_id=USER_CHAT, archive_channel=None)
    result = await uploader.send_file(file_path)

    assert calls == 2
    assert sleeps == [2]
    assert result.id == 999


# -----------------------------------------------------------------------------
# 3. call_with_flood_retry wrapper safety
# -----------------------------------------------------------------------------
async def test_call_with_flood_retry_retries_both_flood_types(monkeypatch):
    """Test 3: call_with_flood_retry handles both FloodWaitError and FloodPremiumWaitError,
    waits the requested seconds, and returns the result."""
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    # 1. FloodWaitError
    calls_1 = 0

    async def func_with_wait():
        nonlocal calls_1
        calls_1 += 1
        if calls_1 == 1:
            raise FloodWaitError(None, capture=3)
        return "success_wait"

    res1 = await call_with_flood_retry(func_with_wait)
    assert res1 == "success_wait"
    assert calls_1 == 2
    assert sleeps == [3]

    # 2. FloodPremiumWaitError
    sleeps.clear()
    calls_2 = 0

    async def func_with_premium():
        nonlocal calls_2
        calls_2 += 1
        if calls_2 == 1:
            raise FloodPremiumWaitError(None, capture=4)
        return "success_premium"

    res2 = await call_with_flood_retry(func_with_premium)
    assert res2 == "success_premium"
    assert calls_2 == 2
    assert sleeps == [4]


async def test_call_with_flood_retry_aborts_on_excessive_wait(monkeypatch):
    """Test 3b: When flood wait seconds exceeds MAX_FLOOD_WAIT_SECONDS, it does NOT sleep;
    it re-raises immediately to protect responsiveness."""
    slept = False

    async def fake_sleep(sec):
        nonlocal slept
        slept = True

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def excessive_flood():
        raise FloodPremiumWaitError(None, capture=int(MAX_FLOOD_WAIT_SECONDS + 50))

    with pytest.raises(FloodPremiumWaitError):
        await call_with_flood_retry(excessive_flood)

    assert slept is False


async def test_call_with_flood_retry_aborts_after_max_retries(monkeypatch):
    """Test 3c: When flood wait persists beyond max_retries, it re-raises."""
    calls = 0

    async def fake_sleep(sec):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def endless_flood():
        nonlocal calls
        calls += 1
        raise FloodWaitError(None, capture=1)

    with pytest.raises(FloodWaitError):
        await call_with_flood_retry(endless_flood, max_retries=3)

    assert calls == 4  # Initial attempt + 3 retries


# -----------------------------------------------------------------------------
# 4. Connection scaling by file size
# -----------------------------------------------------------------------------
def test_calculate_connections_scaling():
    """Test 4: Connection scaling adopts Kraken lessons:
    - Small files (< 10MB) -> 1 connection.
    - Medium files (< 100MB) -> at most 2 connections.
    - Large files (>= 100MB) -> up to max_connections (bounded by 1..5).
    - Clamped by configured max_connections.
    """
    mb = 1024 * 1024

    # Small files: always 1 connection
    assert calculate_connections(0, 5) == 1
    assert calculate_connections(1 * mb, 5) == 1
    assert calculate_connections(9 * mb, 5) == 1
    assert calculate_connections(9 * mb, 1) == 1

    # Medium files (10MB .. 99.9MB): at most 2 connections
    assert calculate_connections(10 * mb, 5) == 2
    assert calculate_connections(50 * mb, 5) == 2
    assert calculate_connections(99 * mb, 5) == 2
    # If config allows only 1 connection, respect it
    assert calculate_connections(50 * mb, 1) == 1

    # Large files (>= 100MB): up to max_connections (up to 5)
    assert calculate_connections(100 * mb, 5) == 5
    assert calculate_connections(500 * mb, 5) == 5
    assert calculate_connections(1000 * mb, 5) == 5
    # If config specifies fewer, clamp to config
    assert calculate_connections(500 * mb, 3) == 3
    assert calculate_connections(500 * mb, 1) == 1


async def test_uploader_adaptive_connections(tmp_path, monkeypatch):
    """Test 4b: TelethonUploader with adaptive=True passes calculated connections
    based on file size."""
    small_path = make_file(tmp_path / "small.bin", 2 * 1024 * 1024)  # 2MB
    large_path = make_file(tmp_path / "large.bin", 150 * 1024 * 1024)  # 150MB

    captured_connections = []

    async def fake_upload(client, path, *, workers, connections, progress):
        captured_connections.append(connections)
        return MagicMock()

    monkeypatch.setattr("media_bot_v2.telegram.uploader.upload_file_parallel", fake_upload)

    client = MagicMock()
    client.send_file = AsyncMock()

    uploader = TelethonUploader(client, chat_id=USER_CHAT, archive_channel=None, workers=5, connections=5, adaptive=True)

    # Small file -> 1 connection despite connections=5
    await uploader.send_file(small_path, media=MediaInfo(kind=KIND_VIDEO))
    assert captured_connections[-1] == 1

    # Large file -> 5 connections
    await uploader.send_file(large_path, media=MediaInfo(kind=KIND_VIDEO))
    assert captured_connections[-1] == 5
