"""End-to-end download pipeline: check quota -> download -> split -> upload
-> archive forward -> charge credits -> delete local files.

Credit-charging decision (fixes spec/INVENTORY.md section 2's flagged bug):
credits are deducted only after every part of a download has been
successfully uploaded. The old bot charged credits *before* the ffmpeg
split+upload for large videos (src/engine/base.py:976), so a send that
failed after a successful split still cost the user credits, with no refund
path anywhere in that codebase.

Charge-after-success was chosen over charge-then-refund-on-failure because
it needs exactly one accounting write, gated on total success - there is
never a window where a user has been charged and no file was delivered. A
refund-on-failure design needs a second write (the refund) to itself
succeed for the accounting to stay correct, which is one more failure mode
for no real benefit here.

Every failure path (download error, split error, upload error, or the
pipeline being interrupted) is caught by the single try/except/finally
below: no credits are deducted, the user sees one error message (via the
same progress message used for status updates), and every file this run
wrote to disk - including split parts and the original pre-split download -
is deleted.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from media_bot_v2.credits.service import CreditsService
from media_bot_v2.engines.base import BaseEngine, DownloadResult
from media_bot_v2.telegram import texts
from media_bot_v2.upload import splitter

logger = logging.getLogger(__name__)


class ProgressReporter(Protocol):
    async def update(self, text: str) -> None: ...


class Uploader(Protocol):
    async def send_file(self, path: Path, *, caption: str | None = None) -> None: ...
    async def forward_to_archive(self, path: Path, *, caption: str | None = None) -> None: ...


class DownloadPipeline:
    def __init__(self, *, credits_service: CreditsService, download_dir: Path) -> None:
        self._credits = credits_service
        self._download_dir = download_dir

    async def run(
        self,
        *,
        user_id: int,
        url: str,
        engine: BaseEngine,
        uploader: Uploader,
        progress: ProgressReporter,
    ) -> None:
        self._credits.check_quota(user_id)

        user_dir = self._download_dir / str(user_id)
        result: DownloadResult | None = None
        parts: list[Path] = []
        try:
            await progress.update(texts.DOWNLOADING)
            result = await engine.download(url, dest_dir=user_dir)

            await progress.update(texts.PROCESSING)
            for raw_path in result.file_paths:
                parts.extend(splitter.split_file(Path(raw_path)))

            await progress.update(texts.UPLOADING)
            total_size = 0
            for index, part in enumerate(parts, start=1):
                caption = result.title if len(parts) == 1 else f"{result.title} ({index}/{len(parts)})"
                await uploader.send_file(part, caption=caption)
                await uploader.forward_to_archive(part, caption=caption)
                total_size += part.stat().st_size

            self._credits.use_quota_dynamic(user_id, total_size)
            self._credits.add_bandwidth_used(user_id, total_size)
            await progress.update(texts.DOWNLOAD_DONE)
        except Exception:
            logger.exception("Download pipeline failed for user=%s url=%s", user_id, url)
            await progress.update(texts.DOWNLOAD_FAILED)
            raise
        finally:
            self._cleanup(result, parts, user_dir)

    def _cleanup(self, result: DownloadResult | None, parts: list[Path], user_dir: Path) -> None:
        paths_to_remove: set[Path] = set(parts)
        if result:
            paths_to_remove.update(Path(p) for p in result.file_paths)

        for path in paths_to_remove:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                logger.warning("Failed to delete leftover file %s", path)

        try:
            if user_dir.exists() and not any(user_dir.iterdir()):
                user_dir.rmdir()
        except OSError:
            pass
