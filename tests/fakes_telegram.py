"""A recording stand-in for the Telethon client.

Delivery tests need to assert what is *actually sent to Telegram* - which
method, which keyword arguments, in which order - not merely that some
uploader function ran. `FakeTelegramClient` records every call with its full
args/kwargs, hands out sequential message ids, remembers sent messages so
`get_messages` can find them, and can be told to fail the Nth `send_file`.

`forward_messages` exists only so a test can assert it is never used: the
bot must not show recipients a "Forwarded from <archive channel>" header.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeMessage:
    id: int
    chat: Any
    media: Any
    caption: str | None = None
    reply_to: Any = None


@dataclass
class FakeMedia:
    """What Telegram would hold for an uploaded file (identity is what matters)."""

    name: str


@dataclass
class Call:
    method: str
    args: tuple
    kwargs: dict = field(default_factory=dict)


class FakeTelegramClient:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.stored: dict[tuple[Any, int], FakeMessage] = {}
        self._next_id = 0
        self._send_file_count = 0
        self.fail_send_file: dict[int, Exception] = {}

    # -- recording helpers -------------------------------------------------
    def calls_to(self, method: str) -> list[Call]:
        return [c for c in self.calls if c.method == method]

    def send_files(self, chat: Any = None) -> list[Call]:
        return [c for c in self.calls_to("send_file") if chat is None or c.args[0] == chat]

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    # -- Telethon surface ----------------------------------------------------
    async def send_file(self, entity, file, **kwargs):
        self.calls.append(Call("send_file", (entity, file), kwargs))
        self._send_file_count += 1
        failure = self.fail_send_file.get(self._send_file_count)
        if failure is not None:
            raise failure
        media = file if not isinstance(file, str) else FakeMedia(file.rsplit("/", 1)[-1])
        message = FakeMessage(self._new_id(), entity, media, kwargs.get("caption"))
        self.stored[(entity, message.id)] = message
        return message

    async def send_message(self, entity, message, **kwargs):
        self.calls.append(Call("send_message", (entity, message), kwargs))
        sent = FakeMessage(self._new_id(), entity, None, message, kwargs.get("reply_to"))
        self.stored[(entity, sent.id)] = sent
        return sent

    async def edit_message(self, entity, message, text=None, **kwargs):
        self.calls.append(Call("edit_message", (entity, message, text), kwargs))
        return message

    async def get_messages(self, entity, ids=None, **kwargs):
        self.calls.append(Call("get_messages", (entity,), {"ids": ids, **kwargs}))
        if isinstance(ids, list):
            return [self.stored.get((entity, i)) for i in ids]
        return self.stored.get((entity, ids))

    async def forward_messages(self, *args, **kwargs):
        self.calls.append(Call("forward_messages", args, kwargs))
        raise AssertionError("forward_messages must never be used (leaks 'Forwarded from')")
