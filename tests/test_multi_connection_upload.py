"""M10.1: the lanes are spread over several real connections, proven on a simulated network.

`ConnectionNetwork` enforces a bandwidth cap **per connection** (the shape of
Telegram's limit): parts sent over the same connection queue behind each
other, parts on different connections do not. It counts distinct connections
(not requests), so "5 lanes on one connection" and "5 connections" are told
apart. Nothing here touches Telegram or a session file.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections import defaultdict

import pytest
from pydantic import ValidationError
from telethon import TelegramClient
from telethon.errors import FloodWaitError, MediaInvalidError

from media_bot_v2.telegram import parallel_upload
from media_bot_v2.telegram.parallel_upload import upload_file_parallel
from media_bot_v2.telegram.uploader import TelethonUploader
from tests.test_parallel_upload import PART, USER_CHAT, PartServer, _settings, make_file, sha

_LOGGERS = defaultdict(lambda: logging.getLogger("test"))
PART_SECONDS = 0.05  # per-connection cost of one 128KB part (~2.5MB/s stand-in for 1MB/s)


class FakeSender:
    def __init__(self, net: ConnectionNetwork, number: int, fail_after: int | None = None) -> None:
        self.net = net
        self.number = number
        self.fail_after = fail_after
        self.sent = 0
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True


class ConnectionNetwork(PartServer):
    """A `client` whose main connection and extra senders each have their own cap."""

    def __init__(self, open_plan: list[bool] | None = None) -> None:
        super().__init__(delay=0.0)
        self._sender = object()  # the main connection
        self.senders: list[FakeSender] = []
        self.open_plan = open_plan  # per extra sender: True = opens, False = fails
        self.opens = 0
        self.fail_after: dict[int, int] = {}  # extra sender number -> parts it accepts before dying
        self.used_connections: set[object] = set()
        self._wire: dict[object, asyncio.Lock] = {}

    async def open_sender(self, client) -> FakeSender:
        plan = (
            self.open_plan[self.opens]
            if self.open_plan and self.opens < len(self.open_plan)
            else True
        )
        self.opens += 1
        if not plan:
            raise ConnectionError("cannot reach the DC")
        sender = FakeSender(self, len(self.senders) + 1, self.fail_after.get(len(self.senders) + 1))
        self.senders.append(sender)
        return sender

    async def __call__(self, request):
        return await self._on_wire(self._sender, request)

    async def _call(self, sender, request):
        if sender.disconnected:
            raise ConnectionError("sender is closed")
        if sender.fail_after is not None and sender.sent >= sender.fail_after:
            raise ConnectionError("connection reset")
        sender.sent += 1
        return await self._on_wire(sender, request)

    async def _on_wire(self, connection, request):
        self.used_connections.add(connection)
        lock = self._wire.setdefault(connection, asyncio.Lock())
        async with lock:  # one pipe per connection: its parts are serialised
            await asyncio.sleep(PART_SECONDS)
        return await PartServer.__call__(self, request)


@pytest.fixture
def net(monkeypatch):
    network = ConnectionNetwork()

    async def opener(client):
        return await network.open_sender(client)

    monkeypatch.setattr(parallel_upload, "_open_sender", opener)
    return network


@pytest.fixture
def sleeps(monkeypatch):
    waited: list[float] = []

    async def fake_sleep(seconds):
        waited.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(parallel_upload, "_sleep", fake_sleep)
    return waited


# ---------------------------------------------------------------- the speed claim
async def test_five_connections_reach_five_times_the_throughput_of_one(tmp_path, monkeypatch):
    path = make_file(tmp_path / "f.bin", 20 * PART)  # 20 parts
    results = {}
    for label, connections in (("1 connection x5 lanes", 1), ("5 connections", 5)):
        network = ConnectionNetwork()

        async def opener(client, network=network):
            return await network.open_sender(client)

        monkeypatch.setattr(parallel_upload, "_open_sender", opener)
        begin = time.monotonic()
        await upload_file_parallel(network, path, workers=5, connections=connections)
        results[label] = (
            time.monotonic() - begin,
            len(network.used_connections),
            network.max_in_flight,
        )

    (one, one_conns, _), (five, five_conns, _) = results.values()
    print(
        f"\n[M10.1 measurement] per-connection cap, 20 parts x {PART_SECONDS * 1000:.0f}ms: "
        f"1 connection {one:.3f}s ({one_conns} conn), 5 connections {five:.3f}s ({five_conns} conn), "
        f"speedup x{one / five:.2f}"
    )
    assert one_conns == 1 and five_conns == 5  # real connections, not requests on one
    assert one >= 0.9  # the per-connection cap really binds: 5 lanes on one connection gain nothing
    assert one / five > 3.8


async def test_extra_connections_are_distinct_and_main_stays_open(tmp_path, net):
    path = make_file(tmp_path / "f.bin", 20 * PART)
    await upload_file_parallel(net, path, workers=5, connections=5)

    assert net.opens == 4  # main + 4 extra = 5
    assert len(net.used_connections) == 5
    assert net._sender in net.used_connections
    assert all(s.disconnected for s in net.senders)  # extras closed afterwards
    # The main connection object is never handed to the closer.
    assert not hasattr(net._sender, "disconnected")


@pytest.mark.parametrize(
    ("workers", "connections", "size", "expected_extra"),
    [
        (5, 5, 20 * PART, 4),
        (3, 5, 20 * PART, 2),
        (5, 1, 20 * PART, 0),
        (5, 5, PART, 0),
        (5, 5, 2 * PART, 1),
    ],
)
async def test_connection_count_is_capped_by_workers_and_parts(
    tmp_path, net, workers, connections, size, expected_extra
):
    path = make_file(tmp_path / "f.bin", size)
    await upload_file_parallel(net, path, workers=workers, connections=connections)
    assert net.opens == expected_extra


# ---------------------------------------------------------------- correctness
@pytest.mark.parametrize("size", [3 * 1024 * 1024 + 777, 11 * 1024 * 1024 + 12345])
async def test_file_built_from_five_connections_is_byte_identical(tmp_path, net, size):
    path = make_file(tmp_path / "clip.mp4", size)  # below and above the 10MB InputFileBig line
    handle = await upload_file_parallel(net, path, workers=5, connections=5)

    assert sha(net.assembled(handle)) == sha(path.read_bytes())
    assert len(net.used_connections) == 5
    assert net.big_flags == {size > 10 * 1024 * 1024}
    assert sorted(net.uploaded_ok) == list(range(handle.parts))  # each part exactly once
    assert handle.name == "clip.mp4"


# ---------------------------------------------------------------- fail-open
async def test_all_extra_connections_failing_falls_back_to_the_single_connection(
    tmp_path, net, caplog
):
    net.open_plan = [False] * 4
    path = make_file(tmp_path / "f.bin", 12 * PART)
    with caplog.at_level(logging.WARNING):
        handle = await upload_file_parallel(net, path, workers=5, connections=5)

    assert sha(net.assembled(handle)) == sha(path.read_bytes())
    assert net.used_connections == {net._sender}
    assert "uploading over the single main connection" in caplog.text
    assert "Extra upload connection failed" in caplog.text


async def test_some_extra_connections_failing_uses_the_rest(tmp_path, net):
    net.open_plan = [True, False, True, False]
    path = make_file(tmp_path / "f.bin", 12 * PART)
    handle = await upload_file_parallel(net, path, workers=5, connections=5)
    assert sha(net.assembled(handle)) == sha(path.read_bytes())
    assert len(net.used_connections) == 3


async def test_connection_dying_mid_upload_moves_its_parts_to_main(tmp_path, net, caplog):
    net.fail_after = {1: 2, 3: 0}
    path = make_file(tmp_path / "f.bin", 24 * PART)
    with caplog.at_level(logging.WARNING):
        handle = await upload_file_parallel(net, path, workers=5, connections=5)

    assert sha(net.assembled(handle)) == sha(path.read_bytes())
    assert sorted(net.uploaded_ok) == list(range(24))  # nothing lost, nothing sent twice
    assert "moving to the main connection" in caplog.text


async def test_a_hanging_connect_is_abandoned(tmp_path, monkeypatch):
    network = ConnectionNetwork()

    async def hang(client):
        await asyncio.sleep(60)

    monkeypatch.setattr(parallel_upload, "_open_sender", hang)
    monkeypatch.setattr(parallel_upload, "CONNECT_TIMEOUT", 0.05)
    path = make_file(tmp_path / "f.bin", 6 * PART)
    handle = await upload_file_parallel(network, path, workers=5, connections=5)
    assert sha(network.assembled(handle)) == sha(path.read_bytes())
    assert network.used_connections == {network._sender}


async def test_failure_on_main_still_propagates_and_closes_everything(tmp_path, net):
    original = PartServer.__call__

    async def boom(self, request):
        if request.file_part == 7:
            raise MediaInvalidError(request)
        return await original(self, request)

    PartServer.__call__ = boom
    try:
        path = make_file(tmp_path / "f.bin", 20 * PART)
        before = {t for t in asyncio.all_tasks()}
        with pytest.raises(MediaInvalidError):
            await upload_file_parallel(net, path, workers=5, connections=5)
    finally:
        PartServer.__call__ = original
    await asyncio.sleep(0)
    assert all(s.disconnected for s in net.senders)
    assert {t for t in asyncio.all_tasks()} <= before | {asyncio.current_task()}


# ---------------------------------------------------------------- FloodWait per connection
async def test_flood_on_an_extra_connection_retries_only_that_part(tmp_path, net, sleeps):
    net.flood_plan = {3: 1}  # part 3 (lane 3 -> an extra connection) floods once
    path = make_file(tmp_path / "f.bin", 12 * PART)
    handle = await upload_file_parallel(net, path, workers=5, connections=5)

    assert sha(net.assembled(handle)) == sha(path.read_bytes())
    assert sleeps == [7]
    assert net.attempts[3] == 2
    assert all(n == 1 for i, n in net.attempts.items() if i != 3)


async def test_repeated_floods_still_shrink_lanes_with_several_connections(tmp_path, net, sleeps):
    net.flood_plan = {i: 1 for i in range(6)}
    path = make_file(tmp_path / "f.bin", 30 * PART)
    handle = await upload_file_parallel(net, path, workers=5, connections=5)
    assert sha(net.assembled(handle)) == sha(path.read_bytes())


async def test_endless_flood_fails_cleanly_with_several_connections(tmp_path, net, sleeps):
    net.flood_plan = {2: 10**6}
    path = make_file(tmp_path / "f.bin", 12 * PART)
    with pytest.raises(FloodWaitError):
        await upload_file_parallel(net, path, workers=5, connections=5)
    assert all(s.disconnected for s in net.senders)


# ---------------------------------------------------------------- no second client / session file
async def test_no_second_client_and_no_session_file_are_opened(tmp_path, net, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a second TelegramClient / session file must never be opened")

    monkeypatch.setattr(TelegramClient, "__init__", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    path = make_file(tmp_path / "f.bin", 12 * PART)
    handle = await upload_file_parallel(net, path, workers=5, connections=5)
    assert sha(net.assembled(handle)) == sha(path.read_bytes())


async def test_real_open_sender_reuses_the_main_auth_key_and_never_builds_a_client(monkeypatch):
    """`_open_sender` against the real MTProtoSender class, with only the socket faked."""
    from telethon.crypto import AuthKey
    from telethon.network import MTProtoSender
    from telethon.tl import functions

    made, sent = {}, []

    class Conn:
        pass

    class Session:
        server_address, port, dc_id = "149.154.167.51", 443, 2

    class Main:
        session = Session()
        _sender = MTProtoSender(AuthKey(b"k" * 256), loggers=_LOGGERS)
        _log = _LOGGERS
        _proxy = None
        _local_addr = None

        def _connection(self, *args, **kwargs):
            made["args"] = args
            return Conn()

        _init_request = functions.InitConnectionRequest(
            api_id=1, device_model="d", system_version="s", app_version="a",
            lang_code="en", system_lang_code="en", lang_pack="", query=None,
        )  # fmt: skip

    async def fake_connect(self, connection):
        made["sender"] = self

    def fake_send(self, request, ordered=False):
        sent.append(request)
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(True)
        return fut

    monkeypatch.setattr(MTProtoSender, "connect", fake_connect)
    monkeypatch.setattr(MTProtoSender, "send", fake_send)

    main = Main()
    sender = await parallel_upload._open_sender(main)

    assert sender is not main._sender
    assert sender.auth_key is main._sender.auth_key  # same key, in memory
    assert made["args"] == ("149.154.167.51", 443, 2)  # same DC as the account
    assert isinstance(sent[0], functions.InvokeWithLayerRequest)
    assert isinstance(sent[0].query.query, functions.help.GetConfigRequest)
    assert main._init_request.query is None  # the client's own request was not mutated


# ---------------------------------------------------------------- config and wiring
def test_upload_connections_config(monkeypatch):
    def connections(value):
        if value is None:
            monkeypatch.delenv("UPLOAD_CONNECTIONS", raising=False)
        else:
            monkeypatch.setenv("UPLOAD_CONNECTIONS", value)
        return _settings(monkeypatch, None).upload_connections

    assert connections(None) == 5
    assert connections("1") == 1
    assert connections("3") == 3
    assert connections("9") == 5  # clamped
    for bad in ("0", "-1"):
        with pytest.raises(ValidationError, match="UPLOAD_CONNECTIONS"):
            connections(bad)


async def test_uploader_passes_connections_to_the_parallel_upload(tmp_path, net):
    path = make_file(tmp_path / "a.bin", 20 * PART)
    uploader = TelethonUploader(
        net, chat_id=USER_CHAT, archive_channel=None, workers=5, connections=5
    )
    await uploader.send_file(path)
    handle = net.send_files()[0].args[1]
    assert sha(net.assembled(handle)) == sha(path.read_bytes())
    assert len(net.used_connections) == 5


async def test_uploader_default_is_single_connection(tmp_path, net):
    path = make_file(tmp_path / "a.bin", 20 * PART)
    await TelethonUploader(net, chat_id=USER_CHAT, archive_channel=None, workers=5).send_file(path)
    assert net.used_connections == {net._sender}
    assert net.opens == 0
