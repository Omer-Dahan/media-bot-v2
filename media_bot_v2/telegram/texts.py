"""Hebrew user-facing text.

Ported from the old bot's BotText (src/config/constant.py), verbatim where
the content applies to v1's command set. Trimmed of references to
out-of-v1-scope commands (/buy, /spdl, /torrent, /adminpanel) and the
JDownloader fallback mention - advertising commands or fallbacks that don't
exist in this bot would just confuse users (spec/SPEC.md locked decision 4).

ABOUT is kept verbatim, including the @BennyThink/@YD_IL attribution -
spec/SPEC.md open question 3 (keep/update/drop) is still unanswered by the
user as of this milestone.
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

ABOUT = (
    "בוט הורדות יוטיוב נבנה על ידי @BennyThink.\n"
    "שופר רבות ומופעל על ידי @YD_IL\n\n"
    "קוד פתוח ב-GitHub: https://github.com/tgbot-collection/ytdlbot"
)

SETTINGS = """
⚙️ **הגדרות הורדה (YouTube)**
בחר איכות וידאו וצורת שליחה.

🎥 **איכות:** 1080p | 720p | 480p
📤 **שליחה:** וידאו (צפייה ישירה) | קובץ (לא מתנגן)
📝 **כתוביות:** פעיל / כבוי
📑 **אורך תיאור:** 100-1000 | 4000 (נפרד) | ללא הגבלה 🔗
"""

YOUTUBE_QUALITY_SELECT = """
🎬 **בחר איכות להורדה**

📹 **{title}**
⏱️ משך: {duration}

👇 בחר את האיכות הרצויה:
"""

YOUTUBE_NOT_YET_IMPLEMENTED = (
    "🚧 הורדת YouTube תיתמך בשלב הבא של הפיתוח (M2).\nבינתיים אפשר לשלוח קישור הורדה ישירה."
)
TIKTOK_NOT_YET_IMPLEMENTED = "🚧 הורדת TikTok תיתמך בשלב הבא של הפיתוח (M3)."
INSTAGRAM_NOT_YET_IMPLEMENTED = "🚧 הורדת Instagram תיתמך בשלב הבא של הפיתוח (M3)."

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
