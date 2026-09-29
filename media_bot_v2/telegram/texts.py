"""Hebrew user-facing text.

Ported from the old bot's BotText (src/config/constant.py), verbatim where
the content applies to v1's command set. Trimmed of references to
out-of-v1-scope commands (/buy, /spdl, /torrent, /adminpanel) and the
JDownloader fallback mention - advertising commands or fallbacks that don't
exist in this bot would just confuse users (spec/SPEC.md locked decision 4).

ABOUT has been updated to a neutral description of the bot and its capabilities,
with all upstream attribution and repository links removed per the project owner's
request (spec/SPEC.md question 3 closed).
"""

from __future__ import annotations

START = """
🎉 **ברוכים הבאים לבוט הורדות המדיה!**
שלחו לי קישור ואני אדאג להוריד עבורכם סרטונים, שירים וקבצי מדיה במהירות 🚀

🔹 מה הבוט יודע לעשות?
• 🎥 הורדת וידאו ואודיו מיוטיוב, טיקטוק ואינסטגרם.
• 📁 תמיכה גם בקישורי הורדה ישירה לכל סוגי הקבצים.
• 📤 שליחה אוטומטית של הקובץ ישירות לטלגרם.

🔹 פקודות שימושיות:
/start - התחלה
/help - עזרה ומידע נוסף
/settings - הגדרות הורדה

ℹ️ שימו לב: קישורים פרטיים או מוגנים לא תמיד נתמכים.
שלחו קישור ונתחיל ⬇️😄
"""

HELP = """
🤖 **מדריך שימוש בבוט ההורדות**
הבוט מאפשר הורדה של סרטונים, שירים וקבצים מיוטיוב, טיקטוק, אינסטגרם וגם מקישורי הורדה ישירה.

🔹 איך מורידים?
• פשוט שלחו קישור לתוכן הרצוי
• הבוט יזהה אוטומטית את האתר והפורמט
• 📤 הקובץ יישלח אליכם ישירות לטלגרם

🔹 פקודות זמינות:
/start: הודעת התחל.
/help, /about: מידע ועזרה.
/settings: הגדרת איכות, פורמט וכתוביות.
/ping: בדיקת מהירות תגובה.

🔹 מה אפשר להוריד?
  • 🎥 סרטונים
  • 🎵 אודיו ושירים
  • 📁 קבצים מקישורי הורדה ישירה

ℹ️ הערות חשובות:
• איכות ההורדה נבחרת אוטומטית לפי הזמינות
• קישורים פרטיים, מוגנים או בתשלום עשויים לא לעבוד
• יש להשתמש בתוכן באחריות ובהתאם לזכויות יוצרים
• לכל משתמש יש מכסת הורדות כדי שהבוט יישאר מהיר וזמין לכולם
"""

ABOUT = """
🤖 **בוט הורדות מדיה**

הבוט מאפשר הורדת וידאו ואודיו מיוטיוב, טיקטוק, אינסטגרם וקישורי הורדה ישירה.

🔹 מה הבוט מציע?
• 🎥 בחירת איכות הורדה מותאמת אישית.
• 📤 שליחת הקבצים במהירות ישירות לטלגרם.
• 🎫 ניהול מכסת הורדות לשמירה על שירות מהיר וזמין לכולם.
"""

# The settings screen's body is built dynamically from the user's actual
# current values (telegram/settings_menu.py's describe_settings) rather than
# a static block here, so the current quality/format/subtitles/description
# length is always what the screen shows - never a generic, possibly-stale
# description of the choices.
SETTINGS_HEADER = "⚙️ **הגדרות הורדה**"

SETTINGS_CREDITS = "💳 **קרדיטים נותרו:** {credits}"

YOUTUBE_QUALITY_SELECT = """
🎬 **בחר איכות להורדה**

📹 **{title}**
⏱️ משך: {duration}

👇 בחר את האיכות הרצויה:
"""

