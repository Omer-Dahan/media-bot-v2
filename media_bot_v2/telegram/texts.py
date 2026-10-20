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

import random
import zlib

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

HELP = """🤖 **מדריך שימוש בבוט ההורדות**

ברוכים הבאים! הבוט מאפשר להוריד סרטונים, שירים וקבצים במהירות ובאיכות גבוהה ישירות לטלגרם.

🔹 **מה אפשר להוריד?**
• 📺 **יוטיוב:** סרטונים בכל האיכויות הזמינות, או חילוץ שמע לקובץ MP3.
• 🎵 **טיקטוק:** סרטונים באיכות מקורית וללא סימן מים.
• 📸 **אינסטגרם:** סרטוני Reels, פוסטים וסרטונים ציבוריים.
• 📁 **קישורים ישירים:** הורדה מהירה של קבצי מדיה מכל קישור ישיר נתמך.

🔹 **איך משתמשים?**
• פשוט שולחים קישור לתוכן הרצוי בצ'אט.
• ביוטיוב תוכלו לבחור את איכות הווידאו או להוריד כשמע בלבד.
• **ביטול בזמן אמת:** בזמן ההורדה מופיע כפתור "ביטול" לעצירת הבקשה בכל רגע.
• **ניסיון חוזר:** אם הורדה נכשלה, יופיע כפתור "נסה שוב" להפעלה חוזרת בלחיצה אחת.

💡 **טיפים לאיכות מרבית:**
• להורדה באיכות המקורית ללא דחיסה של טלגרם, הגדירו בהגדרות שליחה כקובץ.
• שימו לב: קישורים פרטיים או כאלה הדורשים התחברות אינם נתמכים.
• קבצים שהורדו בעבר יישלחו אליכם מיד מהמטמון ללא צורך בהמתנה.

⌨️ **פקודות זמינות:**
• /start - התחלת שימוש והצגת הודעת הפתיחה
• /help - פתיחת מדריך זה
• /settings - בחירת איכות ברירת מחדל, פורמט, כתוביות ובדיקת קרדיטים
• /ping - בדיקת מהירות התגובה של הבוט"""

