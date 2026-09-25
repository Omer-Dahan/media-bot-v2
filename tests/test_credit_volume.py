"""Volume-based credit model (M4.3): 1 credit per MB_PER_CREDIT MB delivered in
a request, summed over all delivered parts, rounded up, minimum 1."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telethon import TelegramClient, events
from telethon.sessions import MemorySession

from media_bot_v2.config import Settings
from media_bot_v2.credits.service import CreditsService, credits_for_sizes
from media_bot_v2.db.models import Base, User
from media_bot_v2.engines.base import BaseEngine, DownloadResult
from media_bot_v2.pipeline import DownloadPipeline
from media_bot_v2.telegram import texts
from media_bot_v2.telegram.router import register_handlers

MB = 1024 * 1024


@pytest.mark.parametrize(
    ("sizes_mb", "expected"),
    [
        ([1], 1),
        ([200], 1),
        ([200.1], 2),
        ([400], 2),
        ([1024], 6),  # 1GB
        ([5 * 1024], 26),  # 5GB
        ([100, 100, 100], 2),  # three parts, 300MB total
        ([0.001, 0.001, 0.001], 1),  # small file in 3 parts is still 1
        ([29.8], 1),  # real TikTok example from the old bot's log
        ([0], 1),  # a delivered (empty) file still costs the minimum
    ],
)
def test_credits_for_sizes_boundaries(sizes_mb, expected):
    assert credits_for_sizes([int(mb * MB) for mb in sizes_mb]) == expected


def test_credits_for_sizes_nothing_delivered_costs_nothing():
    assert credits_for_sizes([]) == 0


def test_credits_for_sizes_respects_configured_mb_per_credit():
    assert credits_for_sizes([200 * MB], mb_per_credit=100) == 2
    assert credits_for_sizes([5 * 1024 * MB], mb_per_credit=100) == 52


def test_credits_for_sizes_rejects_non_positive_mb_per_credit():
    with pytest.raises(ValueError):
        credits_for_sizes([MB], mb_per_credit=0)


def test_settings_mb_per_credit_defaults_to_200_and_reads_env(monkeypatch):
    base = {"APP_ID": "1", "APP_HASH": "h", "BOT_TOKEN": "t"}
    for key, value in base.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MB_PER_CREDIT", raising=False)
    assert Settings(_env_file=None).mb_per_credit == 200
    monkeypatch.setenv("MB_PER_CREDIT", "100")
    assert Settings(_env_file=None).mb_per_credit == 100
    monkeypatch.setenv("MB_PER_CREDIT", "0")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(User(user_id=1, free=10, paid=0, bandwidth_used=0, total_bandwidth=0, is_blocked=0))
        session.commit()
    return factory


def _credits(session_factory, **kwargs) -> CreditsService:
    return CreditsService(session_factory, enable_vip=True, owner_ids=[], free_bandwidth=10**12, **kwargs)


def _free(session_factory) -> int:
    with session_factory() as session:
        return session.query(User).filter(User.user_id == 1).one().free


def test_service_deducts_by_volume_and_uses_configured_size(session_factory):
    service = _credits(session_factory, mb_per_credit=100)
    assert service.use_quota_dynamic(1, [100 * MB, 100 * MB, 100 * MB]) == 7  # 300MB / 100 = 3
    assert _free(session_factory) == 7


class _SparseEngine(BaseEngine):
    """Returns files of the given sizes (sparse on disk, so cheap to create)."""

    def __init__(self, sizes: list[int]) -> None:
        self._sizes = sizes

    def matches(self, url: str) -> bool:
        return True

    async def download(self, url: str, *, dest_dir: Path) -> DownloadResult:
        dest_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, size in enumerate(self._sizes):
            path = dest_dir / f"part{i}.bin"
            with path.open("wb") as fh:
                fh.truncate(size)
            paths.append(str(path))
        return DownloadResult(file_paths=paths, title="T")


class _Uploader:
    def __init__(self, fail_on_call: int | None = None) -> None:
        self.calls = 0
        self._fail_on_call = fail_on_call

    async def send_file(self, path: Path, *, caption=None, **kwargs):
        self.calls += 1
        if self._fail_on_call == self.calls:
            raise RuntimeError("upload dropped")
        return object()

    async def copy_to_archive(self, message, **kwargs):
        return None

    async def send_cached(self, archive_chat, message_ids, **kwargs):
        return None


class _Progress:
    async def update(self, text: str) -> None:
        pass


async def _run(session_factory, tmp_path, sizes, uploader=None, **service_kwargs):
    pipeline = DownloadPipeline(credits_service=_credits(session_factory, **service_kwargs), download_dir=tmp_path)
    await pipeline.run(
        user_id=1,
        url="http://x",
        engine=_SparseEngine(sizes),
        uploader=uploader or _Uploader(),
        progress=_Progress(),
    )


async def test_pipeline_three_100mb_parts_charge_two_credits(session_factory, tmp_path):
    await _run(session_factory, tmp_path, [100 * MB] * 3)
    assert _free(session_factory) == 8  # per-part model would have charged 3


async def test_pipeline_small_file_in_three_parts_charges_one_credit(session_factory, tmp_path):
    await _run(session_factory, tmp_path, [1000, 1000, 1000])
    assert _free(session_factory) == 9


async def test_pipeline_single_small_file_charges_one_credit(session_factory, tmp_path):
    await _run(session_factory, tmp_path, [1024])
    assert _free(session_factory) == 9


async def test_pipeline_exactly_200mb_is_one_credit_and_just_over_is_two(session_factory, tmp_path):
    await _run(session_factory, tmp_path, [200 * MB])
    assert _free(session_factory) == 9
    await _run(session_factory, tmp_path, [200 * MB + 1])
    assert _free(session_factory) == 7


async def test_pipeline_partial_delivery_charges_only_what_was_delivered(session_factory, tmp_path):
    # Parts 1-2 (250MB + 250MB = 500MB -> 3 credits) delivered, part 3 fails.
    # Charging all three parts (750MB) would have cost 4.
    with pytest.raises(RuntimeError):
        await _run(session_factory, tmp_path, [250 * MB] * 3, uploader=_Uploader(fail_on_call=3))
    assert _free(session_factory) == 7
    with session_factory() as session:
        assert session.query(User).one().bandwidth_used == 500 * MB


async def test_pipeline_first_part_failing_charges_nothing(session_factory, tmp_path):
    with pytest.raises(RuntimeError):
        await _run(session_factory, tmp_path, [250 * MB] * 3, uploader=_Uploader(fail_on_call=1))
    assert _free(session_factory) == 10


async def test_pipeline_honours_configured_mb_per_credit(session_factory, tmp_path):
    await _run(session_factory, tmp_path, [200 * MB], mb_per_credit=100)
    assert _free(session_factory) == 8


# --- /settings shows the live balance -------------------------------------


class _Sender:
    first_name = "T"
    username = "t"


class _Event:
    def __init__(self, sender_id: int) -> None:
        self.sender_id = sender_id
        self.chat_id = sender_id
        self.sender = _Sender()
        self.responses: list[str] = []
        self.edits: list[str] = []

    async def respond(self, text, *args, **kwargs):
        self.responses.append(text)

    async def edit(self, text=None, *args, **kwargs):
        self.edits.append(text)

    async def answer(self, *args, **kwargs):
        pass


def _settings_router(session_factory, *, enable_vip=True, owner_ids=()):
    client = TelegramClient(MemorySession(), 1, "hash")
    credits = CreditsService(
        session_factory, enable_vip=enable_vip, owner_ids=list(owner_ids), free_bandwidth=10**12
    )
    register_handlers(
        client,
        session_factory=session_factory,
        credits_service=credits,
        free_download=3,
        pipeline=DownloadPipeline(credits_service=credits, download_dir=Path("/tmp/media-bot-v2-test")),
        archive_channel=None,
        max_download_size=4 * 1024 * 1024 * 1024,
    )
    return client


def _handler(client, matcher):
    for callback, ev in client.list_event_handlers():
        if matcher(ev):
            return callback
    raise AssertionError("handler not registered")


def _settings_handler(client):
    return _handler(
        client, lambda ev: isinstance(ev, events.NewMessage) and ev.pattern and ev.pattern("/settings")
    )


def _toggle_handler(client):
    return _handler(
        client,
        lambda ev: isinstance(ev, events.CallbackQuery) and getattr(ev, "match", None) and ev.match(b"toggle_quality"),
    )


async def test_settings_screen_shows_remaining_credits(session_factory):
    client = _settings_router(session_factory)
    event_ = _Event(1)
    await _settings_handler(client)(event_)
    assert texts.SETTINGS_CREDITS.format(credits=10) in event_.responses[0]


async def test_settings_screen_balance_updates_after_a_charged_download(session_factory, tmp_path):
    client = _settings_router(session_factory)
    handler = _settings_handler(client)
    before = _Event(1)
    await handler(before)
    await _run(session_factory, tmp_path, [400 * MB])  # 2 credits
    after = _Event(1)
    await handler(after)
    assert texts.SETTINGS_CREDITS.format(credits=10) in before.responses[0]
    assert texts.SETTINGS_CREDITS.format(credits=8) in after.responses[0]


async def test_toggle_refresh_also_shows_the_live_balance(session_factory, tmp_path):
    client = _settings_router(session_factory)
    await _run(session_factory, tmp_path, [1024])
    event_ = _Event(1)
    event_.data = b"toggle_quality"
    await _toggle_handler(client)(event_)
    assert texts.SETTINGS_CREDITS.format(credits=9) in event_.edits[0]


async def test_settings_screen_hides_balance_when_vip_disabled_or_owner(session_factory):
    for kwargs in ({"enable_vip": False}, {"owner_ids": [1]}):
        client = _settings_router(session_factory, **kwargs)
        event_ = _Event(1)
        await _settings_handler(client)(event_)
        assert event_.responses[0] == texts.SETTINGS
