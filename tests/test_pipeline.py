"""DownloadPipeline: the credit-after-success decision is the point of this
test file. See media_bot_v2/pipeline.py's module docstring for why charging
happens only after a fully successful upload - this must hold even when the
upload fails partway through a multi-part (split) file."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from media_bot_v2.cache.video_cache import VideoCacheStore, compute_cache_key
from media_bot_v2.credits.exceptions import CreditsExhaustedException
from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Base, User
from media_bot_v2.engines.base import BaseEngine, DownloadResult
from media_bot_v2.pipeline import DownloadPipeline


class _FakeEngine(BaseEngine):
    def __init__(self, *, content: bytes = b"abc", filename: str = "file.bin"):
        self._content = content
        self._filename = filename

    def matches(self, url: str) -> bool:
        return True

    async def download(self, url: str, *, dest_dir: Path) -> DownloadResult:
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / self._filename
        path.write_bytes(self._content)
        return DownloadResult(file_paths=[str(path)], title=self._filename)


class _FailingEngine(BaseEngine):
    def matches(self, url: str) -> bool:
        return True

    async def download(self, url: str, *, dest_dir: Path):
        raise RuntimeError("boom: download failed")


class _PartialWriteFailingEngine(BaseEngine):
    """Simulates a download that dies mid-write, leaving a partial file."""

    def matches(self, url: str) -> bool:
        return True

    async def download(self, url: str, *, dest_dir: Path):
        dest_dir.mkdir(parents=True, exist_ok=True)
        partial_path = dest_dir / "partial.bin"
        partial_path.write_bytes(b"only-some-of-the-bytes")
        raise RuntimeError("boom: connection dropped mid-download")


class _FakeMessage:
    def __init__(self, id: int, label: str):
        self.id = id
        self.label = label

    def __repr__(self):
        return self.label


class _FakeUploader:
    def __init__(
        self,
        *,
        fail_on_part: int | None = None,
        fail_archive: bool = False,
        fail_cached_send: bool = False,
    ):
        self.sent: list[Path] = []
        self.archived: list[object] = []
        self.cached_sends: list[tuple[str, list[int]]] = []
        self._fail_on_part = fail_on_part
        self._fail_archive = fail_archive
        self._fail_cached_send = fail_cached_send
        self._next_archive_id = 1000

    async def send_file(self, path: Path, *, caption=None, **kwargs):
        if self._fail_on_part is not None and len(self.sent) == self._fail_on_part:
            raise RuntimeError("boom: upload failed")
        self.sent.append(path)
        return _FakeMessage(len(self.sent), f"message-for-{path.name}")

    async def copy_to_archive(self, message, **kwargs):
        if self._fail_archive:
            raise RuntimeError("boom: archive channel unreachable")
        self.archived.append(message)
        self._next_archive_id += 1
        return _FakeMessage(self._next_archive_id, f"archived-{message}")

    async def send_cached(self, archive_chat: str, message_ids: list[int], **kwargs):
        if self._fail_cached_send:
            raise RuntimeError("boom: cached message no longer exists")
        self.cached_sends.append((archive_chat, message_ids))
        return _FakeMessage(9999, "resent-from-cache")


class _FakeProgress:
    def __init__(self):
        self.updates: list[str] = []

    async def update(self, text: str) -> None:
        self.updates.append(text)


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


@pytest.fixture
def cache_store(session_factory):
    return VideoCacheStore(session_factory)


async def test_successful_download_charges_credits_and_deletes_local_file(
    session_factory, credits_service, tmp_path
):
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    uploader = _FakeUploader()
    progress = _FakeProgress()

    await pipeline.run(
        user_id=1, url="http://x", engine=_FakeEngine(), uploader=uploader, progress=progress
    )

    assert len(uploader.sent) == 1
    assert len(uploader.archived) == 1
    assert not any(tmp_path.rglob("*.bin"))  # deleted from disk after upload

    with session_factory() as session:
        user = session.query(User).filter(User.user_id == 1).one()
        assert user.free == 2  # exactly one credit deducted

    assert progress.updates[-1] == "הושלם ✅"


async def test_failed_download_charges_nothing_and_leaves_no_files(
    session_factory, credits_service, tmp_path
):
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    uploader = _FakeUploader()
    progress = _FakeProgress()

    with pytest.raises(RuntimeError):
        await pipeline.run(
            user_id=1, url="http://x", engine=_FailingEngine(), uploader=uploader, progress=progress
        )

    with session_factory() as session:
        user = session.query(User).filter(User.user_id == 1).one()
        assert user.free == 3  # untouched - no charge on failure

    assert progress.updates[-1] == "❌ ההורדה נכשלה. נסה שוב או שלח קישור אחר."


async def test_upload_failure_charges_nothing_and_cleans_up_partial_files(
    session_factory, credits_service, tmp_path
):
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    uploader = _FakeUploader(fail_on_part=0)  # fail sending the very first (only) part
    progress = _FakeProgress()

    with pytest.raises(RuntimeError):
        await pipeline.run(
            user_id=1, url="http://x", engine=_FakeEngine(), uploader=uploader, progress=progress
        )

    with session_factory() as session:
        user = session.query(User).filter(User.user_id == 1).one()
        assert user.free == 3  # untouched - upload failed, no file was delivered

    assert not any(tmp_path.rglob("*.bin"))  # partial download cleaned up


async def test_quota_exhausted_never_calls_engine_and_charges_nothing(session_factory, tmp_path):
    with session_factory() as session:
        user = session.query(User).filter(User.user_id == 1).one()
        user.free = 0
        user.paid = 0
        session.commit()

    credits_service = CreditsService(session_factory, enable_vip=True, owner_ids=[], free_bandwidth=2_000_000_000)
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)

    class _EngineThatMustNotBeCalled(BaseEngine):
        def matches(self, url: str) -> bool:
            return True

        async def download(self, url: str, *, dest_dir: Path):
            raise AssertionError("engine.download must not be called when quota is exhausted")

    with pytest.raises(CreditsExhaustedException):
        await pipeline.run(
            user_id=1,
            url="http://x",
            engine=_EngineThatMustNotBeCalled(),
            uploader=_FakeUploader(),
            progress=_FakeProgress(),
        )


async def test_download_failure_mid_write_leaves_no_partial_file_on_disk(
    session_factory, credits_service, tmp_path
):
    """Covers finding 2: a download that dies mid-write must not leak a
    partial file, even though `result` is never assigned in that case."""
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    uploader = _FakeUploader()
    progress = _FakeProgress()

    with pytest.raises(RuntimeError):
        await pipeline.run(
            user_id=1,
            url="http://x",
            engine=_PartialWriteFailingEngine(),
            uploader=uploader,
            progress=progress,
        )

    with session_factory() as session:
        user = session.query(User).filter(User.user_id == 1).one()
        assert user.free == 3  # untouched - no charge on failure

    user_dir = tmp_path / "1"
    assert not user_dir.exists() or not any(user_dir.iterdir())


async def test_archive_forward_failure_does_not_fail_download_or_undo_charge(
    session_factory, credits_service, tmp_path
):
    """Covers finding 4: the user already received the file and the bytes
    already count toward their charge - an archive-channel hiccup must not
    turn a successful delivery into a reported failure or a missed charge."""
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    uploader = _FakeUploader(fail_archive=True)
    progress = _FakeProgress()

    await pipeline.run(
        user_id=1, url="http://x", engine=_FakeEngine(), uploader=uploader, progress=progress
    )

    assert len(uploader.sent) == 1
    assert uploader.archived == []  # archive forward failed, nothing recorded

    with session_factory() as session:
        user = session.query(User).filter(User.user_id == 1).one()
        assert user.free == 2  # still charged - the user got the file

    assert progress.updates[-1] == "הושלם ✅"


async def test_successful_download_writes_a_cache_entry_when_archive_channel_configured(
    session_factory, credits_service, cache_store, tmp_path
):
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    uploader = _FakeUploader()
    key = compute_cache_key("video123", "720")

    await pipeline.run(
        user_id=1,
        url="http://x",
        engine=_FakeEngine(filename="clip.mp4"),
        uploader=uploader,
        progress=_FakeProgress(),
        cache=cache_store,
        cache_key=key,
        archive_channel="@archive",
    )

    entry = cache_store.get(key)
    assert entry is not None
    assert entry.archive_chat == "@archive"
    assert entry.title == "clip.mp4"


async def test_no_cache_entry_written_without_an_archive_channel(
    session_factory, credits_service, cache_store, tmp_path
):
    """Caching leans entirely on the archive-channel forward for a stable
    resend source - without one configured there is nothing to cache from."""
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    uploader = _FakeUploader()
    key = compute_cache_key("video123", "720")

    await pipeline.run(
        user_id=1,
        url="http://x",
        engine=_FakeEngine(),
        uploader=uploader,
        progress=_FakeProgress(),
        cache=cache_store,
        cache_key=key,
        archive_channel=None,
    )

    assert cache_store.get(key) is None


async def test_no_cache_entry_written_when_archive_forward_fails(
    session_factory, credits_service, cache_store, tmp_path
):
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    uploader = _FakeUploader(fail_archive=True)
    key = compute_cache_key("video123", "720")

    await pipeline.run(
        user_id=1,
        url="http://x",
        engine=_FakeEngine(),
        uploader=uploader,
        progress=_FakeProgress(),
        cache=cache_store,
        cache_key=key,
        archive_channel="@archive",
    )

    assert cache_store.get(key) is None


async def test_cache_hit_resends_without_calling_the_engine_and_charges_no_credits(
    session_factory, credits_service, cache_store, tmp_path
):
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    uploader = _FakeUploader()
    key = compute_cache_key("video123", "720")
    cache_store.put(key, archive_chat="@archive", message_ids=[42, 43], title="Cached Clip")

    class _EngineThatMustNotBeCalled(BaseEngine):
        def matches(self, url: str) -> bool:
            return True

        async def download(self, url: str, *, dest_dir: Path):
            raise AssertionError("engine.download must not run on a cache hit")

    progress = _FakeProgress()
    await pipeline.run(
        user_id=1,
        url="http://x",
        engine=_EngineThatMustNotBeCalled(),
        uploader=uploader,
        progress=progress,
        cache=cache_store,
        cache_key=key,
        archive_channel="@archive",
    )

    assert uploader.cached_sends == [("@archive", [42, 43])]
    assert uploader.sent == []  # nothing was freshly uploaded

    with session_factory() as session:
        user = session.query(User).filter(User.user_id == 1).one()
        assert user.free == 3  # cache hits are free - no credit deducted

    assert progress.updates[-1] == "הושלם ✅"


async def test_cache_hit_that_fails_to_resend_deletes_the_stale_entry_and_downloads_fresh(
    session_factory, credits_service, cache_store, tmp_path
):
    """A cache entry can go stale (the archived message was deleted, the
    channel access changed) - that must not fail the whole request, it
    should fall back to a normal download and self-heal the cache."""
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    uploader = _FakeUploader(fail_cached_send=True)
    key = compute_cache_key("video123", "720")
    cache_store.put(key, archive_chat="@archive", message_ids=[42], title="Stale Clip")

    await pipeline.run(
        user_id=1,
        url="http://x",
        engine=_FakeEngine(),
        uploader=uploader,
        progress=_FakeProgress(),
        cache=cache_store,
        cache_key=key,
        archive_channel="@archive",
    )

    assert len(uploader.sent) == 1  # fell back to a real download
    with session_factory() as session:
        user = session.query(User).filter(User.user_id == 1).one()
        assert user.free == 2  # charged normally for the fresh download

    # the stale entry was replaced by a fresh one from this successful run
    entry = cache_store.get(key)
    assert entry is not None
    assert entry.message_ids != [42]
