"""Parallel file upload over several "lanes" (concurrent `upload.save*FilePart` calls).

Telethon 1.45's `upload_file` has no worker/concurrency parameter: it reads
and sends the parts strictly one after another (see
`telethon/client/uploads.py`, the `for part_index in range(part_count)` loop).
Telegram limits bandwidth per request stream, so sending several parts at the
same time is much faster for big files. This module does the same job as
`upload_file` but with up to `MAX_WORKERS` parts in flight, and returns the
same kind of handle (`InputFileBig` above 10MB, `InputSizedFile` with an MD5
below) so it plugs straight into `client.send_file`.

Failure handling:

* `FloodWaitError` on a part: sleep what the server asked and retry *that
  part only*; every part already uploaded stays uploaded.
* Repeated flood waits shrink the number of lanes (5 -> 2 -> 1) instead of
  failing, and the upload carries on.
* Any other error cancels the remaining lanes and propagates, so the caller
  fails exactly as it does with the sequential upload.

Progress is counted only when a part is *accepted*, so retries never move it
backwards and it can never pass the file size.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from telethon import custom, helpers, utils
from telethon.errors import FloodWaitError
from telethon.tl import functions, types

logger = logging.getLogger(__name__)

MAX_WORKERS = 5
BIG_FILE_THRESHOLD = 10 * 1024 * 1024  # Telegram: above this, saveBigFilePart is required
MAX_FLOOD_RETRIES_PER_PART = 6
# Flood events seen -> lanes allowed from then on (never above what was asked for).
_DEGRADE_AFTER = ((4, 1), (2, 2))

ProgressCallback = Callable[[int, int], Awaitable[None] | None]


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class _State:
    def __init__(self, workers: int, total: int) -> None:
        self.limit = workers
        self.total = total
        self.uploaded = 0
        self.floods = 0

    def note_flood(self) -> None:
        self.floods += 1
        for threshold, lanes in _DEGRADE_AFTER:
            if self.floods >= threshold and lanes < self.limit:
                logger.warning(
                    "Upload hit %d flood waits, reducing parallel lanes %d -> %d", self.floods, self.limit, lanes
                )
                self.limit = lanes
                break


def _read_part(fd: int, offset: int, size: int) -> bytes:
    return os.pread(fd, size, offset)


async def upload_file_parallel(
    client,
    path: Path,
    *,
    workers: int = MAX_WORKERS,
    progress: ProgressCallback | None = None,
    file_name: str | None = None,
) -> types.InputFile | types.InputFileBig:
    workers = max(1, min(MAX_WORKERS, workers))
    file_size = path.stat().st_size
    if file_size == 0:
        return await client.upload_file(str(path))

    part_size = utils.get_appropriated_part_size(file_size) * 1024
    part_count = (file_size + part_size - 1) // part_size
    is_big = file_size > BIG_FILE_THRESHOLD
    file_id = helpers.generate_random_long()
    name = file_name or path.name

    # Small files need an MD5 over the whole content; computing it up front
    # keeps parts independent of each other (and is cheap below 10MB).
    md5 = None if is_big else await asyncio.to_thread(_md5_of, path)

    state = _State(workers, file_size)
    pending: asyncio.Queue[int] = asyncio.Queue()
    for index in range(part_count):
        pending.put_nowait(index)

    fd = os.open(path, os.O_RDONLY)
    try:
        lanes = [
            asyncio.create_task(
                _lane(client, lane, fd, file_id, part_count, part_size, is_big, pending, state, progress)
            )
            for lane in range(workers)
        ]
        try:
            done, _ = await asyncio.wait(lanes, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                if task.exception() is not None:
                    raise task.exception()
        finally:
            for task in lanes:
                task.cancel()
            await asyncio.gather(*lanes, return_exceptions=True)
    finally:
        os.close(fd)

    if is_big:
        return types.InputFileBig(file_id, part_count, name)
    return custom.InputSizedFile(file_id, part_count, name, md5=md5, size=file_size)


def _md5_of(path: Path) -> hashlib._Hash:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest


async def _lane(client, lane, fd, file_id, part_count, part_size, is_big, pending, state, progress) -> None:
    while lane < state.limit:
        try:
            index = pending.get_nowait()
        except asyncio.QueueEmpty:
            return
        data = await asyncio.to_thread(_read_part, fd, index * part_size, part_size)
        if is_big:
            request = functions.upload.SaveBigFilePartRequest(file_id, index, part_count, data)
        else:
            request = functions.upload.SaveFilePartRequest(file_id, index, data)
        await _send_part(client, request, index, state)
        state.uploaded += len(data)
        if progress is not None:
            result = progress(state.uploaded, state.total)
            if asyncio.iscoroutine(result):
                await result


async def _send_part(client, request, index: int, state: _State) -> None:
    attempts = 0
    while True:
        try:
            ok = await client(request)
        except FloodWaitError as exc:
            attempts += 1
            state.note_flood()
            if attempts > MAX_FLOOD_RETRIES_PER_PART:
                raise
            logger.warning("Flood wait %ss on upload part %d (attempt %d)", exc.seconds, index, attempts)
            await _sleep(exc.seconds)
            continue
        if not ok:
            raise RuntimeError(f"Failed to upload file part {index}.")
        return
