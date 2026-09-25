"""M10: parallel upload over up to 5 lanes, proven against a simulated client.

Nothing here talks to Telegram. `PartServer` stands in for the MTProto
connection: it accepts `upload.saveFilePart` / `saveBigFilePart` requests,
takes a fixed time per part, records how many were in flight at once, can
answer with FLOOD_WAIT, and keeps the parts so the file Telegram would have
assembled can be compared byte-for-byte with the source.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telethon.errors import FloodPremiumWaitError, FloodWaitError, MediaInvalidError
from telethon.tl import functions, types

from media_bot_v2.config import Settings
from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Base, User
from media_bot_v2.engines.base import BaseEngine, DownloadResult
from media_bot_v2.pipeline import DownloadPipeline
from media_bot_v2.telegram import parallel_upload
from media_bot_v2.telegram.parallel_upload import upload_file_parallel
from media_bot_v2.telegram.progress import UploadProgress
from media_bot_v2.telegram.uploader import TelethonUploader
from media_bot_v2.upload.media_probe import KIND_PHOTO, MediaInfo
from tests.fakes_telegram import FakeTelegramClient

USER_CHAT = 4242
PART = 128 * 1024  # part size Telethon uses for files up to 100MB


class PartServer(FakeTelegramClient):
    def __init__(self, delay: float = 0.0) -> None:
        super().__init__()
        self.delay = delay
        self.parts: dict[int, dict[int, bytes]] = {}
        self.attempts: dict[int, int] = {}
        self.in_flight = 0
        self.max_in_flight = 0
        self.timeline: list[tuple[float, float, int]] = []  # (start, end, part index)
        self.flood_plan: dict[int, int] = {}  # part index -> floods still to raise
        self.flood_premium_plan: dict[int, int] = {}
        self.flood_premium_seconds: int = 5
        self.flood_times: list[float] = []
        self.uploaded_ok: list[int] = []
        self.big_flags: set[bool] = set()

    async def __call__(self, request):
        index = request.file_part
        self.big_flags.add(isinstance(request, functions.upload.SaveBigFilePartRequest))
        self.attempts[index] = self.attempts.get(index, 0) + 1
        start = time.monotonic()
        if self.flood_premium_plan.get(index, 0) > 0:
            self.flood_premium_plan[index] -= 1
            self.flood_times.append(start)
            raise FloodPremiumWaitError(request, capture=self.flood_premium_seconds)
        if self.flood_plan.get(index, 0) > 0:
            self.flood_plan[index] -= 1
            self.flood_times.append(start)
            raise FloodWaitError(request, capture=7)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        self.parts.setdefault(request.file_id, {})[index] = bytes(request.bytes)
        self.timeline.append((start, time.monotonic(), index))
        self.uploaded_ok.append(index)
        return True

    def assembled(self, handle) -> bytes:
        parts = self.parts[handle.id]
        assert sorted(parts) == list(range(handle.parts)), "missing or extra parts"
        return b"".join(parts[i] for i in range(handle.parts))

    async def upload_file(self, path, **kwargs):  # sequential fallback used for empty files
        raise AssertionError("parallel path must not use Telethon's sequential upload_file")


def make_file(path: Path, size: int) -> Path:
    path.write_bytes(os.urandom(size))
    return path


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def sleeps(monkeypatch):
    waited: list[float] = []

    async def fake_sleep(seconds):
        waited.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(parallel_upload, "_sleep", fake_sleep)
    return waited


# ---------------------------------------------------------------- concurrency
@pytest.mark.parametrize(("workers", "expected_max"), [(5, 5), (1, 1), (3, 3), (9, 5)])
async def test_lanes_in_flight_match_the_worker_count(tmp_path, workers, expected_max):
    server = PartServer(delay=0.02)
    path = make_file(tmp_path / "f.bin", 20 * PART)
    await upload_file_parallel(server, path, workers=workers)
    assert server.max_in_flight == expected_max


async def test_five_lanes_are_several_times_faster_than_one(tmp_path):
    path = make_file(tmp_path / "f.bin", 20 * PART)  # 20 parts, 50ms each

    async def timed(workers: int) -> float:
        server = PartServer(delay=0.05)
        begin = time.monotonic()
        await upload_file_parallel(server, path, workers=workers)
        return time.monotonic() - begin

    one, five = await timed(1), await timed(5)
    print(f"\n[M10 measurement] 20 parts x 50ms: 1 lane {one:.3f}s, 5 lanes {five:.3f}s, speedup x{one / five:.2f}")
    assert one >= 1.0
    assert five < 0.45
    assert one / five > 3.5


# ---------------------------------------------------------------- correctness
@pytest.mark.parametrize("size", [3 * 1024 * 1024 + 777, 11 * 1024 * 1024 + 12345, PART, 1])
async def test_assembled_file_is_identical_to_the_source(tmp_path, size):
    path = make_file(tmp_path / "clip.mp4", size)
    server = PartServer(delay=0.001)

    handle = await upload_file_parallel(server, path, workers=5)

    assert sha(server.assembled(handle)) == sha(path.read_bytes())
    assert handle.name == "clip.mp4"
    big = size > 10 * 1024 * 1024
    assert isinstance(handle, types.InputFileBig) == big
    assert server.big_flags == {big}  # small -> saveFilePart, big -> saveBigFilePart
    if not big:
        assert handle.md5_checksum == hashlib.md5(path.read_bytes()).hexdigest()
        assert isinstance(handle, types.InputFile)


async def test_big_file_part_count_matches_telegram_contract(tmp_path):
    path = make_file(tmp_path / "big.bin", 10 * 1024 * 1024 + 1)
    server = PartServer()
    handle = await upload_file_parallel(server, path, workers=5)
    assert handle.parts == -(-path.stat().st_size // PART)


# ---------------------------------------------------------------- FloodWait
async def test_flood_wait_retries_only_the_same_part(tmp_path, sleeps):
    path = make_file(tmp_path / "f.bin", 12 * PART)
    server = PartServer(delay=0.005)
    server.flood_plan = {3: 1}

    handle = await upload_file_parallel(server, path, workers=5)

    assert sleeps == [7]  # waited exactly what the server asked
    assert server.attempts[3] == 2
    assert all(n == 1 for i, n in server.attempts.items() if i != 3)  # nothing else re-sent
    assert sorted(server.uploaded_ok) == list(range(12))  # no part uploaded twice
    assert sha(server.assembled(handle)) == sha(path.read_bytes())


async def test_repeated_floods_shrink_the_lanes_and_the_upload_still_finishes(tmp_path, sleeps, caplog):
    path = make_file(tmp_path / "f.bin", 40 * PART)
    server = PartServer(delay=0.02)
    server.flood_plan = {0: 1, 1: 1, 2: 1, 3: 1}  # four flood events in the first wave

    with caplog.at_level("WARNING"):
        handle = await upload_file_parallel(server, path, workers=5)

    assert "lanes 5 -> 2" in caplog.text
    assert "lanes 2 -> 1" in caplog.text
    assert sha(server.assembled(handle)) == sha(path.read_bytes())
    # once degraded to one lane, whatever starts after the in-flight ones drain never overlaps
    settled = max(server.flood_times) + 3 * server.delay
    late = sorted((s, e) for s, e, _ in server.timeline if s > settled)
    assert len(late) >= 10
    assert all(late[i][1] <= late[i + 1][0] + 0.005 for i in range(len(late) - 1))


async def test_endless_flood_gives_up_and_cancels_the_other_lanes(tmp_path, sleeps):
    path = make_file(tmp_path / "f.bin", 12 * PART)
    server = PartServer(delay=0.02)
    server.flood_plan = {2: 10**6}

    with pytest.raises(FloodWaitError):
        await upload_file_parallel(server, path, workers=5)

    assert server.attempts[2] == parallel_upload.MAX_FLOOD_RETRIES_PER_PART + 1
    await asyncio.sleep(0.05)
    assert server.in_flight == 0  # no lane left running behind the failure


async def test_other_errors_propagate_unchanged(tmp_path):
    class Broken(PartServer):
        async def __call__(self, request):
            raise ConnectionError("socket died")

    path = make_file(tmp_path / "f.bin", 4 * PART)
    with pytest.raises(ConnectionError):
        await upload_file_parallel(Broken(), path, workers=5)


# ---------------------------------------------------------------- progress
async def test_raw_progress_is_monotonic_capped_and_survives_retries(tmp_path, sleeps):
    path = make_file(tmp_path / "f.bin", 30 * PART + 5)
    server = PartServer(delay=0.003)
    server.flood_plan = {4: 2, 9: 1}
    seen: list[int] = []

    async def on_progress(done, total):
        seen.append(done)
        assert total == path.stat().st_size

    await upload_file_parallel(server, path, workers=5, progress=on_progress)

    assert seen == sorted(seen)
    assert seen[-1] == path.stat().st_size
    assert max(seen) <= path.stat().st_size
    assert len(seen) == 31  # one report per accepted part, none per retry


class _Reporter:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def update(self, text: str, **kwargs) -> None:
        self.texts.append(text)


async def test_user_progress_does_not_flood_the_message_and_reaches_100(tmp_path, sleeps):
    path = make_file(tmp_path / "f.bin", 200 * PART)
    server = PartServer(delay=0.0005)
    server.flood_plan = {50: 1, 120: 1}
    reporter = _Reporter()
    clock = iter(x * 3.5 for x in range(10_000))  # every report is "3.5s later"
    progress = UploadProgress(reporter, "uploading", path.stat().st_size, clock=lambda: next(clock))

    await upload_file_parallel(server, path, workers=5, progress=progress)

    percents = [int(t.split()[-1].rstrip("%")) for t in reporter.texts]
    assert percents == sorted(percents) and len(set(percents)) == len(percents)
    assert percents[-1] == 100 and max(percents) <= 100
    assert len(percents) <= 21  # 100/5 steps + the final one, out of 200 reports


async def test_user_progress_is_time_throttled():
    reporter = _Reporter()
    now = [0.0]
    progress = UploadProgress(reporter, "up", 1000, clock=lambda: now[0])
    for done in range(0, 1000, 10):  # 1% steps, but the clock never moves
        await progress(done + 10, 1000) if done + 10 < 1000 else None
    assert len(reporter.texts) == 1  # only the first edit until time passes
    now[0] = 10.0
    await progress(600, 1000)
    assert reporter.texts[-1] == "up 60%"
    await progress(500, 1000)  # a late, smaller report never moves it back
    assert reporter.texts[-1] == "up 60%"


async def test_progress_sums_over_split_parts():
    reporter = _Reporter()
    progress = UploadProgress(reporter, "up", 400, min_step=1, min_interval=0)
    progress.start_part(0)
    await progress(100, 100)
    progress.start_part(100)
    await progress(50, 100)
    assert reporter.texts == ["up 25%", "up 37%"]  # continues, does not restart at 0


# ---------------------------------------------------------------- config
def _settings(monkeypatch, value):
    monkeypatch.setenv("APP_ID", "1")
    monkeypatch.setenv("APP_HASH", "h")
    monkeypatch.setenv("BOT_TOKEN", "t")
    if value is None:
        monkeypatch.delenv("UPLOAD_WORKERS", raising=False)
    else:
        monkeypatch.setenv("UPLOAD_WORKERS", value)
    return Settings(_env_file=None)


def test_upload_workers_config(monkeypatch):
    assert _settings(monkeypatch, None).upload_workers == 5
    assert _settings(monkeypatch, "3").upload_workers == 3
    assert _settings(monkeypatch, "1").upload_workers == 1
    assert _settings(monkeypatch, "9").upload_workers == 5  # clamped
    for bad in ("0", "-2"):
        with pytest.raises(ValidationError, match="UPLOAD_WORKERS"):
            _settings(monkeypatch, bad)


# ---------------------------------------------------------------- uploader wiring
async def test_uploader_with_one_worker_still_sends_the_plain_path(tmp_path):
    server = PartServer()
    path = make_file(tmp_path / "a.bin", 3 * PART)
    await TelethonUploader(server, chat_id=USER_CHAT, archive_channel=None).send_file(path)
    assert server.send_files()[0].args[1] == str(path)
    assert server.parts == {}


async def test_uploader_with_workers_sends_the_parallel_handle(tmp_path):
    server = PartServer()
    path = make_file(tmp_path / "a.bin", 12 * 1024 * 1024)
    uploader = TelethonUploader(server, chat_id=USER_CHAT, archive_channel=None, workers=5)

    await uploader.send_file(path, caption="c")

    handle = server.send_files()[0].args[1]
    assert isinstance(handle, types.InputFileBig)
    assert sha(server.assembled(handle)) == sha(path.read_bytes())


async def test_photo_keeps_the_plain_path(tmp_path):
    server = PartServer()
    path = make_file(tmp_path / "p.jpg", 3 * PART)
    uploader = TelethonUploader(server, chat_id=USER_CHAT, archive_channel=None, workers=5)
    await uploader.send_file(path, media=MediaInfo(kind=KIND_PHOTO))
    assert server.send_files()[0].args[1] == str(path)


async def test_rejected_video_retry_as_document_reuses_the_upload(tmp_path):
    server = PartServer()
    server.fail_send_file = {1: MediaInvalidError(request=None)}
    path = make_file(tmp_path / "v.mp4", 6 * PART)
    uploader = TelethonUploader(server, chat_id=USER_CHAT, archive_channel=None, workers=5)

    await uploader.send_file(path)

    first, second = server.send_files()
    assert first.args[1] is second.args[1]  # same handle, not uploaded a second time
    assert second.kwargs["force_document"] is True
    assert sum(server.attempts.values()) == 6


# ---------------------------------------------------------------- end to end
class _BlobEngine(BaseEngine):
    def __init__(self, sizes):
        self.sizes = sizes

    def matches(self, url):
        return True

    async def download(self, url, *, dest_dir, cancel_token=None):
        dest_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, size in enumerate(self.sizes):
            paths.append(str(make_file(dest_dir / f"blob{i}.bin", size)))
        return DownloadResult(file_paths=paths, title="t")


async def test_pipeline_end_to_end_progress_charging_and_cleanup(tmp_path, sleeps):
    engine_db = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine_db)
    factory = sessionmaker(bind=engine_db)
    with factory() as session:
        session.add(User(user_id=USER_CHAT, free=50, paid=0, bandwidth_used=0, total_bandwidth=0, is_blocked=0))
        session.commit()
    credits = CreditsService(factory, enable_vip=True, owner_ids=[], free_bandwidth=10**12)

    server = PartServer(delay=0.002)
    server.flood_plan = {5: 1}
    reporter = _Reporter()
    uploader = TelethonUploader(server, chat_id=USER_CHAT, archive_channel=None, workers=5)
    total = 40 * PART
    pipeline = DownloadPipeline(credits_service=credits, download_dir=tmp_path / "dl")

    await pipeline.run(
        user_id=USER_CHAT, url="https://x/y", engine=_BlobEngine([total]), uploader=uploader,
        progress=reporter, cache=None, cache_key=None, archive_channel=None,
    )  # fmt: skip

    handle = server.send_files()[0].args[1]
    assert handle.parts == 40
    assert len(server.assembled(handle)) == total
    percent_texts = [t for t in reporter.texts if t.endswith("%")]
    percents = [int(t.split()[-1].rstrip("%")) for t in percent_texts]
    assert percents == sorted(percents) and percents[-1] == 100
    assert not list((tmp_path / "dl").rglob("*.bin"))  # scratch files removed
    with factory() as session:
        assert session.query(User).one().bandwidth_used == total  # charged once, on what was delivered
