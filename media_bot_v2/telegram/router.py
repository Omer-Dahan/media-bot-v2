"""Request router: maps incoming messages/commands to engine dispatch.

Placeholder for M1 - wires up command handlers (/start, /help, /settings,
...) and delegates URL messages to media_bot_v2.engines. No engine logic
lives here.
"""

from __future__ import annotations

from telethon import TelegramClient, events


def register_handlers(client: TelegramClient) -> None:
    @client.on(events.NewMessage(pattern="/start"))
    async def start_handler(event: events.NewMessage.Event) -> None:
        await event.respond("media-bot-v2 skeleton: handlers not implemented yet.")