INSTAGRAM_PRIVATE_OR_LOGIN = (
    "❌ התוכן באינסטגרם פרטי, מוגבל או דורש התחברות לחשבון.\n➡️ הבוט תומך בהורדת תוכן ציבורי בלבד - נסה קישור אחר."
)
INSTAGRAM_NOT_FOUND = (
    "❌ הפוסט או הסרטון באינסטגרם אינו זמין (נמחק או שהקישור שגוי).\n➡️ בדוק את הקישור ונסה שוב."
)
INSTAGRAM_UNSUPPORTED_MEDIA = (
    "❌ סוג המדיה באינסטגרם אינו נתמך (למשל שידור חי או תוכן ללא מדיה נתמכת).\n➡️ נסה קישור לפוסט אחר."
)
INSTAGRAM_NETWORK_ERROR = "❌ שגיאת רשת בהורדה מאינסטגרם.\n➡️ נסה שוב בעוד מספר רגעים."
INSTAGRAM_GENERIC_FAILURE = "❌ ההורדה מאינסטגרם נכשלה.\n➡️ נסה שוב או שלח קישור אחר."
YOUTUBE_GENERIC_FAILURE = "❌ ההורדה מיוטיוב נכשלה.\n➡️ נסה שוב או שלח קישור אחר."

DOWNLOAD_STARTED = "בקשת ההורדה התקבלה..."
DOWNLOADING = "מוריד..."
PROCESSING = "מעבד..."
UPLOADING = "מעלה לטלגרם..."
DOWNLOAD_FROM_CACHE = "נמצא במטמון, שולח..."
DOWNLOAD_DONE = "✅ **הושלם**"
DOWNLOAD_FAILED = "❌ ההורדה נכשלה. נסה שוב או שלח קישור אחר."
FLOOD_WAIT_FAILED = "❌ טלגרם הגבילה את הפעילות עקב עומס זמני. אנא נסה שוב מאוחר יותר."
FLOOD_WAIT_MESSAGE = "⏳ עומס זמני בשרתי טלגרם, אנא המתן {seconds} שניות..."

CONTACT_URL = "https://t.me/YD_IL"
CONTACT_BUTTON = "לצ'אט איתי 💬"
CREDITS_BUTTON = "💬 לרכישת קרדיטים"
CREDITS_EXHAUSTED = "❌ נגמרו הקרדיטים שלך.\n➡️ אפשר לרכוש קרדיטים נוספים דרך הכפתור למטה 👇"
BANDWIDTH_EXHAUSTED = "❌ הגעת למגבלת 2GB היומית למשתמשים ללא מנוי.\n➡️ לרכישת חבילה ללא הגבלה צרו קשר בכפתור למטה 👇"

QUALITY_NAMES = {
    "1080": "1080p HD",
    "720": "720p",
    "480": "480p",
    "360": "360p",
    "audio": "שמע בלבד",
}
QUALITY_TOAST = "⏳ מתחיל הורדה באיכות {name}..."
QUALITY_TOAST_AUDIO = "⏳ מתחיל להוריד שמע..."
DOWNLOADING_QUALITY = "🔄 מוריד באיכות {name}..."
DOWNLOADING_AUDIO = "🔄 מוריד שמע..."

YOUTUBE_LINK_EXPIRED = "⏰ פג תוקף הקישור לבחירת איכות.\n➡️ שלח את קישור היוטיוב שוב ובחר איכות מחדש."
YOUTUBE_QUEUE_WAIT = "⏳ יש עומס הורדות כרגע, הבקשה שלך בתור..."
YOUTUBE_JS_RUNTIME_MISSING = (
    "⚠️ הורדות יוטיוב עלולות להיכשל: לא נמצא JavaScript runtime בשרת (Node.js או Deno). "
    "יש להתקין Node.js (גרסה 22+) או Deno (גרסה 2.3+) ולוודא שהם נגישים ב-PATH."
)

PING_MESSAGE = "בודק פינג..."
PING_RESULT = "פינג: {ms} מילישניות"