ABOUT = """
🤖 **בוט הורדות מדיה**

הבוט מאפשר הורדת וידאו ואודיו מיוטיוב, טיקטוק, אינסטגרם, פייסבוק וקישורי הורדה ישירה.

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

FACEBOOK_PRIVATE_OR_LOGIN = (
    "❌ התוכן בפייסבוק פרטי, מוגבל או דורש התחברות לחשבון.\n➡️ הבוט תומך בהורדת תוכן ציבורי בלבד - נסה קישור אחר."
)
FACEBOOK_NOT_FOUND = (
    "❌ הסרטון או הפוסט בפייסבוק אינו זמין (נמחק או שהקישור שגוי).\n➡️ בדוק את הקישור ונסה שוב."
)
FACEBOOK_UNSUPPORTED_MEDIA = (
    "❌ סוג המדיה בפייסבוק אינו נתמך (למשל שידור חי או תוכן ללא מדיה נתמכת).\n➡️ נסה קישור לסרטון אחר."
)
FACEBOOK_NETWORK_ERROR = "❌ שגיאת רשת בהורדה מפייסבוק.\n➡️ נסה שוב בעוד מספר רגעים."
FACEBOOK_GENERIC_FAILURE = "❌ ההורדה מפייסבוק נכשלה.\n➡️ נסה שוב או שלח קישור אחר."

DOWNLOAD_STARTED = "בקשת ההורדה התקבלה..."
DOWNLOADING = "מוריד..."
PROCESSING = "מעבד..."
UPLOADING = "מעלה לטלגרם..."
DOWNLOAD_FROM_CACHE = "נמצא במטמון, שולח..."
DOWNLOAD_DONE = "✅ **הושלם**"
CACHE_DELIVERY_SUMMARY = "⚡ נשלח מהמטמון (מיידי)"
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
QUEUE_WAIT = "⏳ ממתין לעיבוד..."
YOUTUBE_QUEUE_WAIT = QUEUE_WAIT
YOUTUBE_JS_RUNTIME_MISSING = (
    "⚠️ הורדות יוטיוב עלולות להיכשל: לא נמצא JavaScript runtime בשרת (Node.js או Deno). "
    "יש להתקין Node.js (גרסה 22+) או Deno (גרסה 2.3+) ולוודא שהם נגישים ב-PATH."
)

PING_MESSAGE = "בודק פינג..."
PING_RESULT = "🟢 הבוט פעיל · זמן תגובה: {ms}ms"

DIRECT_FILE_TOO_LARGE = (
    "❌ הקובץ גדול מדי ({size}). מגבלת ההורדה המוגדרת בבוט היא {max_size}.\n"
    "➡️ נסה קישור לקובץ קטן יותר."
)
UNSUPPORTED_URL = (
    "❌ סוג הקישור אינו נתמך.\n➡️ שלח קישור מיוטיוב, טיקטוק, אינסטגרם, פייסבוק או קישור הורדה ישירה לקובץ."
)
NOT_MEDIA_CONTENT = (
    "❌ הקישור מפנה לדף אינטרנט (HTML) או לתוכן טקסטואלי, לא לקובץ מדיה.\n"
    "➡️ ודא שהקישור מצביע ישירות על קובץ להורדה."
)
DIRECT_LINK_REFUSED = (
    "❌ האתר חוסם הורדה ישירה של קובץ זה (נדרשת הרשאה או שהגישה נחסמה).\n➡️ נסה קישור אחר."
)
DIRECT_LINK_GONE = "❌ הקובץ בקישור אינו קיים יותר באתר המקור.\n➡️ נסה קישור אחר."
DIRECT_LINK_SERVER_BUSY = (
    "❌ שרת האתר עמוס או חוסם גישה זמנית לקובץ.\n➡️ נסה שוב בעוד מספר דקות."
)
REQUEST_TIMEOUT_EXCEEDED = "⏱️ הבקשה בוטלה עקב חריגה ממגבלת הזמן.\n➡️ נסה שוב מאוחר יותר או הורד קובץ קטן יותר."

CANCEL_BUTTON = "❌ ביטול"
REQUEST_CANCELLED = "❌ הבקשה בוטלה."
CANCEL_TOAST = "מבטל את הבקשה..."
CANCEL_NOT_OWNER = "אפשר לבטל רק בקשה שהתחלת בעצמך."
CANCEL_NOT_ACTIVE = "אין בקשה פעילה לביטול."

RETRY_BUTTON = "🔄 נסה שוב"
RETRY_NOT_OWNER = "אפשר לנסות שוב רק בקשה שהתחלת בעצמך."
RETRY_ALREADY_RUNNING = "הבקשה כבר רצה."
RETRY_NOT_AVAILABLE = "הכפתור כבר לא זמין."


# --- Randomized phrasing variants ---
#
# Each tuple below rotates the Hebrew phrasing for one canonical status/error
# constant. `pick` selects deterministically per `key` (e.g. a message id) so
# a single progress message never flickers between phrasings as it's edited.

DOWNLOADING_VARIANTS = (
    "מוריד... הקובץ עוד לא יודע שהוא מוריד.",
    "מוריד עכשיו. הקובץ מתנגד, אני מנצח.",
    "מוריד. סבלנות, גם הקובץ צריך לרדת.",
    "מוריד — בדרך אליי, ואז אליך.",
    "מוריד. עוד רגע וזה אצלך ביד.",
    "מוריד... הקובץ לא ממהר לשום מקום.",
    "מוריד. אני עובד, הוא מתבכיין.",
    "מוריד. תכף תקבל הודעה שזה מוכן.",
)

UPLOADING_VARIANTS = (
    "📤 מעלה לטלגרם... כמעט אצלך.",
    "📤 מעלה לטלגרם. הקובץ בדרך, תפנה לו מקום.",
    "📤 מעלה לטלגרם. עוד רגע זה קופץ לצ'אט.",
    "📤 מעלה לטלגרם — הרגע האחרון והכי איטי.",
    "📤 מעלה לטלגרם. זה הקובץ הגדול, אז לוקח קצת.",
    "📤 מעלה לטלגרם... זה כבר כמעט שלך.",
    "📤 מעלה לטלגרם. הקובץ בטיסה, חגור.",
    "📤 מעלה לטלגרם. עוד שנייה, בלי להגזים.",
)

PROCESSING_VARIANTS = (
    "🧠 מסדר את זה לפני שמגיש.",
    "🧠 עוד ליטוש ואתה מקבל.",
    "🧠 מעבד. הקובץ עובר טיפול.",
)

DOWNLOAD_FROM_CACHE_VARIANTS = (
    "⚡ כבר הורדתי את זה פעם. שולח מיד.",
    "⚡ זה אצלי במטמון. מגיע מיד.",
    "⚡ אותו קובץ בדיוק, אין צורך להוריד שוב.",
)

_UNSUPPORTED_URL_LINES = (
    "❌ הקישור הזה לא מוביל לקובץ שאני יודע להוריד.",
    "❌ מהקישור הזה אין מה להוריד.",
    "❌ הקישור הזה לא זז בשום כיוון.",
)
_UNSUPPORTED_URL_TAIL = "➡️ שלח קישור מיוטיוב, טיקטוק, אינסטגרם, פייסבוק או קישור הורדה ישירה לקובץ."
UNSUPPORTED_URL_VARIANTS = tuple(f"{line}\n{_UNSUPPORTED_URL_TAIL}" for line in _UNSUPPORTED_URL_LINES)

_NOT_MEDIA_CONTENT_LINES = (
    "❌ זה דף אינטרנט, לא קובץ. אני מוריד מדיה, לא קורא מאמרים.",
    "❌ הקישור מוביל לדף, לא לקובץ להורדה.",
    "❌ מצאתי טקסט, לא וידאו.",
)
_NOT_MEDIA_CONTENT_TAIL = "➡️ ודא שהקישור מצביע ישירות על קובץ להורדה."
NOT_MEDIA_CONTENT_VARIANTS = tuple(f"{line}\n{_NOT_MEDIA_CONTENT_TAIL}" for line in _NOT_MEDIA_CONTENT_LINES)

_NOT_FOUND_LINES = (
    "❌ הפוסט הזה כבר לא שם. מישהו מחק אותו, או שהקישור שגוי.",
    "❌ חיפשתי, לא מצאתי. הקישור כנראה שבור.",
    "❌ הקובץ יצא לטיול ולא השאיר הודעה.",
    "❌ אופס, לא מצאתי שום דבר שזז בקישור שלך. 🐕",
)
_NOT_FOUND_TAIL = "➡️ בדוק את הקישור ונסה שוב."
INSTAGRAM_NOT_FOUND_VARIANTS = tuple(f"{line}\n{_NOT_FOUND_TAIL}" for line in _NOT_FOUND_LINES)
FACEBOOK_NOT_FOUND_VARIANTS = tuple(f"{line}\n{_NOT_FOUND_TAIL}" for line in _NOT_FOUND_LINES)

_PRIVATE_OR_LOGIN_LINES = (
    "❌ זה פרטי. הדלת נעולה מבפנים.",
    "❌ התוכן הזה דורש התחברות, ואין לי חשבון.",
    "❌ האתר לא מרשה לי להיכנס לקישור הזה.",
)
_PRIVATE_OR_LOGIN_TAIL = "➡️ הבוט תומך בהורדת תוכן ציבורי בלבד - נסה קישור אחר."
INSTAGRAM_PRIVATE_OR_LOGIN_VARIANTS = tuple(f"{line}\n{_PRIVATE_OR_LOGIN_TAIL}" for line in _PRIVATE_OR_LOGIN_LINES)
FACEBOOK_PRIVATE_OR_LOGIN_VARIANTS = tuple(f"{line}\n{_PRIVATE_OR_LOGIN_TAIL}" for line in _PRIVATE_OR_LOGIN_LINES)

DIRECT_LINK_REFUSED_VARIANTS = (
    "❌ האתר סגר את השער בפני.\n➡️ נסה קישור אחר.",
    "❌ האתר הזה חוסם הורדות ישירות.\n➡️ נסה קישור אחר.",
    "❌ נדרשת הרשאה כדי להוריד את הקובץ הזה, ואין לי אותה.\n➡️ נסה קישור אחר.",
)

DIRECT_LINK_GONE_VARIANTS = (
    "❌ הקובץ הזה כבר לא שם.\n➡️ נסה קישור אחר.",
    "❌ הקישור מוביל לשום מקום. הקובץ נמחק מהשרת.\n➡️ נסה קישור אחר.",
    "❌ הקובץ יצא לטיול ולא השאיר הודעה.\n➡️ נסה קישור אחר.",
)

DIRECT_LINK_SERVER_BUSY_VARIANTS = (
    "❌ האתר עמוס ולא מוכן לדבר איתי כרגע.\n➡️ נסה שוב בעוד מספר דקות.",
    "❌ השרת שלהם עמוס. גם אני לא אוהב לחכות בתור.\n➡️ נסה שוב בעוד מספר דקות.",
    "❌ האתר חוסם גישה זמנית. זה יעבור.\n➡️ נסה שוב בעוד מספר דקות.",
)

DIRECT_FILE_TOO_LARGE_VARIANTS = (
    "❌ הקובץ הזה כבד ממני: {size}. המקסימום שלי {max_size}.",
    "❌ {size} זה יותר מדי בשבילי. התקרה היא {max_size}.",
    "❌ {size} מול {max_size} — הקובץ הזה מנצח בגדול.",
)

_DOWNLOAD_FAILED_LINES = (
    "❌ ניסיתי. באמת ניסיתי. הקובץ ניצח הפעם.",
    "❌ ההורדה נכשלה. זה קורה, גם לי.",
    "❌ לא הצלחתי הפעם.",
)
_DOWNLOAD_FAILED_TAIL = "נסה שוב או שלח קישור אחר."
DOWNLOAD_FAILED_VARIANTS = tuple(f"{line} {_DOWNLOAD_FAILED_TAIL}" for line in _DOWNLOAD_FAILED_LINES)

_REQUEST_TIMEOUT_LINES = (
    "⏱️ רדפתי אחריו יותר מדי זמן, ויתרתי.",
    "⏱️ הבקשה עברה את מגבלת הזמן.",
    "⏱️ הקובץ הזה לוקח יותר זמן ממה שיש לי.",
)
_REQUEST_TIMEOUT_TAIL = "➡️ נסה שוב מאוחר יותר או הורד קובץ קטן יותר."
REQUEST_TIMEOUT_EXCEEDED_VARIANTS = tuple(f"{line}\n{_REQUEST_TIMEOUT_TAIL}" for line in _REQUEST_TIMEOUT_LINES)

PING_RESULT_VARIANTS = (
    "🟢 הבוט פעיל · זמן תגובה: {ms}ms",
    "🟢 חי, נוהם ומגיב ב-{ms}ms",
    "🟢 כאן, ערני, {ms}ms",
)

# Order matters only in that `resolve` scans by descending key length, so a
# longer canonical string is never shadowed by a shorter one that happens to
# be a prefix of it.
STAGE_VARIANTS: dict[str, tuple[str, ...]] = {
    DOWNLOADING: DOWNLOADING_VARIANTS,
    UPLOADING: UPLOADING_VARIANTS,
    PROCESSING: PROCESSING_VARIANTS,
    DOWNLOAD_FROM_CACHE: DOWNLOAD_FROM_CACHE_VARIANTS,
    DOWNLOAD_FAILED: DOWNLOAD_FAILED_VARIANTS,
    REQUEST_TIMEOUT_EXCEEDED: REQUEST_TIMEOUT_EXCEEDED_VARIANTS,
    UNSUPPORTED_URL: UNSUPPORTED_URL_VARIANTS,
    NOT_MEDIA_CONTENT: NOT_MEDIA_CONTENT_VARIANTS,
    DIRECT_LINK_REFUSED: DIRECT_LINK_REFUSED_VARIANTS,
    DIRECT_LINK_GONE: DIRECT_LINK_GONE_VARIANTS,
    DIRECT_LINK_SERVER_BUSY: DIRECT_LINK_SERVER_BUSY_VARIANTS,
    INSTAGRAM_NOT_FOUND: INSTAGRAM_NOT_FOUND_VARIANTS,
    INSTAGRAM_PRIVATE_OR_LOGIN: INSTAGRAM_PRIVATE_OR_LOGIN_VARIANTS,
    FACEBOOK_NOT_FOUND: FACEBOOK_NOT_FOUND_VARIANTS,
    FACEBOOK_PRIVATE_OR_LOGIN: FACEBOOK_PRIVATE_OR_LOGIN_VARIANTS,
}

_SORTED_CANONICALS = sorted(STAGE_VARIANTS, key=len, reverse=True)

# Every variant of every category retry.py treats as hopeless (no retry
# button), so a rendered variant still suppresses the retry button exactly
# like its canonical constant does today.
ALL_HOPELESS_VARIANTS: tuple[str, ...] = (
    UNSUPPORTED_URL_VARIANTS
    + NOT_MEDIA_CONTENT_VARIANTS
    + INSTAGRAM_NOT_FOUND_VARIANTS
    + INSTAGRAM_PRIVATE_OR_LOGIN_VARIANTS
    + DIRECT_LINK_REFUSED_VARIANTS
    + DIRECT_LINK_GONE_VARIANTS
)


def pick(variants: tuple[str, ...], key: str | None = None) -> str:
    """Deterministically pick a variant for `key` (never Python's built-in
    `hash()`, which is salted per process and would change on every restart)."""
    if not key:
        return random.choice(variants)
    return variants[zlib.crc32(key.encode()) % len(variants)]


def resolve(text: str, key: str | None) -> str:
    """Render `text` through its variant tuple if it's a known canonical
    status/error string (exact match, or a leading segment of a longer
    message such as `f"⬇️ {DOWNLOADING}\\n45%..."`). Anything else is
    returned unchanged - this is a boundary-only renderer, so canonical
    constants keep flowing unresolved into classification code."""
    if text in STAGE_VARIANTS:
        return pick(STAGE_VARIANTS[text], key)

    has_prefix = text.startswith("⬇️ ")
    body = text[len("⬇️ "):] if has_prefix else text

    for canonical in _SORTED_CANONICALS:
        if body.startswith(canonical):
            rendered = pick(STAGE_VARIANTS[canonical], key) + body[len(canonical):]
            return f"⬇️ {rendered}" if has_prefix else rendered

    return text


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
    size_str = human_size(file_size)
    max_str = human_size(max_size)
    if size_str == max_str:
        # Rounding can collapse a boundary case (e.g. one byte over the cap)
        # to the same display string as the cap itself; fall back to exact
        # byte counts so the two numbers are never printed identically.
        size_str = f"{int(file_size):,} bytes"
        max_str = f"{int(max_size):,} bytes"
    return DIRECT_FILE_TOO_LARGE.format(size=size_str, max_size=max_str)


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


def format_raw_split_notice(total_parts: int, filename: str) -> str:
    """Sent once after a non-video file too large for one message is split
    into raw byte chunks (upload.splitter._split_raw) - unlike a split
    video, these parts are not independently usable, so without this the
    user has `total_parts` document attachments and no idea they must be
    concatenated in order to get the original file back."""
    return (
        f"ℹ️ הקובץ “{filename}” גדול מהמותר בהודעה אחת, ולכן פוצל ל-{total_parts} חלקים.\n"
        "בניגוד לווידאו, לא ניתן לצפות בחלקים האלו בנפרד - יש להוריד את כולם ולחבר אותם "
        "**לפי הסדר** (חלק 1, חלק 2, ...) כדי לקבל את הקובץ המקורי."
    )


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
    is_audio: bool = False,
    from_cache: bool = False,
) -> str:
    """`DOWNLOAD_DONE` plus whichever of quality/format, duration, size, and
    total request time are actually known - never a guessed or placeholder
    value, and never a credits balance (that stays in /settings only, per
    the owner's standing decision - see SETTINGS_CREDITS)."""
    lines = [DOWNLOAD_DONE]
    if from_cache:
        lines.append(CACHE_DELIVERY_SUMMARY)
    if is_audio or quality_label == "MP3":
        lines.append("🎵 נשלח: MP3")
    elif quality_label:
        lines.append(f"🎬 נשלח: {quality_label}")

    detail_parts = []
    if duration_seconds:
        detail_parts.append(_duration_text(duration_seconds))
    if size_bytes:
        detail_parts.append(human_size(size_bytes))
    if detail_parts:
        lines.append(f"📦 {' · '.join(detail_parts)}")

    if elapsed_seconds is not None and elapsed_seconds >= 0:
        if from_cache:
            lines.append(f"⏱️ נמסר ב-{_elapsed_text(elapsed_seconds)}")
        else:
            lines.append(f"⏱️ הושלם ב-{_elapsed_text(elapsed_seconds)}")

    return "\n".join(lines)


# --- Error report to the archive/error channel (telegram/error_report.py) ---

ERROR_REPORT_PLATFORM_HE = {
    "youtube": "יוטיוב",
    "tiktok": "טיקטוק",
    "instagram": "אינסטגרם",
    "facebook": "פייסבוק",
    "direct": "קישור ישיר",
}

ERROR_REPORT_QUALITY_NONE = "-"

ERROR_REPORT_HEADER = "❌ <b>דיווח שגיאה</b> #{platform} #{kind}"
ERROR_REPORT_BODY = (
    "👤 משתמש: {user_display}\n"
    "🆔 <code>{user_id}</code>\n"
    "🧭 מסלול: {platform_he} · איכות: {quality}\n"
    "<blockquote expandable>🔗 קישור: {url}</blockquote>\n"
    "⚠️ הודעה למשתמש: {user_text}\n"
    "🔧 חריגה: <code>{exc_type}: {exc_text}</code>\n"
    "⏱️ משך: {elapsed} · 🕒 {timestamp}"
)
ERROR_REPORT_LOG_SECTION = "<blockquote expandable>📋 לוג מפורט:\n<pre>{log_tail}</pre></blockquote>"
ERROR_REPORT_NO_LOG = "אין לוג זמין"
ERROR_REPORT_TRUNCATED_NOTICE = "... (נקטעו {n} שורות ראשונות, הלוג המלא בקובץ המצורף)"
ERROR_REPORT_TRUNCATED_NOTICE_NO_ATTACHMENT = (
    "... (נקטעו {n} שורות ראשונות, הקובץ המצורף דולג בגלל הגבלת קצב)"
)
ERROR_REPORT_RATE_LIMITED_NOTICE = "ℹ️ דולגו {n} דיווחים בגלל הגבלת קצב"
ERROR_REPORT_FILE_CAPTION = "📎 לוג מלא · <code>{user_id}</code>"


def format_failure_summary(attempts: list[tuple[str, str]]) -> str:
    if not attempts:
        return DOWNLOAD_FAILED
    lines = [DOWNLOAD_FAILED, "פירוט הניסיונות:"]
    for route_name, reason in attempts:
        lines.append(f"• {route_name}: {reason}")
    return "\n".join(lines)
