"""Parallel file upload over several "lanes" (concurrent `upload.save*FilePart` calls).

Telethon 1.45's `upload_file` has no worker/concurrency parameter: it reads
and sends the parts strictly one after another (see
`telethon/client/uploads.py`, the `for part_index in range(part_count)` loop).
Telegram limits bandwidth per request stream, so sending several parts at the
same time is much faster for big files. This module does the same job as
`upload_file` but with up to `MAX_WORKERS` parts in flight, and returns the
same kind of handle (`InputFileBig` above 10MB, `InputSizedFile` with an MD5
below) so it plugs straight into `client.send_file`.

Connections: with `connections > 1` the lanes are spread over several real TCP
connections to the *same* DC, because Telegram's bandwidth cap is per
connection. `client._borrow_exported_sender` cannot provide them: it caches
one sender per DC and its `auth.exportAuthorization` is refused for the DC the
account is already on (Telethon's own downloader special-cases that). So each
extra connection is a fresh `MTProtoSender` that reuses the main client's
in-memory `AuthKey` (own session id, own seqno; nothing is written to the
session file and no second `TelegramClient` exists). Lane 0 always uses the
main connection, which stays owned by the client and is never closed here.
If an extra connection cannot be set up, or breaks mid-upload, its parts go to
the main connection: the upload still succeeds.

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
import copy
import hashlib
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from telethon import custom, helpers, utils
from telethon.errors import FloodWaitError
from telethon.network import MTProtoSender
from telethon.tl import functions, types
from telethon.tl.alltlobjects import LAYER

logger = logging.getLogger(__name__)

MAX_WORKERS = 5
BIG_FILE_THRESHOLD = 10 * 1024 * 1024  # Telegram: above this, saveBigFilePart is required
MAX_FLOOD_RETRIES_PER_PART = 6
CONNECT_TIMEOUT = 20.0
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


class _Link:
    """One connection the lanes send through. `sender is None` is the main one."""

    def __init__(self, sender=None) -> None:
        self.sender = sender
        self.dead = False

    async def call(self, client, request):
        if self.sender is None:
            return await client(request)
        return await client._call(self.sender, request)


async def _open_sender(client) -> MTProtoSender:
    """A new connection to the client's current DC, authorised by the same key."""
    sender = MTProtoSender(client._sender.auth_key, loggers=client._log)
    try:
        session = client.session
        await sender.connect(
            client._connection(
                session.server_address,
                session.port,
                session.dc_id,
                loggers=client._log,
                proxy=client._proxy,
                local_addr=client._local_addr,
            )
        )
        # A new connection must announce the layer/app once. Copy the request:
        # the client's own one is mutated by Telethon on reconnects.
        init = copy.copy(client._init_request)
        init.query = functions.help.GetConfigRequest()
        await sender.send(functions.InvokeWithLayerRequest(LAYER, init))
    except BaseException:
        await sender.disconnect()
        raise
    return sender


async def _open_links(client, wanted: int) -> list[_Link]:
    """`wanted` links, the first being the main connection. Never raises: any
    extra connection that fails to come up is dropped with a log line."""
    links = [_Link()]
    if wanted <= 1:
        return links
    results = await asyncio.gather(
        *(asyncio.wait_for(_open_sender(client), CONNECT_TIMEOUT) for _ in range(wanted - 1)),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            logger.warning("Extra upload connection failed (%r); continuing with fewer", result)
        else:
            links.append(_Link(result))
    if len(links) == 1:
        logger.warning("No extra upload connection could be opened; uploading over the single main connection")
    else:
        logger.info("Uploading over %d TCP connections", len(links))
    return links


async def _close_links(links: list[_Link]) -> None:
    for link in links:
        if link.sender is not None:
            await link.sender.disconnect()


def _read_part(fd: int, offset: int, size: int) -> bytes:
    return os.pread(fd, size, offset)


async def upload_file_parallel(
    client,
    path: Path,
    *,
    workers: int = MAX_WORKERS,
    connections: int = 1,
    progress: ProgressCallback | None = None,
    file_name: str | None = None,
) -> types.InputFile | types.InputFileBig:
    workers = max(1, min(MAX_WORKERS, workers))
    connections = max(1, min(MAX_WORKERS, connections, workers))
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

    # No point in a connection per lane that will never get a part.
    links = await _open_links(client, min(connections, part_count))
    fd = os.open(path, os.O_RDONLY)
    try:
        lanes = [
            asyncio.create_task(
                _lane(client, lane, links, fd, file_id, part_count, part_size, is_big, pending, state, progress)
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
        await _close_links(links)

    if is_big:
        return types.InputFileBig(file_id, part_count, name)
    return custom.InputSizedFile(file_id, part_count, name, md5=md5, size=file_size)


def _md5_of(path: Path) -> hashlib._Hash:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest


async def _lane(client, lane, links, fd, file_id, part_count, part_size, is_big, pending, state, progress) -> None:
    link = links[lane % len(links)]
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
        link = await _send_via(client, link, links[0], request, index, state)
        state.uploaded += len(data)
        if progress is not None:
            result = progress(state.uploaded, state.total)
            if asyncio.iscoroutine(result):
                await result


async def _send_via(client, link: _Link, main: _Link, request, index: int, state: _State) -> _Link:
    """Send one part; if an extra connection fails (not a flood wait), retry
    that same part on the main connection and stay there. Returns the link to
    use for the next part."""
    try:
        await _send_part(client, link, request, index, state)
        return link
    except FloodWaitError:
        raise
    except Exception as exc:
        if link is main:
            raise
        link.dead = True
        logger.warning("Extra upload connection failed on part %d (%r); moving to the main connection", index, exc)
    await _send_part(client, main, request, index, state)
    return main


async def _send_part(client, link: _Link, request, index: int, state: _State) -> None:
    attempts = 0
    while True:
        try:
            ok = await link.call(client, request)
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