DIRECT_FILE_TOO_LARGE = (
    "❌ הקובץ גדול מדי ({size}). מגבלת ההורדה המרבית היא {max_size} - "
    "טלגרם אינה תומכת בהעברת קבצים בגודל כזה.\n➡️ נסה קישור לקובץ קטן יותר."
)
UNSUPPORTED_URL = (
    "❌ סוג הקישור אינו נתמך.\n➡️ שלח קישור מיוטיוב, טיקטוק, אינסטגרם או קישור הורדה ישירה לקובץ."
)
NOT_MEDIA_CONTENT = (
    "❌ הקישור מפנה לדף אינטרנט (HTML) או לתוכן טקסטואלי, לא לקובץ מדיה.\n"
    "➡️ ודא שהקישור מצביע ישירות על קובץ להורדה."
)
REQUEST_TIMEOUT_EXCEEDED = "⏱️ הבקשה בוטלה עקב חריגה ממגבלת הזמן.\n➡️ נסה שוב מאוחר יותר או הורד קובץ קטן יותר."

CANCEL_BUTTON = "❌ ביטול"
REQUEST_CANCELLED = "❌ הבקשה בוטלה."
CANCEL_TOAST = "מבטל את הבקשה..."
CANCEL_NOT_OWNER = "אפשר לבטל רק בקשה שהתחלת בעצמך."
CANCEL_NOT_ACTIVE = "אין בקשה פעילה לביטול."


def human_size(num_bytes: float | None) -> str:
    if not num_bytes:
        return "0B"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def format_download_too_large(file_size: float, max_size: float) -> str:
    return DIRECT_FILE_TOO_LARGE.format(
        size=human_size(file_size),
        max_size=human_size(max_size),
    )


def format_playlist_trim_reason(*, skipped_for_credits: int, too_large: int, failed: int) -> str | None:
    """Itemised reason for a trimmed playlist: what was never attempted because
    of the credit cap, what exceeded the size limit, and what failed outright."""
    parts: list[str] = []
    if skipped_for_credits > 0:
        parts.append(f"{skipped_for_credits} לא הורדו עקב מגבלת יתרת הקרדיטים")
    if too_large > 0:
        parts.append(f"{too_large} חרגו ממגבלת הגודל")
    if failed > 0:
        parts.append(f"{failed} אינם זמינים או נכשלו")
    return ", ".join(parts) if parts else None


def format_playlist_trimmed(downloaded: int, total: int, reason: str | None = None) -> str:
    lines = [DOWNLOAD_DONE, f"📦 הורדו {downloaded} מתוך {total} פריטים בפלייליסט."]
    if reason:
        lines.append(f"ℹ️ {reason}.")
    return "\n".join(lines)


def _elapsed_text(seconds: float) -> str:
    """Compact `M:SS`/`H:MM:SS` for the completed-request summary line - the
    owner's own example ("הושלם ב-1:23"), not the wordy `M:SS דקות` used for
    a video's own duration elsewhere, so the two concepts read differently
    even though both are fundamentally "time"."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _duration_text(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d} דקות"


def format_download_summary(
    *,
    quality_label: str | None = None,
    duration_seconds: int | None = None,
    size_bytes: int | None = None,
    elapsed_seconds: float | None = None,
) -> str:
    """`DOWNLOAD_DONE` plus whichever of quality/format, duration, size, and
    total request time are actually known - never a guessed or placeholder
    value, and never a credits balance (that stays in /settings only, per
    the owner's standing decision - see SETTINGS_CREDITS)."""
    lines = [DOWNLOAD_DONE]
    if quality_label:
        lines.append(f"🎬 נשלח: {quality_label}")

    detail_parts = []
    if duration_seconds:
        detail_parts.append(_duration_text(duration_seconds))
    if size_bytes:
        detail_parts.append(human_size(size_bytes))
    if detail_parts:
        lines.append(f"📦 {' · '.join(detail_parts)}")

    if elapsed_seconds is not None and elapsed_seconds >= 0:
        lines.append(f"⏱️ הושלם ב-{_elapsed_text(elapsed_seconds)}")

    return "\n".join(lines)


def format_failure_summary(attempts: list[tuple[str, str]]) -> str:
    if not attempts:
        return DOWNLOAD_FAILED
    lines = [DOWNLOAD_FAILED, "פירוט הניסיונות:"]
    for route_name, reason in attempts:
        lines.append(f"• {route_name}: {reason}")
    return "\n".join(lines)
