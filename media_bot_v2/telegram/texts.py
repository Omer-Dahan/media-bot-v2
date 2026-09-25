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
🎉 ברוכים הבאים לבוט הורדות המדיה! 🎥🎵
שלחו לי קישור ואני אדאג להוריד עבורכם סרטונים, שירים וקבצי מדיה במהירות 🚀

🔹 מה הבוט יודע לעשות?
• הורדת וידאו ואודיו מיוטיוב, טיקטוק ואינסטגרם.
• תמיכה גם בקישורי הורדה ישירה לכל סוגי הקבצים.
• שליחה אוטומטית של הקובץ ישירות לטלגרם.

🔹 פקודות שימושיות:
/start - התחלה
/help - עזרה ומידע נוסף
/settings - הגדרות הורדה
ℹ️ שימו לב: קישורים פרטיים או מוגנים לא תמיד נתמכים.
שלחו קישור ונתחיל ⬇️😄
"""

HELP = """
🤖 מדריך שימוש בבוט ההורדות
הבוט מאפשר הורדה של סרטונים, שירים וקבצים מיוטיוב, טיקטוק, אינסטגרם וגם מקישורי הורדה ישירה.

🔹 איך מורידים?
• פשוט שלחו קישור לתוכן הרצוי
• הבוט יזהה אוטומטית את האתר והפורמט
• הקובץ יישלח אליכם ישירות לטלגרם 📥

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
🤖 בוט הורדות מדיה

הבוט מאפשר הורדת וידאו ואודיו מיוטיוב, טיקטוק, אינסטגרם וקישורי הורדה ישירה.

🔹 מה הבוט מציע?
• בחירת איכות הורדה מותאמת אישית.
• שליחת הקבצים במהירות ישירות לטלגרם.
• ניהול מכסת הורדות לשמירה על שירות מהיר וזמין לכולם.
"""

SETTINGS = """
⚙️ **הגדרות הורדה (YouTube)**
בחר איכות וידאו וצורת שליחה.

🎥 **איכות:** 1080p | 720p | 480p
📤 **שליחה:** וידאו (צפייה ישירה) | קובץ (לא מתנגן)
📝 **כתוביות:** פעיל / כבוי
📑 **אורך תיאור:** 100-1000 | 4000 (נפרד) | ללא הגבלה 🔗
"""

SETTINGS_CREDITS = "💳 **קרדיטים נותרו:** {credits}"

YOUTUBE_QUALITY_SELECT = """
🎬 **בחר איכות להורדה**

📹 **{title}**
⏱️ משך: {duration}

👇 בחר את האיכות הרצויה:
"""

INSTAGRAM_PRIVATE_OR_LOGIN = (
    "❌ התוכן באינסטגרם פרטי, מוגבל או דורש התחברות לחשבון.\nהבוט תומך בהורדת תוכן ציבורי בלבד."
)
INSTAGRAM_NOT_FOUND = "❌ הפוסט או הסרטון באינסטגרם אינו זמין (נמחק או שהקישור שגוי)."
INSTAGRAM_UNSUPPORTED_MEDIA = (
    "❌ סוג המדיה באינסטגרם אינו נתמך (למשל שידור חי או תוכן ללא מדיה נתמכת)."
)
INSTAGRAM_NETWORK_ERROR = "❌ שגיאת רשת בהורדה מאינסטגרם. נסה שוב בעוד מספר רגעים."
INSTAGRAM_GENERIC_FAILURE = "ההורדה מאינסטגרם נכשלה. נסה שוב או שלח קישור אחר."
YOUTUBE_GENERIC_FAILURE = "ההורדה מיוטיוב נכשלה. נסה שוב או שלח קישור אחר."

DOWNLOAD_STARTED = "בקשת ההורדה התקבלה..."
DOWNLOADING = "מוריד..."
PROCESSING = "מעבד..."
UPLOADING = "מעלה לטלגרם..."
DOWNLOAD_FROM_CACHE = "נמצא במטמון, שולח..."
DOWNLOAD_DONE = "הושלם ✅"
DOWNLOAD_FAILED = "❌ ההורדה נכשלה. נסה שוב או שלח קישור אחר."

YOUTUBE_LINK_EXPIRED = "⏰ תפוגת קישור: יש לשלוח את קישור היוטיוב שוב ולבחור איכות מחדש."
YOUTUBE_QUEUE_WAIT = "⏳ יש עומס הורדות כרגע, הבקשה שלך בתור..."
YOUTUBE_JS_RUNTIME_MISSING = (
    "⚠️ הורדות יוטיוב עלולות להיכשל: לא נמצא JavaScript runtime בשרת (Node.js או Deno). "
    "יש להתקין Node.js (גרסה 22+) או Deno (גרסה 2.3+) ולוודא שהם נגישים ב-PATH."
)

PING_MESSAGE = "בודק פינג..."
PING_RESULT = "פינג: {ms} מילישניות"

DIRECT_FILE_TOO_LARGE = "❌ הקובץ גדול מדי ({size}). מגבלת ההורדה המרבית היא {max_size}. טלגרם אינה תומכת בהעברת קבצים בגודל כזה."
UNSUPPORTED_URL = "❌ סוג הקישור אינו נתמך. הבוט תומך ביוטיוב, טיקטוק, אינסטגרם וקישורי הורדה ישירה לקבצים."
NOT_MEDIA_CONTENT = (
    "קישור זה מפנה לדף אינטרנט או לתוכן טקסטואלי (HTML/JSON/XML) ולא לקובץ מדיה או הורדה ישירה."
)
REQUEST_TIMEOUT_EXCEEDED = "⏱️ הבקשה בוטלה עקב חריגה ממגבלת הזמן (timeout). נסה שוב מאוחר יותר או הורד קובץ קטן יותר."


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
    if reason:
        return f"הושלם ✅\nהורדו {downloaded} מתוך {total} פריטים בפלייליסט ({reason})."
    return f"הושלם ✅\nהורדו {downloaded} מתוך {total} פריטים בפלייליסט."


def format_failure_summary(attempts: list[tuple[str, str]]) -> str:
    if not attempts:
        return DOWNLOAD_FAILED
    lines = [DOWNLOAD_FAILED, "פירוט הניסיונות:"]
    for route_name, reason in attempts:
        lines.append(f"• {route_name}: {reason}")
    return "\n".join(lines)
