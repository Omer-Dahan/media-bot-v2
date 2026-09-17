"""Settings menu: toggle-style buttons cycling in place.

Ported from the old bot's _build_settings_markup / toggle_*_callback
(src/main.py:850-1000). The toggle math (apply_toggle, get_or_create_user)
is kept as plain functions with no Telethon event objects, so it is
unit-testable against a real SQLAlchemy session without mocking Telethon.
"""

from __future__ import annotations

from telethon import Button

from media_bot_v2.db.models import Setting, User
from media_bot_v2.telegram.callback_data import encode

QUALITY_CYCLE = {"high": "medium", "medium": "low", "low": "high"}
QUALITY_DISPLAY = {"high": "1080p", "medium": "720p", "low": "480p"}
FORMAT_DISPLAY = {"video": "וידאו", "document": "קובץ"}
TITLE_LENGTH_CYCLE = {100: 250, 250: 500, 500: 1000, 1000: 4000, 4000: 0, 0: 100}

TOGGLE_QUALITY = "toggle_quality"
TOGGLE_FORMAT = "toggle_format"
TOGGLE_SUBTITLES = "toggle_subtitles"
TOGGLE_TITLE_LEN = "toggle_title_len"


def get_or_create_user(
    session, user_id: int, *, first_name: str | None, username: str | None, free_download: int
) -> User:
    user = session.query(User).filter(User.user_id == user_id).first()
    if user is None:
        user = User(
            user_id=user_id,
            first_name=first_name,
            username=username,
            free=free_download,
            paid=0,
            bandwidth_used=0,
            total_bandwidth=0,
            is_blocked=0,
        )
        session.add(user)
        session.flush()
    else:
        if first_name:
            user.first_name = first_name
        if username:
            user.username = username

    if user.settings is None:
        user.settings = Setting(quality="high", format="video", subtitles=0, title_length=500)
        session.flush()

    return user


def build_settings_buttons(setting: Setting) -> list[list[Button]]:
    title_len_display = setting.title_length if setting.title_length != 0 else "ללא הגבלה 🔗"
    return [
        [Button.inline(f"🎥 איכות: {QUALITY_DISPLAY.get(setting.quality, '1080p')}", encode(TOGGLE_QUALITY))],
        [Button.inline(f"📤 שליחה: {FORMAT_DISPLAY.get(setting.format, 'וידאו')}", encode(TOGGLE_FORMAT))],
        [Button.inline(f"📝 כתוביות: {'פעיל' if setting.subtitles else 'כבוי'}", encode(TOGGLE_SUBTITLES))],
        [Button.inline(f"📑 אורך תיאור: {title_len_display}", encode(TOGGLE_TITLE_LEN))],
    ]


def apply_toggle(setting: Setting, toggle_key: str) -> str:
    """Mutate `setting` in place for the given toggle; return the answer text."""
    if toggle_key == TOGGLE_QUALITY:
        setting.quality = QUALITY_CYCLE.get(setting.quality, "high")
        return f"✅ איכות: {QUALITY_DISPLAY[setting.quality]}"

    if toggle_key == TOGGLE_FORMAT:
        setting.format = "document" if setting.format == "video" else "video"
        return f"✅ שליחה: {FORMAT_DISPLAY[setting.format]}"

    if toggle_key == TOGGLE_SUBTITLES:
        setting.subtitles = 0 if setting.subtitles else 1
        return "✅ כתוביות: פעיל" if setting.subtitles else "❌ כתוביות: כבוי"

    if toggle_key == TOGGLE_TITLE_LEN:
        setting.title_length = TITLE_LENGTH_CYCLE.get(setting.title_length, 500)
        if setting.title_length == 4000:
            return "📋 בחרת בתיאור מלא!\nהתיאור יישלח בהודעה נפרדת מתחת למדיה."
        if setting.title_length == 0:
            return "🌐 התיאור יישלח כדף Telegraph!\nהקישור יישלח בהודעה נפרדת מתחת למדיה."
        return f"✅ אורך תיאור: {setting.title_length} תווים"

    raise ValueError(f"Unknown toggle: {toggle_key}")
