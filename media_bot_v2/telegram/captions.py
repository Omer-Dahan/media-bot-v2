"""Caption ("signature") text sent with delivered media.

Ported from the old bot's `get_metadata` (src/engine/base.py:417-526) and its
split-upload / archive helpers. All captions are HTML: every dynamic value
(title, URL, file name, user name) is `html.escape`d and the send call must use
`parse_mode="html"`.

Deliberately absent: a "credits left" line. The owner banned it from the
signature; the balance is shown only in the settings screen.

Telegram limits a caption to 1024 characters *after* markup is parsed,
counted in UTF-16 code units (an emoji is 2). `fit` shrinks the title - never
the fixed template text - until that holds, and finally the URL if the URL
alone is too long.
"""

from __future__ import annotations

import html

from telethon.extensions import html as tl_html

from media_bot_v2.upload.media_probe import KIND_AUDIO, KIND_VIDEO

CAPTION_LIMIT = 1024
DESCRIPTION_LIMIT = 4000
_ELLIPSIS = "…"

# Old bot: a title length of 0 (unlimited, Telegraph) or >= 1000 is cut to 750
# so that the URL and the template text still fit under 1024.
_UNLIMITED_TITLE_CAP = 750


def utf16_units(caption_html: str) -> int:
    """Length Telegram will measure: parsed text, in UTF-16 code units."""
    plain, _ = tl_html.parse(caption_html)
    return len(plain.encode("utf-16-le")) // 2


def title_limit(title_length: int) -> int:
    """Characters of the title to keep for the user's "description length" setting."""
    if title_length == 0 or title_length >= 1000:
        return _UNLIMITED_TITLE_CAP
    return title_length


def format_duration(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d} דקות"


def part_label(index: int, total: int) -> str:
    return f"📎 חלק {index}/{total}"


def doc_part_label(index: int, total: int, filename: str) -> str:
    return f"📎 חלק {index}/{total}: {html.escape(filename)}"


def with_part_label(label: str, full_caption: str) -> str:
    return f"{label}\n\n{full_caption}"


def subtitle_caption(filename: str) -> str:
    return f"📝 כתוביות: {html.escape(filename)}"


def build_user_caption(
    *,
    kind: str,
    title: str | None,
    url: str,
    width: int = 0,
    height: int = 0,
    duration: int = 0,
    title_length: int = 500,
    reserved_units: int = 0,
) -> str:
    """The signature under a delivered file.

    `reserved_units` keeps room for text the caller will prepend (a part
    label), so the combined caption still fits.
    """
    shown_title = (title or "").strip() or "Unknown"
    shown_title = shown_title[: title_limit(title_length)]
    limit = CAPTION_LIMIT - reserved_units

    def render(t: str, u: str) -> str:
        return _render(kind, t, u, width=width, height=height, duration=duration)

    caption = render(shown_title, url)
    overflow = utf16_units(caption) - limit
    while overflow > 0 and shown_title:
        cut = max(overflow, 1)
        # The title carries an ellipsis once shortened, so shave a little extra.
        keep = max(len(shown_title) - cut - 1, 0)
        shown_title = shown_title[:keep].rstrip() + (_ELLIPSIS if keep else "")
        caption = render(shown_title, url)
        overflow = utf16_units(caption) - limit
        if not keep:
            break

    shown_url = url
    while overflow > 0 and len(shown_url) > 1:
        keep = max(len(shown_url) - overflow - 1, 0)
        shown_url = shown_url[:keep] + _ELLIPSIS
        caption = render(shown_title, shown_url)
        overflow = utf16_units(caption) - limit
        if not keep:
            break
    return caption


def _render(kind: str, title: str, url: str, *, width: int, height: int, duration: int) -> str:
    esc_title = html.escape(title)
    esc_url = html.escape(url)
    if kind == KIND_AUDIO:
        duration_line = f"⏱️ אורך: {format_duration(duration)}\n" if duration else ""
        return (
            f"🎵 <b>{esc_title}</b>\n\n"
            f"🔗 מקור: {esc_url}\n"
            f"{duration_line}"
            "⬇️ הקובץ מוכן להורדה\n"
            "שמיעה מהנה 🎧✨"
        )
    if kind == KIND_VIDEO:
        resolution_line = f"📐 רזולוציה: {width}x{height}\n" if width and height else ""
        duration_line = f"⏱️ אורך: {format_duration(duration)}\n" if duration else ""
        return (
            f"🎬 <b>{esc_title}</b>\n\n"
            f"🔗 מקור: {esc_url}\n"
            f"{resolution_line}"
            f"{duration_line}"
            "\n⬇️ הקובץ מוכן לצפייה והורדה\n"
            "צפייה מהנה 👀✨"
        )
    return f"📁 <b>{esc_title}</b>\n\n🔗 מקור: {esc_url}"


def build_description_message(title: str | None, description: str | None) -> str | None:
    """Follow-up message for the "4000" description-length setting."""
    full_text = "\n\n".join(part for part in ((title or "").strip(), (description or "").strip()) if part)
    if not full_text:
        return None
    body = html.escape(full_text[:DESCRIPTION_LIMIT])
    return f"📋 <b>תיאור מלא:</b>\n\n<blockquote expandable>{body}</blockquote>"


def build_archive_caption(*, user_display: str, user_id: int, filename: str, url: str) -> str:
    """Operator-facing header of the archive copy (never shown to a user)."""
    shown_name = filename if len(filename) <= 200 else filename[:200] + "..."
    shown_url = url if len(url) <= 300 else url[:300] + "..."
    return (
        f"👤 משתמש: {html.escape(user_display)}\n"
        f"🆔 {user_id}\n"
        f"📁 קובץ: {html.escape(shown_name)}\n"
        f"<blockquote expandable>🔗 קישור: {html.escape(shown_url)}</blockquote>"
    )
