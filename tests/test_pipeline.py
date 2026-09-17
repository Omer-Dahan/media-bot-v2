"""DownloadPipeline: the credit-after-success decision is the point of this
test file. See media_bot_v2/pipeline.py's module docstring for why charging
happens only after a fully successful upload - this must hold even when the
upload fails partway through a multi-part (split) file."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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


class _FakeUploader:
    def __init__(self, *, fail_on_part: int | None = None):
        self.sent: list[Path] = []
        self.archived: list[Path] = []
        self._fail_on_part = fail_on_part

    async def send_file(self, path: Path, *, caption=None) -> None:
        if self._fail_on_part is not None and len(self.sent) == self._fail_on_part:
            raise RuntimeError("boom: upload failed")
        self.sent.append(path)

    async def forward_to_archive(self, path: Path, *, caption=None) -> None:
        self.archived.append(path)


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
