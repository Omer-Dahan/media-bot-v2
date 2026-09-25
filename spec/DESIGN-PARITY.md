> **סטטוס (2026-09-25):** התוכנית אושרה לביצוע על ידי הבעלים. מיושמת בשלבים (M7a, M7b, ...).
> - M7a שלב 0: שמירת התוכנית בריפו - בוצע (2026-09-25).
> - M7a שלבים 1-3 (Forwarded, סגנון שליחה, הגדרות בפועל): בוצעו ב-2026-09-25. פירוט בסעיף "התקדמות M7a" בסוף המסמך.
> - שלבים 4 ומעלה (התקדמות/ביטול, אלבומים, Telegraph, טקסטים שנותרו): טרם בוצעו.
>
> **החלטות בעלים שנסגרו (גוברות על ההמלצות בסעיף 6):** Q1 לא לשים קרדיטים בחתימה (רק במסך ההגדרות); Q2 כפתור קשר `t.me/YD_IL`; Q3 בלי `/stats` למשתמשים; Q5 צ'אט פרטי בלבד; Q9 "שליחה: קובץ" חל גם על יוטיוב; Q10 כותרת ומשך אמיתיים בתפריט האיכות; בחירת איכות עורכת את הודעת התפריט; `/adminpanel`, `/buy`, `/torrent`, `/spdl` מחוץ להיקף.

---

# M7 — שחזור העיצוב, הטקסטים וסגנון השליחה של הבוט הישן ב-media-bot-v2

> מסמך תכנון בלבד. לא נכתב ולא שונה קוד בשום ריפו.
> מקור אמת למראה: `/home/vm/projects/media-downloader-bot` (להלן **ישן**, נתיבים יחסיים ל-`src/`).
> יעד: `/home/vm/projects/media-bot-v2` (להלן **חדש**, נתיבים יחסיים ל-`media_bot_v2/` אלא אם צוין אחרת).
> מצב בסיס של החדש בזמן הכתיבה (2026-09-25, commit `f9b07e2`): `pytest` → 368 passed, 2 skipped; `ruff check` → נקי; `git status` נקי.

מקרא סטטוס: ✅ קיים · 🟡 חלקי · ❌ חסר · ➕ קיים רק בחדש.
מקרא החלטה: **שחזר** (כמו שהוא) · **התאם** (אותו מראה, מימוש/פרט שונה) · **דלג** (עם נימוק).

---

## 0. תקציר

- הטקסטים הסטטיים (`/start`, `/help`, `/about`, `/settings`, מקלדת ההגדרות ותפריט האיכות) כבר הועברו כמעט אחד לאחד. **הפער הגדול הוא בסגנון השליחה.**
- `uploader.py:32-33` שולח היום `send_file(chat, path, caption=title)` בלבד: בלי `supports_streaming`, בלי `duration`/`width`/`height`, בלי תמונה ממוזערת, בלי HTML, בלי מצב "קובץ", בלי אלבום ובלי כתוביות. `hachoir` לא מותקן, אז Telethon לא יכול לחלץ את המטא-דאטה של הווידאו בעצמו. התוצאה: וידאו בלי משך ובלי תמונה ממוזערת, ולפעמים הוא לא מתנגן בתוך הצ'אט.
- הגדרות המשתמש (פורמט, כתוביות, אורך תיאור) **נשמרות ב-DB אבל אף אחת מהן לא נקראת** ב-pipeline או במנועים. `settings_menu.py` משנה ערכים, ו-`router.py`/`pipeline.py` לא משתמשים בהם.
- הודעת ההתקדמות רזה: אין פס ירחים, אין סימני RTL, אין כפתור "❌ ביטול" ואין התקדמות העלאה. בסוף היא נשארת כ-"הושלם ✅" במקום להימחק.
- **באג חזותי חמור:** הארכוב ושליחה חוזרת מהמטמון עושים `forward_messages` (`uploader.py:38`, `:42`). משתמש שמקבל קובץ מהמטמון רואה "Forwarded from <ערוץ הארכיון>", כלומר שם ערוץ הארכיון דולף. בישן זה נעשה ב-`copy_message`/file_id, בלי כותרת העברה.

סה"כ **71 אלמנטים** במלאי: A=9, B=16, C=12, D=7, E=23, F=4.

---

## 1. מלאי המשטח של הבוט הישן ומיפוי לחדש

### A. פקודות וטקסטים קבועים (9)

| # | אלמנט | ישן | חדש | סטטוס | החלטה ונימוק |
|---|---|---|---|---|---|
| A1 | `/start` | טקסט `config/constant.py:8-22`; שליחה `main.py:207-224` עם `disable_web_page_preview=True` ו-`reply_markup=ReplyKeyboardRemove()` + chat action TYPING | `telegram/texts.py:16-31`, `router.py:120-127` (`link_preview=False`) | 🟡 | **התאם:** הטקסט כבר הותאם בהחלטת הבעלים (אתרים נתמכים ושורת `/settings`). צריך להוסיף `buttons=Button.clear()`, המקבילה ל-ReplyKeyboardRemove, כדי לנקות מקלדות ישנות אצל משתמשים קיימים. את ה-TYPING אפשר לדלג (לא נראה בפועל). |
| A2 | `/help` + כפתור | `constant.py:24-61`, `main.py:227-237` כפתור URL `"לצ'אט איתי 💬"` → `https://t.me/YD_IL` | `texts.py:33-58`, `router.py:129-135` | ✅ | **שחזר (קיים).** הרשימה קוצרה בכוונה: בלי `/buy`, `/spdl`, `/torrent`, `/stats`, `/direct` ו-JDownloader. |
| A3 | `/about` | `constant.py:64`, כולל ייחוס ליוצר המקורי | `texts.py:60-69`, `router.py:137-139` | ✅ (שונה בכוונה) | **דלג על הייחוס** לבקשת הבעלים (SPEC §7 שאלה 3 נסגרה). להשאיר את הטקסט החדש. |
| A4 | `/settings` טקסט | `constant.py:66-74`, `main.py:354-361` | `texts.py:71-79`, `router.py:148-156` | ✅ זהה | **שחזר (קיים).** |
| A5 | טקסט בחירת איכות | `constant.py:76-83`; ממולא בכותרת ובמשך אמיתיים מ-`get_youtube_video_info` (`main.py:663-670`, `engine/generic.py:609-642`), ואם נכשל: `"סרטון יוטיוב"` / `"לא ידוע"` | `texts.py:81-88`; `router.py:283` ממלא **תמיד** placeholders | 🟡 | **שחזר**, עם מגבלת זמן: חילוץ `extract_info(download=False)` בתקציב קצר (למשל 8 שניות), ואם נכשל או חורג, ה-placeholders הקיימים. זו לא "ניסיון הורדה" ולא נכנסת ל-RouteAttemptTracker. ראה סיכון R7. |
| A6 | `/ping` | `main.py:248-269`: "בודק פינג...", אחר כך reply מצוטט "פינג: X מילישניות", עריכה ל-"בדיקת הפינג הושלמה." ומחיקה | `router.py:141-146` עורך במקום | 🟡 | **דלג:** אותו מידע וטקסט זהה (`texts.py:114-115`). הריקוד של שלוש ההודעות לא מוסיף ערך. |
| A7 | `/stats` (למשתמש: דיסק וזמן פעילות) | `main.py:336-351`, `utils/__init__.py:108-135` | אין | ❌ | **החלטת בעלים (Q3).** המלצה: לדלג. `HELP` החדש כבר לא מפרסם את הפקודה, ובעיקר מדובר במידע של מפעיל, שמתאים יותר ל-`/adminpanel` העתידי. |
| A8 | "הבוט הזה פרטי ולא זמין עבורך." (AUTHORIZED_USER) | `main.py:181-204` | אין הגדרה כזו ב-`config.py` | ❌ | **החלטת בעלים (Q4).** אם AUTHORIZED_USER ריק בפרודקשן, אין מה לשחזר. |
| A9 | התנהגות בקבוצות: בישן מתעלמים מקישורים בקבוצה חוץ מ-`/ytdl` | `main.py:186-190`, `:417-436` | `router.py:260` `events.NewMessage()` בלי פילטר פרטי, כך שהבוט יגיב לכל קישור בקבוצה | ❌ | **החלטת בעלים (Q5).** המלצה: להגביל ל-private (`events.NewMessage(func=lambda e: e.is_private)`) ולדלג על `/ytdl`. זה פער התנהגותי ולא עיצובי, אבל הוא נראה למשתמשים. |

### B. זרימה, התקדמות וסיום (16)

| # | אלמנט | ישן | חדש | סטטוס | החלטה |
|---|---|---|---|---|---|
| B1 | אישור קבלת קישור (לא יוטיוב) | `main.py:708-710`: reply מצוטט `"הקישור נקלט, מתחיל לעבוד עליו 📥"` | `texts.py:99` `"בקשת ההורדה התקבלה..."` דרך `event.respond` (`router.py:288`, `:329`, `:369`) | 🟡 | **שחזר** את הטקסט וה-reply (`event.reply`). |
| B2 | תפריט איכות כתגובה מצוטטת לקישור | `main.py:700-704` `quote=True` | `router.py:282` `event.respond` | 🟡 | **שחזר** (`event.reply`). |
| B3 | toast אחרי בחירת איכות | `main.py:1049-1054`: `"⏳ מתחיל הורדה באיכות {name}..."` / `"⏳ מתחיל להוריד שמע..."`; `quality_names` ב-`:1042-1048` | `router.py:196` `event.answer()` ריק | ❌ | **שחזר.** |
| B4 | הודעת התפריט נערכת ל-`"🔄 מוריד באיכות {name}..."` / `"🔄 מוריד שמע..."` ומשמשת כהודעת ההתקדמות | `main.py:1057-1065`, `:1086` (אותה `callback_query.message`) | `router.py:197-205`: מוחק כפתורים ושולח הודעה **חדשה** | ❌ | **שחזר:** `event.edit(text, buttons=None)`, ואז `MessageProgressReporter(await event.get_message())`. יוצא בדיוק הודעה אחת לכל הורדה. |
| B5 | קישור שפג תוקפו | `main.py:1034-1036` alert `"❌ הקישור פג תוקף, נא לשלוח שוב"` | `texts.py:107` טקסט אחר, alert | 🟡 | **שחזר** את הטקסט הישן. |
| B6 | התקדמות הורדה: פס ירחים | `engine/base.py:156-186` (תבנית עם `‏` RLM, `━` ×18, `📥 **מוריד...**`, `{bar} {pct}%`, `📊 X/Y`, `⚡ מהירות:`, `⏱️ זמן משוער:`), `helper.py:38-79` `moon_progress_bar`, throttle של 2 שניות ב-`base.py:258-291` | `engines/youtube.py:452-464` (טקסט פשוט), `engines/instagram.py:223`, TikTok דומה; ל-Direct **אין בכלל** התקדמות | 🟡 | **שחזר** בפורמט זהה. פונקציה אחת משותפת (`telegram/progress_format.py`) שכל המנועים קוראים לה. להוסיף התקדמות גם ל-`engines/direct.py` ול-`providers/downloader.py`. |
| B7 | התקדמות העלאה | `base.py:205-247` (`"מעלה..."`, מהירות מחושבת כל 0.5 שניות, ETA בשניות/דקות/שעות) | אין | ❌ | **שחזר** דרך `progress_callback` של Telethon. |
| B8 | סיום הורדה ישירה | `engine/direct.py:115-117` `"✅ ההורדה הושלמה ({mb:.1f} MB)\n⏳ מעלה לטלגרם..."` | `texts.py:101-102` כללי | ❌ | **שחזר** ל-Direct. |
| B9 | הודעות פיצול | `base.py:716` `"⏳ הקובץ גדול מדי לטלגרם, מפצל לחלקים..."`, `:746` `"⏳ מפצל חלק {i}/{n}..."`, `:798` ו-`:1002` `"⬆️ מעלה חלק {i}/{n}..."` / `"⬆️ מעלה חלק {i} מתוך {n}..."` | `pipeline.py:143` `"מעבד..."` בלבד | ❌ | **התאם:** בחדש הפיצול נעשה ב-ffmpeg segment בפעולה אחת (`upload/splitter.py:88-129`), אז אין "חלק i" בזמן הפיצול. מציגים את הראשונה לפני הפיצול, ואת "⬆️ מעלה חלק i/N..." ככותרת של התקדמות ההעלאה. |
| B10 | כפתור ביטול | `base.py:277-288` `"❌ ביטול"` → `cancel:{chat}:{msg}`; `main.py:1116-1130` toast `"🛑 מבטל..."`; הודעת סיום `"🛑 ההורדה בוטלה בהצלחה"` ב-`base.py:1267` | אין. `CancellationToken` קיים (`engines/base.py:22-55`) אבל רק לתקציב זמן | ❌ | **שחזר**, עם שיפור: רק מי שביקש יכול לבטל (בישן כל אחד יכול היה). |
| B11 | מחיקת הודעת ההתקדמות אחרי הצלחה ("הקובץ הוא האישור") | `base.py:1219-1223`, `:984-986` | `pipeline.py:205` עורך ל-`"הושלם ✅"` ומשאיר | ❌ | **שחזר** בהצלחה מלאה. **אבל** אם יש סיכום אמת שחייב להישאר (פלייליסט חלקי, חלקים שלא נשלחו), לערוך ולא למחוק (כלל M4). |
| B12 | הודעת קרדיטים זמנית | `base.py:1274-1296`: `"💳 קרדיטים נותרים: {n}"`, `disable_notification=True`, נמחקת אחרי 5 שניות | אין | ❌ | **שחזר**, עם `asyncio.create_task` + `sleep(5)` + delete. רק כש-VIP/קרדיטים פעילים (`get_total_credits` סופי). |
| B13 | עומס במקביל | `main.py:646-652` דחייה: `"❌ **יש לך יותר מדי פעולות פעילות במקביל.**\nאנא המתן..."` | `queue/limiter.py` + `texts.py:108` `"⏳ יש עומס הורדות כרגע, הבקשה שלך בתור..."` | ➕ | **דלג (להשאיר את החדש):** תור עדיף על דחייה, וזה כבר עבר M4. |
| B14 | הודעות aria2 / gallery-dl | `generic.py:804-806`, `:1125` | אין | — | **דלג:** המסלולים האלה לא קיימים בחדש (LESSONS §6). |
| B15 | כפתור "▶️ המשך הורדה" + הודעת רשת | `base.py:297-351`, `network_errors.py:128-140`, `main.py:1133-1200` | אין | ❌ | **דלג:** מתנגש ב-M4 (ניסיון אחד לכל מסלול ותקציב זמן), והישן בכל מקרה התחיל מאפס. המשתמש שולח שוב את הקישור. |
| B16 | סיכום פלייליסט שקוצץ / timeout | אין | `texts.py:119`, `:140-143`, `pipeline.py:192-205` | ➕ | **להשאיר** (הודעת סיכום אמת של M4). |

### C. הודעות שגיאה ומכסה (12)

| # | אלמנט | ישן | חדש | סטטוס | החלטה |
|---|---|---|---|---|---|
| C1 | שגיאה כללית | `main.py:508-511` `"❌ לא הצלחתי להוריד את הקישור הזה כרגע.\nנסה שוב עוד מעט או שלח קישור אחר."` | `texts.py:105` `"❌ ההורדה נכשלה. נסה שוב או שלח קישור אחר."` | 🟡 | **שחזר** את הניסוח הישן כשורה הראשונה. את פירוט הניסיונות (`texts.py:146-152`) להשאיר מתחתיה (כלל M4). |
| C2 | קידומת ❌ אחידה | `main.py:529-545` מוסיף `❌` אם חסר | `engines/youtube.py:405-418` מחזיר בלי ❌; `pipeline.py:241` מציג `str(exc)` | 🟡 | **שחזר:** נרמול במקום אחד (`texts.as_error(msg)`) ב-`pipeline.py` וב-`router.py`. |
| C3 | שגיאות יוטיוב מסווגות | `generic.py:488-589` | `engines/youtube.py:258-418` (מועתק כמעט מילה במילה) | ✅ | **קיים.** |
| C4 | נגמרו הקרדיטים + כפתור | `main.py:548-558`, `:1089-1102`: `"❌ **הקרדיטים שלך נגמרו.**\n\nלרכישת קרדיטים נוספים, צור קשר עם יוצר הבוט. 👇"` + כפתור URL `"💬 לרכישת קרדיטים"` → `t.me/YD_IL` | `credits/service.py:57` `"הקרדיטים שלך נגמרו."` בלבד; `router.py:251-252` | 🟡 | **שחזר** טקסט וכפתור (כפתור קשר, לא `/buy`). ראה Q2. |
| C5 | מגבלת רוחב פס | `database/model.py:252` | `credits/service.py:59-61`, זהה, **כולל "שלח /buy"** | 🟡 | **התאם:** `/buy` מחוץ להיקף. להחליף ל-"לרכישת חבילה ללא הגבלה צרו קשר 👇" + אותו כפתור קשר (Q2). |
| C6 | משתמש חסום | `model.py:245` | `credits/service.py:55` | ✅ | קיים (להוסיף ❌ דרך C2). |
| C7 | פלייליסט בלי קרדיטים | `main.py:494-502`, `:637-639` → הודעת C4 | `router.py:209-212` זורק אותה חריגה | 🟡 | ייפתר עם C4. |
| C8 | קובץ גדול מדי | `base.py:195-197` `"גודל הקובץ {X} גדול מדי (מקסימום 4GB)"` | `texts.py:117` (מדויק יותר) | ➕ | **להשאיר את החדש** (LESSONS §2, §5). |
| C9 | שגיאות אינסטגרם | `instagram.py:231-233` | `texts.py:90-97` | ✅ | קיים (מפורט יותר). |
| C10 | FloodWait: קובץ "אנא המתן.txt" + הודעה לבעלים | `main.py:728-741` | אין | — | **דלג:** מוזר, חוסם thread ושולח DM לבעלים. Telethon מטפל ב-FloodWait קצר בעצמו (`flood_sleep_threshold`). |
| C11 | קישור לא נתמך | אין (הישן שלח הכול ל-yt-dlp) | `texts.py:118` | ➕ | להשאיר. |
| C12 | חריגת תקציב זמן | אין | `texts.py:119` | ➕ | להשאיר. |

### D. מקלדות וכפתורים (7)

| # | כפתורים (תווית, סוג, סדר) | ישן | חדש | סטטוס | החלטה |
|---|---|---|---|---|---|
| D1 | `/help`: שורה אחת, URL `"לצ'אט איתי 💬"` | `main.py:232-234` | `router.py:134` | ✅ | קיים |
| D2 | הגדרות: 4 שורות, כפתור אחד בכל שורה, callback: `🎥 איכות: {1080p/720p/480p}` → `toggle_quality`; `📤 שליחה: {וידאו/קובץ}` → `toggle_format`; `📝 כתוביות: {פעיל/כבוי}` → `toggle_subtitles`; `📑 אורך תיאור: {n / ללא הגבלה 🔗}` → `toggle_title_len`. ההודעה נבנית מחדש ונערכת במקום (`edit_text(BotText.settings, reply_markup=…)`), ו-`MessageNotModified` נבלע | `main.py:854-1005` | `settings_menu.py:57-64`, `router.py:158-175` | ✅ | קיים. הערה: `FORMAT_DISPLAY` בחדש (`settings_menu.py:18`) חסר `"audio": "שמע"` (בישן `main.py:868`), אז משתמש עם `format='audio'` ב-DB יראה "וידאו". **שחזר** את המיפוי. |
| D3 | תשובות toggle: alert **רק** ל-4000 ול-0, toast לשאר (`"✅ אורך תיאור: {n} תווים"`) | `main.py:987-998` | `router.py:170` alert לכל toggle של אורך | 🟡 | **שחזר:** alert רק כשהערך 4000 או 0. |
| D4 | תפריט איכות: `[🎬 1080p HD][🎬 720p]` / `[🎬 480p][🎬 360p]` / `[🎵 שמע בלבד]`, callback `ytq:{q}:{hash8}` | `main.py:673-697` | `quality_menu.py:51-62` | ✅ | קיים, זהה. |
| D5 | `❌ ביטול`, callback `cancel:{chat}:{msg}`, על כל עריכת התקדמות | `base.py:277-288` | אין | ❌ | **שחזר** (שלב 6). |
| D6 | `▶️ המשך הורדה` | `base.py:338-346` | אין | ❌ | **דלג** (ראה B15). |
| D7 | `💬 לרכישת קרדיטים` (URL `t.me/YD_IL`) | `main.py:550-552`, `:1089-1097` | אין | ❌ | **שחזר** (Q2). |

### E. סגנון שליחה (23), הלב של M7

מקורות בישן: `engine/base.py` `send_something` (`:368-415`), `get_metadata` (`:417-526`), `_upload` (`:892-1224`), `_split_video_if_needed` (`:700-779`), `_upload_split_video` (`:781-890`), `_forward_to_archive` (`:576-698`), `generate_input_media` (`:47-65`).
בחדש: `telegram/uploader.py:32-42` ו-`pipeline.py:148-190` בלבד.

| # | אלמנט | בישן (פרמטרים מדויקים) | חדש | סטטוס | החלטה |
|---|---|---|---|---|---|
| E1 | שליחה כווידאו | `send_video(chat_id, video, caption, progress, parse_mode=HTML, supports_streaming=True, duration, width, height, thumb)` (`base.py:399-415`, `:1060-1082`) | `send_file(chat, path, caption)`; אין `supports_streaming`, אין attributes, ו-`hachoir` לא מותקן | ❌ | **שחזר:** `send_file(..., supports_streaming=True, attributes=[DocumentAttributeVideo(duration, w, h, supports_streaming=True)], thumb=..., parse_mode='html', force_document=False)`. |
| E2 | תמונה ממוזערת | ffmpeg, פריים באמצע (`ss=duration/2`), scale ל-300 בצד הארוך, PNG, ולוודא שהקובץ גדול מ-100 בתים ואחרת `None` (`base.py:448-469`) | אין | ❌ | **שחזר** (JPEG ≤320px ו-≤200KB, כדרישת טלגרם. ב-PNG זה לעתים נדחה). |
| E3 | מצב "קובץ" | `send_document(..., force_document=True, thumb=...)` (`base.py:992-1033`) | אין | ❌ | **שחזר:** `force_document=True` + thumb. |
| E4 | שמע בלבד | yt-dlp `FFmpegExtractAudio mp3 192` (`generic.py:821-828`), ו-`send_audio` (`base.py:1042-1059`), עם fallback למסמך | `build_format_selector('audio')` = `"bestaudio/best"` (`engines/youtube.py:149-150`), בלי המרה, נשלח כקובץ גנרי | ❌ | **שחזר:** postprocessor ל-mp3 192, ו-`DocumentAttributeAudio(duration, title, performer)`. |
| E5 | שרשרת fallback: video → animation → audio → photo | `base.py:1060-1107` | אין | ❌ | **התאם:** ניסיון אחד כווידאו. אם טלגרם דוחה (`MediaInvalid` וכו'), ניסיון **אחד** כמסמך. בלי שרשרת של 4 (כלל M4: ניסיון אחד לכל מסלול). |
| E6 | אלבום לריבוי קבצים (קרוסלה, סליידשואו, פלייליסט) | `send_media_group`, וידאו עם `supports_streaming=True`, **חתימה על הפריט האחרון** (`base.py:47-65`, `:381-383`) | כל קובץ נשלח בנפרד (`pipeline.py:151-160`) | ❌ | **שחזר**, בקבוצות של עד 10 (מגבלת טלגרם, שהישן לא כיבד) והחתימה על הפריט האחרון בקבוצה האחרונה. לא במצב "קובץ" ולא לחלקים מפוצלים. |
| E7 | חתימת וידאו | `base.py:522`: `🎬 <b>{title}</b>\n\n🔗 מקור: {url}\n📐 רזולוציה: {w}x{h}\n⏱️ אורך: {m}:{ss} דקות\n{telegraph_line}\n⬇️ הקובץ מוכן לצפייה והורדה{credits_line}\nצפייה מהנה 👀✨` (title ו-url עוברים `html.escape`) | `caption = result.title` (`pipeline.py:152`) | ❌ | **שחזר** מילה במילה. |
| E8 | חתימת שמע | `base.py:520`: `🎵 <b>{title}</b>\n\n🔗 מקור: {url}\n⏱️ אורך: {dur}\n{telegraph_line}⬇️ הקובץ מוכן להורדה{credits_line}\nשמיעה מהנה 🎧✨` | אין | ❌ | **שחזר.** |
| E9 | קיצור כותרת לפי "אורך תיאור" | `base.py:477-492`: 100/250/500 כמו שהם, ו-0 / ≥1000 → 750; בלי כותרת: `Path.stem` של הקובץ | אין | ❌ | **שחזר**, ובנוסף אכיפת 1024 תווים לחתימה כולה (R1). |
| E10 | Telegraph (אורך = 0) | `helper.py:138-225` יוצר דף (iframe של יוטיוב, קישור מקור, תיאור) ומכניס ל-caption `🌐 <b>תיאור מלא ב-Telegraph:</b>\n\n{url}\n` (`base.py:499-511`) | אין | ❌ | **שחזר, מאחורי מתג config** (Q6): קריאה לשירות חיצוני בתקציב זמן (10+15 שניות בישן). אם נכשל, בלי השורה. הערה: ה-alert בהגדרות אומר "הקישור יישלח בהודעה נפרדת" אבל בישן הקישור נכנס **ל-caption**. נשמור את התנהגות הישן ונתקן את נוסח ה-alert רק באישור הבעלים. |
| E11 | הודעת תיאור מלא (אורך = 4000) | `base.py:1146-1179`: `📋 <b>תיאור מלא:</b>\n\n<blockquote expandable>{escape(title+desc)[:4000]}</blockquote>` כ-reply לקובץ | `DownloadResult.description` קיים (`engines/base.py:110`), אבל יוטיוב לא ממלא אותו (`engines/youtube.py:535-541`) | ❌ | **שחזר.** ב-Telethon ה-HTML parser צריך לתמוך ב-`<blockquote expandable>` (לבדוק בגרסה המותקנת). אם לא, `<blockquote>` רגיל. |
| E12 | תוויות חלקים בפיצול | `base.py:781-860`: כל חלק נשלח עם `📎 חלק {i}/{n}\n\n{full_caption}`, ואחרי כל חלק נוסף החלקים הקודמים נערכים (`edit_message_caption`) ל-`📎 חלק {k}/{n}` בלבד. בסוף רק האחרון נושא חתימה מלאה | `f"{title} ({i}/{n})"` (`pipeline.py:152`) | ❌ | **שחזר** את האלגוריתם. יתרון: גם אם חלק 3 נכשל, האחרון שנמסר נושא חתימה מלאה. |
| E13 | משך לכל חלק | `duration // num_parts` (`base.py:822-823`), וגם w, h ו-thumb לכל חלק | אין | ❌ | **התאם:** ffprobe לכל חלק בנפרד (מדויק יותר, כי ה-segment בחדש לא שווה-משך). thumb אחד מהמקור, שמופק **לפני** הפיצול (`splitter.py:45` מוחק את המקור). |
| E14 | חלקי מסמך | `base.py:997-1024`: חלקים קודמים `📎 חלק {i}/{n}: {filename}`, האחרון עם חתימה מלאה, `force_document=True` | כמו E12 | ❌ | **שחזר** לחלקים גולמיים (`splitter.py:156-169`, קבצי `.partNNN`), שנשלחים תמיד כמסמך. |
| E15 | כתוביות | yt-dlp `writesubtitles`, `writeautomaticsub`, שפות `en/en-orig/en-US/en-GB`, `srt` (`generic.py:776-789`), ואחרי המדיה `send_document` לכל קובץ עם `📝 כתוביות: {name}` (`base.py:1181-1193`). לא מחויב (רק סיומות מדיה נספרות, `:1195-1217`) | אין | ❌ | **שחזר** (שלבים 2 ו-5). |
| E16 | ארכוב: חתימה + העתקה בלי כותרת העברה | `copy_message` עם `👤 משתמש: {name @user}\n🆔 {id}\n📁 קובץ: {filename[:200]}\n<blockquote expandable>🔗 קישור: {url[:limit]}</blockquote>` (`base.py:600-622`, `:678-685`); לאלבום: `send_media_group` מ-file_id-ים והחתימה על האחרון (`:624-659`) | `forward_messages` בלי חתימה (`uploader.py:35-39`) | ❌ | **שחזר:** `send_file(archive, message.media, caption=archive_caption, parse_mode='html')`. זו שליחה חוזרת לפי file reference, בלי העלאה חוזרת ובלי כותרת "Forwarded". |
| E17 | ארכוב חלקים מפוצלים | `base.py:862-888`: כל חלק עם header משתמש/ID + החתימה המקורית של החלק | כל חלק עם forward | ❌ | **שחזר** עם E16. |
| E18 | שליחה מהמטמון | שליחה חוזרת לפי file_id + meta שמור, עם אותה חתימה (`base.py:1240-1247`, `:1113-1141`) | `forward_messages(user, ids, from_peer=archive)` (`uploader.py:41-42`): **כותרת "Forwarded from <ערוץ הארכיון>" נראית למשתמש**, והחתימה היא של הארכיון (שם ו-ID של משתמש **אחר**) | ❌ חמור | **שחזר:** `get_messages(archive, ids)` ואז `send_file(user, [m.media…], caption=<חתימת משתמש בנויה מחדש מ-meta>)`. meta במטמון יכלול `kind`, `duration`, `w`, `h`, `title`. **זה גם תיקון פרטיות.** |
| E19 | chat action בזמן העלאה | `send_chat_action(UPLOAD_VIDEO/…)` (`base.py:371-379`, `main.py:711`, `:1081`) | אין | ❌ | **שחזר:** `async with client.action(chat, 'video'/'audio'/'document')`, שמתחדש אוטומטית. |
| E20 | HTML ו-escape | `parse_mode=HTML` בכל שליחה, `html.escape` לכותרת ול-URL | ברירת המחדל של Telethon היא markdown, אז כותרת עם `*`/`_`/`[` תשתבש | ❌ | **שחזר:** `parse_mode='html'` + `html.escape` לכל ערך דינמי. |
| E21 | MKV מעל 2GB: פיצול ZIP | `base.py:938-966` | אין (`splitter.py:11-13`) | — | **דלג:** הוחלט ב-INVENTORY §4. ה-segment של ffmpeg שומר גם על אודיו ב-MKV. |
| E22 | שורת קרדיטים בחתימה | `base.py:494-497` (הערה: בפועל מופיעה רק בפגיעת מטמון ובפיצול, כי `_remaining_credits` נקבע לפני `get_metadata` רק שם) | אין | ❌ | **החלטת בעלים (Q1).** המלצה: לדלג. בחדש החיוב קורה **אחרי** ההעלאה (כלל M4), אז כל מספר בחתימה יהיה ניחוש. B12 מציגה את המספר האמיתי. |
| E23 | דיווח שגיאה לערוץ הארכיון | `main.py:112-167` (משתמש, קישור, שגיאה, לוג מפורט, קובץ לוג אם ארוך) | אין | ❌ | **החלטת בעלים (Q7):** זה משטח מפעיל ולא משתמש. המלצה: לדחות ל-`/adminpanel` העתידי, או לממש כשלב אופציונלי 10. |

### F. איך ההגדרות משפיעות בפועל (4)

| # | הגדרה | השפעה בפועל **בישן** (מאומת בקוד) | חדש | החלטה |
|---|---|---|---|---|
| F1 | איכות (high/medium/low = 1080/720/480) | רק במסלול יוטיוב **בלי** תפריט (`/ytdl` בקבוצה, resume): `generic.py:696-733` (`get_format(720/480)` = `height={m}` מדויק + fallback). בפרטי, התפריט דורס: `_selected_quality` (`generic.py:656-694`, `height<=X`). לאתרים אחרים אין השפעה (`generic.py:652-653` מחזיר `[None]`). האיכות **כן** נכנסת למפתח המטמון (`base.py:1229-1233`) | נשמרת ומוצגת בלבד (`settings_menu.py:69-71`); `YouTubeEngine` מקבל רק את איכות התפריט (`router.py:218-219`) | **שחזר כמו שהוא**, כלומר אין השפעה בפרטי. שיפור אופציונלי (Q8): לסמן ב-✅ את כפתור ברירת המחדל בתפריט. בלי המלצה לשנות התנהגות. |
| F2 | שליחה (וידאו/קובץ) | תקף ל-Direct ול-yt-dlp גנרי (`base.py:113`, `:992`). ביוטיוב מהתפריט **נדרס ל-video** (`generic.py:693`). באינסטגרם/טיקטוק/רדיט נקבע לפי סוג התוכן (`instagram.py:220-226`, `tiktok.py:283-304`). נכנס למפתח המטמון | לא נקרא | **התאם:** "קובץ" יחול על **כל** המנועים, כולל יוטיוב (מה שהמשתמש מצפה לו מ-`texts.SETTINGS` "קובץ (לא מתנגן)"). תמונות נשלחות תמיד כתמונה. חובה להוסיף את הפורמט למפתח המטמון (R6). **Q9**: לאשר מול הבעלים, כי זה שונה מהישן ביוטיוב. |
| F3 | כתוביות | yt-dlp מוריד en*, srt; נשלחות כמסמכים אחרי המדיה (E15). יוטיוב/גנרי בלבד | לא נקרא | **שחזר** ליוטיוב בלבד. אם אין כתוביות, שקט (כמו בישן). |
| F4 | אורך תיאור (100/250/500/1000/4000/0) | 100-500 = קיצור כותרת ב-caption; 1000 ו-0 → 750; 4000 → הודעת תיאור נפרדת (E11); 0 → דף Telegraph (E10). תיאור קיים רק ביוטיוב/גנרי | לא נקרא | **שחזר** (E9-E11). |

---

## 2. ארכיטקטורת היעד (תמצית)

- **`DeliveryOptions`** (dataclass חדש, `media_bot_v2/telegram/delivery.py`): `send_as: 'video'|'document'`, `subtitles: bool`, `title_length: int`, `quality_label: str|None`, `user_display: str`, `source_url: str`. נבנה ב-router מתוך `Setting` באותו `session_scope` שכבר קיים (`router.py:191-194`, `:275-278`) ומועבר ל-`pipeline.run(...)`.
- **`MediaInfo`** (`media_bot_v2/upload/media_probe.py`): `kind` (video/audio/photo/other), `duration`, `width`, `height`, `thumb_path`. מחושב ב-ffprobe/ffmpeg ב-`asyncio.to_thread`, בתוך תקציב ההעלאה.
- **`captions.py`** (`media_bot_v2/telegram/captions.py`): פונקציות טהורות: `build_user_caption(...)`, `build_part_label(i, n)`, `build_doc_part_caption(i, n, name)`, `build_archive_caption(...)`, `build_description_message(...)`, `fit_caption(caption, limit=1024)`.
- **`TelethonUploader`** מורחב: `send_media(path, info, caption, *, as_document, progress_cb)`, `send_album(items, caption)`, `edit_caption(msg, caption)`, `send_subtitle(path)`, `send_description(text, reply_to)`, `copy_to_archive(msgs, caption)` (במקום `forward_to_archive`), `resend_cached(archive_chat, ids, caption)` (במקום `send_cached`).
- **`progress_format.py`**: `moon_progress_bar`, `format_transfer(desc, done, total, speed, eta)` עם תבנית זהה ל-`base.py:179-185`, ו-`human_eta` (מאוחד מ-`engines/youtube.py:439-449`).
- **`CancelRegistry`** (in-process, TTL ו-cap כמו `QualitySelectionStore`): `(chat_id, msg_id) → (owner_id, CancellationToken)`.

---

## 3. תוכנית מימוש לפי סדר תלויות

כל שלב נסגר רק כש-`pytest` ירוק ו-`ruff check` נקי. כל הבדיקות רצות מול **client מדומה שמקליט קריאות** ובודקות את ה-kwargs שנשלחו בפועל, לא רק שהפונקציה רצה.

### שלב 0: תשתית בדיקה (FakeTelethonClient)
- **קבצים:** חדש `tests/fakes_telegram.py`. עדכון `tests/test_router.py` ו-`tests/test_pipeline.py` לשימוש בו (להחליף בהדרגה את ה-fakes הקיימים: `test_router.py:57-160`, `test_pipeline.py:81`).
- **תוכן:** `FakeClient` שמקליט `send_file`, `send_message`, `edit_message`, `delete_messages`, `get_messages`, `action` ו-`forward_messages` עם כל ה-args/kwargs; מחזיר `FakeMessage(id, media, caption)` עם id עוקב; ומאפשר להזריק חריגה לקריאה ה-N (בשביל כשל בחלק 2 מתוך 3). `FakeEvent`/`FakeCallbackEvent` עם `reply`, `respond`, `edit`, `answer(text, alert)`, `get_message`.
- **קבלה:** בדיקת עשן אחת שמוודאת את ההקלטה. בנוסף, `test_no_forward_messages_used` שמתחיל כ-`xfail` ויהפוך ל-pass בשלב 4.

### שלב 1: טקסטים וקבועים (בלי שינוי התנהגות)
- **קבצים:** `telegram/texts.py`, `telegram/settings_menu.py` (הוספת `"audio": "שמע"` ל-`FORMAT_DISPLAY`), `credits/service.py:59-61` (הסרת `/buy`, Q2).
- **טקסטים להוסיף ב-`texts.py`** (מילה במילה מהישן): `LINK_RECEIVED`, `QUALITY_NAMES`, `QUALITY_TOAST`, `QUALITY_TOAST_AUDIO`, `DOWNLOADING_QUALITY`, `DOWNLOADING_AUDIO`, `LINK_EXPIRED` (החלפה של `YOUTUBE_LINK_EXPIRED`), `GENERIC_ERROR`, `CREDITS_EXHAUSTED`, `CREDITS_BUTTON`, `CONTACT_URL`, `CANCEL_BUTTON`, `CANCELLING_TOAST`, `CANCELLED`, `SPLITTING`, `UPLOADING_PART`, `DIRECT_DONE`, `CREDITS_LEFT`, `SUBTITLE_CAPTION`, `DESCRIPTION_HEADER`, `TELEGRAPH_LINE`, ו-`as_error(msg)` (נרמול ❌).
- **בדיקות:** `tests/test_texts_parity.py`: לכל קבוע השוואת מחרוזת מדויקת לערך הישן, מוקלד בבדיקה עם הפניה `file:line` בהערה. `as_error` לא מכפיל ❌. אין `/buy`, `JDownloader`, `BennyThink` או `ytdlbot` באף טקסט (grep על המודול).

### שלב 2: מטא-דאטה מהמנועים (כותרת, תיאור, כתוביות, mp3)
- **קבצים:** `engines/youtube.py` (`_build_ydl_opts` `:692-731`: postprocessor mp3 192 כש-`quality=='audio'`; `writesubtitles`/`writeautomaticsub`/`subtitleslangs`/`subtitlesformat='srt'` כש-`subtitles=True`; `_result_from_info` `:510-541`: מילוי `description`, ו-`subtitle_paths` מ-`info['requested_subtitles'][*]['filepath']`), `engines/base.py:106-113` (`DownloadResult.subtitle_paths: list[str] = field(default_factory=list)`), `engines/instagram.py:255-272` ו-`engines/tiktok.py` (description כשקיים), `engines/direct.py:87` (title = שם הקובץ נשאר).
- **ממשק:** `YouTubeEngine(..., subtitles: bool = False)`.
- **בדיקות** (מבוססות על `tests/test_youtube_engine.py` הקיים עם yt-dlp מדומה): opts מכילים את ה-postprocessor **רק** ב-audio; opts מכילים את מפתחות הכתוביות **רק** כש-`subtitles=True`; `DownloadResult.description` ו-`subtitle_paths` מתמלאים מ-info מדומה; קבצי `.srt` **לא** נכללים ב-`file_paths`.

### שלב 3: probe, thumbnail ו-captions (פונקציות טהורות)
- **קבצים:** חדשים `upload/media_probe.py`, `telegram/captions.py`, `telegram/progress_format.py`.
- **פרטים:** `probe(path) → MediaInfo` (ffprobe JSON: streams ו-format). `make_thumb(path, duration) → Path|None` (פריים ב-`duration/2`, scale 320 בצד הארוך, JPEG; `None` אם פחות מ-100 בתים או שגיאה, כמו `base.py:460-469`). `fit_caption` מקצר **רק את הכותרת** עד שהטקסט אחרי `telethon.extensions.html.parse` נכנס ל-1024 יחידות UTF-16.
- **בדיקות:** `tests/test_captions.py`: snapshot מדויק של חתימת וידאו ושמע מול התבניות ב-`base.py:520-522` (כולל `\n` כפול, אימוג'ים וסדר שורות); escape של `<b>&"` בכותרת וב-URL; title_length 100/250/500 חותך בדיוק, 1000/0 → 750; כותרת של 5000 תווים עם URL ארוך נכנסת ל-≤1024 אחרי parse; תווית חלק `📎 חלק 2/3`; חתימת חלק מסמך `📎 חלק 1/3: name.bin`; חתימת ארכיון עם `<blockquote expandable>`. `tests/test_media_probe.py`: mock ל-subprocess שמאמת את פקודת ffmpeg/ffprobe, ובדיקת אינטגרציה אחת (skip אם אין ffmpeg) על קובץ mp4 קצר שנוצר ב-`ffmpeg -f lavfi`. `tests/test_progress_format.py`: `moon_progress_bar(0/37/50/100)` זהה לתוצאות הישן; התבנית כוללת `‏` ו-`━`×18.

### שלב 4: Uploader חדש (שליחה, העתקה לארכיון, מטמון בלי forward)
- **קבצים:** `telegram/uploader.py` (שכתוב), `cache/video_cache.py:71-98` (meta מורחב: `kind`, `duration`, `width`, `height`, `title`, `send_as`; תאימות לאחור לרשומות ישנות שיש בהן רק `title`), `pipeline.py:63-66` (Protocol `Uploader`).
- **פרמטרים שחייבים להישלח:**
  - וידאו: `send_file(chat, path, caption=…, parse_mode='html', supports_streaming=True, force_document=False, thumb=<jpg|None>, attributes=[DocumentAttributeVideo(duration=int, w=int, h=int, supports_streaming=True)], progress_callback=cb)`.
  - שמע: `attributes=[DocumentAttributeAudio(duration=int, title=…, performer=…)]`, `thumb`.
  - מסמך: `force_document=True`, `thumb` אם יש.
  - אלבום: `send_file(chat, [paths≤10], caption=['', …, caption_last], parse_mode='html', supports_streaming=True)`.
  - ארכיון: `send_file(archive, msg.media, caption=archive_caption, parse_mode='html')`; לאלבום: רשימת media.
  - מטמון: `get_messages(archive, ids=…)` ואז `send_file(user_chat, [m.media…], caption=<חתימת משתמש מ-meta>, parse_mode='html')`.
- **Fallback (E5):** אם `send_file` כווידאו נכשל ב-`MediaInvalidError`/`VideoContentTypeInvalidError`/`BadRequestError`, ניסיון אחד כמסמך. בלי שרשרת נוספת.
- **בדיקות** (`tests/test_uploader.py` מול FakeClient): כל ה-kwargs לעיל מאומתים בשמם ובערכם; `DocumentAttributeVideo.supports_streaming is True`; `forward_messages` **לא נקרא אף פעם** (הבדיקה מ-xfail בשלב 0 הופכת ל-pass); בשליחה מהמטמון החתימה מכילה את ה-URL של המבקש ו**לא** את `👤 משתמש:` של הארכיון; אלבום של 13 קבצים יוצא ל-2 קריאות (10+3) והחתימה רק על האחרון של הקריאה השנייה; fallback למסמך מתבצע פעם אחת בדיוק.

### שלב 5: שילוב ב-pipeline (הגדרות, חלקים, כתוביות, תיאור, מחיקה, קרדיטים)
- **קבצים:** `pipeline.py:85-251`, `telegram/router.py` (בניית `DeliveryOptions` ב-`:191-194`, `:275-278`; העברה ל-`pipeline.run`), `cache/video_cache.py:39` (`compute_cache_key(media_ref, quality, send_as)`), `router.py:235`, `:312`, `:352`, `:385`.
- **זרימה בתוך `run`:**
  1. probe ו-thumb למקור **לפני** `splitter.split_file` (שמוחק את המקור, `splitter.py:45`).
  2. אם צריך פיצול: `progress.update(SPLITTING)`.
  3. חלק יחיד: חתימה מלאה. N חלקים: כל חלק עם `label + "\n\n" + full`, ואחרי שחלק k נשלח, `edit_caption` לחלקים 1..k-1 שיכילו label בלבד. חלקים גולמיים (`.partNNN`): תמיד מסמך עם `📎 חלק i/N: name`, והאחרון עם חתימה מלאה.
  4. ריבוי קבצים (לא חלקים): אלבום (שלב 4), או ברצף כמסמכים אם `send_as='document'`.
  5. החיוב נשאר **לכל חלק שנמסר** (`pipeline.py:162-164`, כלל M4 / LESSONS §7).
  6. אחרי המדיה: כתוביות (אם `subtitles` ויש `subtitle_paths`), בלי חיוב, וכשל לא מפיל את ההורדה. הודעת תיאור (4000) כ-reply להודעת המדיה האחרונה, וכשל לא מפיל.
  7. העתקה לארכיון (E16/E17) ועדכון המטמון עם meta מורחב.
  8. הצלחה מלאה: `progress.delete()` + הודעת קרדיטים זמנית (B12). הצלחה חלקית (פלייליסט מקוצץ, או חלקים שנמסרו לפני כשל): **עריכה** לסיכום אמת, בלי מחיקה.
- **בדיקות** (`tests/test_pipeline_delivery.py`):
  - (א) 3 חלקי וידאו: הקריאות ל-`send_file` נושאות `📎 חלק i/3\n\n<full>`; אחרי הסוף `edit_message` נקרא ל-1 ול-2 עם label בלבד; לחלק 3 אין עריכה; לכל חלק `duration` מה-probe שלו ו-thumb של המקור.
  - (ב) כשל בחלק 2 מתוך 3: חלק 1 חויב (`use_quota_dynamic` עם הגודל שלו), חלק 3 לא נשלח, הודעת ההתקדמות **נערכה** לסיכום "נשלח 1 מתוך 3 חלקים" ולא נמחקה, ובחלק 1 החתימה המלאה נשארה (לא נערך ל-label).
  - (ג) `send_as='document'`: כל שליחה עם `force_document=True`.
  - (ד) `subtitles=True` + srt: קריאת `send_file` נוספת עם `caption='📝 כתוביות: x.en.srt'` **אחרי** המדיה, בלי חיוב נוסף. `subtitles=False`: אין קריאה כזו.
  - (ה) `title_length=4000`: `send_message` עם `reply_to=<id המדיה>` ו-`<blockquote`.
  - (ו) הצלחה: `delete` על הודעת ההתקדמות, `send_message(CREDITS_LEFT, silent=True)` ואחרי 5 שניות (זמן מדומה) delete.
  - (ז) מפתח מטמון שונה ל-video ול-document באותו URL.
  - (ח) כל בדיקות `tests/test_lessons.py` הקיימות עוברות בלי שינוי.

### שלב 6: התקדמות וביטול
- **קבצים:** `telegram/progress.py` (throttle 2 שניות מינימום כמו `base.py:269-270`; הצמדת `buttons=[[Button.inline('❌ ביטול', b'cancel:…')]]` בכל עריכה בזמן העבודה; הסרת הכפתור בהודעות סיום ושגיאה; `delete()`), `engines/youtube.py:452-464`, `engines/instagram.py:223`, `engines/tiktok.py` (שימוש ב-`progress_format.format_transfer`), `engines/direct.py` ו-`providers/downloader.py` (hook התקדמות חדש), `pipeline.py` (upload `progress_callback` מחושב כמו `base.py:213-247`), `telegram/router.py` (handler `cancel:` עם בדיקת בעלות), חדש `telegram/cancel_registry.py`.
- **כללים:** ביטול מפעיל את אותו `CancellationToken` שמשמש לתקציב. ביטול בזמן העלאה מתבצע ב-raise מתוך `progress_callback`. חלקים שכבר נמסרו נשארים וחויבו, ושום דבר מעבר לכך לא מחויב. ההודעה הסופית היא `🛑 ההורדה בוטלה בהצלחה`, ואם נמסרו חלקים נוסף אליה "נשלחו k מתוך N".
- **בדיקות:** עריכות מוגבלות ל-≤1 כל 2 שניות (שעון מדומה); כל עריכה בזמן העבודה נושאת את כפתור הביטול עם callback של הצ'אט וההודעה הנכונים; לחיצת משתמש אחר מקבלת answer ולא מבטלת; לחיצת הבעלים מפעילה את ה-token, לא מחייבת ומציגה `CANCELLED`; `format_transfer` זהה ל-snapshot של הישן.

### שלב 7: זרימת הודעות ב-router
- **קבצים:** `telegram/router.py`, `telegram/quality_menu.py` (ללא שינוי בכפתורים), `engines/youtube.py` (פונקציה `fetch_title_duration(url, timeout)` מבודדת).
- **שינויים:** B1/B2 `event.reply`; A5 חילוץ כותרת ומשך בתקציב, ובכישלון placeholders; B3 `event.answer(QUALITY_TOAST…)`; B4 עריכת הודעת התפריט עצמה ושימוש בה כהודעת התקדמות; B5 טקסט; C1/C2 `as_error`; C4/C7 הודעת קרדיטים עם `Button.url(CREDITS_BUTTON, CONTACT_URL)`; A1 `Button.clear()`; D3 alert רק ב-4000/0; A9 (אם יאושר) פילטר private.
- **בדיקות** (`tests/test_router.py`): לחיצה על `ytq:720:…` קוראת ל-`answer('⏳ מתחיל הורדה באיכות 720p...')` ול-`edit('🔄 מוריד באיכות 720p...', buttons=None)` על **אותה** הודעה, ו-`respond` לא נקרא; כשהחילוץ איטי מהתקציב, התפריט נשלח עם placeholders בזמן; נגמרו קרדיטים → טקסט + כפתור URL; `/start` שולח `buttons` מסוג clear.

### שלב 8: Telegraph (אופציונלי, תלוי Q6)
- **קבצים:** חדש `telegram/telegraph.py` (httpx/requests בתוך `to_thread`, timeout 10+15 שניות כמו `helper.py:194-216`), `captions.py` (שורת telegraph), `config.py` (`TELEGRAPH_ENABLED`, ברירת מחדל false).
- **בדיקות:** HTTP מדומה: הצלחה מוסיפה את השורה המדויקת `🌐 <b>תיאור מלא ב-Telegraph:</b>\n\n{url}\n`; כשל או timeout בלי השורה ובלי שגיאה למשתמש; כבוי = אין קריאת רשת בכלל.

### שלב 9: דיווח שגיאות לארכיון (אופציונלי, תלוי Q7)
- **קבצים:** חדש `telegram/error_report.py`, ו-`router.py` בנקודות ה-`except`.
- **בדיקות:** נשלח `send_message(archive, …, parse_mode='html', link_preview=False)` עם escape; לא נשלח כש-`archive_channel=None`; כשל בשליחה לא משפיע על הודעת המשתמש.

### שלב 10: תיעוד ובדיקת קבלה ידנית
- **קבצים:** `docs/LESSONS.md` (שורה למטמון/ארכיון בלי forward), `README.md`, `spec/SPEC.md` (סעיף M7).
- **קבלה ידנית (הבעלים, על טוקן בדיקה):** צילום מסך של כל אחד מאלה לצד הבוט הישן: וידאו יוטיוב 720p, שמע, מצב קובץ, עם כתוביות, אורך 4000, וידאו מעל 2GB (3 חלקים), קרוסלת אינסטגרם, קישור ישיר, ושליחה חוזרת מהמטמון (**בלי "Forwarded from"**).

---

## 4. סיכונים ומקרי קצה

- **R1: אורך חתימה.** מגבלת בוט: 1024 תווים **אחרי** parse של HTML, ביחידות UTF-16 (אימוג'י = 2). בישן חיתוך ל-750 פלוס URL ארוך יכול היה לחרוג (`CAPTION_TOO_LONG`). `fit_caption` מקצר כותרת בלבד, וכשה-URL לבדו ארוך מדי הוא מקוצר ל-`…` כמו `CAPTION_URL_LENGTH_LIMIT` בארכיון. בחלקים: label + `\n\n` + full, ואז fit שוב. בדיקה ייעודית בשלב 3.
- **R2: מספר חלקים.** `splitter.py:64` מכוון ל-70% מהמגבלה, כך שקובץ של 4GB יוצא ~3-4 חלקים, והיותר מהישן (1.9GB לחלק). מספר החלקים ידוע רק אחרי הפיצול, ולכן התוויות נבנות אחריו. חלק שנפל ל-raw fallback (`splitter.py:150-152`) לא מתנגן, ולכן נשלח כמסמך גם כשכל השאר וידאו. זה חייב להיות מטופל ומכוסה בבדיקה.
- **R3: עריכת חתימות של חלקים קודמים.** N עריכות לכל חלק, O(N²) בסך הכול, אבל N קטן. FloodWait על עריכה: לבלוע ולרשום בלוג. אסור שיפיל את ההורדה (אותו עיקרון כמו `progress.py:7-19`).
- **R4: כתוביות.** `writeautomaticsub` עלול להוריד שפות אוטומטיות רבות. להגביל ל-langs של הישן. כתוביות אינן מדיה ולא מחויבות. כשל בהורדת כתוביות (yt-dlp לפעמים זורק על subs) אסור שיפיל את ההורדה: `ignoreerrors` לשלב ה-subs, או `postprocessor` בנפרד. בדיקה.
- **R5: קבצים גדולים ותקציב זמן.** probe, thumb ו-mp3 מוסיפים זמן CPU. ה-probe וה-thumb נכנסים ל**תקציב ההעלאה** וההמרה ל-mp3 ל**תקציב ההורדה** (`pipeline.py:124-160`). כשל ב-thumb = `None` ולא שגיאה. העלאת 2GB עם `progress_callback` צריכה throttle, כדי שלא יהיו עריכות ברצף.
- **R6: מפתח מטמון.** הוספת `send_as` למפתח (`video_cache.py:39`) מבטלת פגיעות במטמון לרשומות קיימות, תופעה חד-פעמית. רשומות מטמון ישנות (עם `title` בלבד) צריכות להמשיך לעבוד: probe לא אפשרי, אז לשלוח עם חתימה בסיסית (כותרת ומקור) בלי רזולוציה ואורך. הסכמה המשותפת לא משתנה (`tests/test_models_match_old_schema.py`): רק תוכן ה-JSON ב-`meta`.
- **R7: כותרת בתפריט.** `extract_info` נוסף לפני בחירת איכות מוסיף בקשה ליוטיוב לכל קישור (סיכון לזיהוי בוט) וזמן המתנה. תקציב קשיח ו-placeholders. לא נרשם ב-health tracker ולא ב-RouteAttemptTracker. אפשר לכבות ב-config (Q10).
- **R8: כשל העלאה אחרי חיוב.** החיוב הוא לכל חלק שנמסר, כך שכשל בחלק k משאיר חיוב על 1..k-1 בלבד. הודעת הסיכום חייבת לומר זאת במפורש ("נשלחו k-1 מתוך N; חויבת רק על מה שנשלח"). במטמון נשמרים רק חלקים שנמסרו **במלואם**, ופגיעה במטמון עם סט חלקי **אסורה**: כרגע `pipeline.py:185-190` שומר כבר אחרי חלק 1. צריך לסמן `complete=True` ב-meta ולהגיש מהמטמון רק רשומות שלמות.
- **R9: M4, ניסיון אחד לכל מסלול.** fallback מווידאו למסמך הוא **ניסיון שליחה נוסף אחד**, לא מסלול הורדה, ולא חוזר. בלי שרשרת של 4 כמו בישן. `max_retries=0` ביוטיוב נשאר (`engines/youtube.py:563`).
- **R10: M4, תקציב זמן.** המחיקה של הודעת הקרדיטים אחרי 5 שניות היא `create_task` מחוץ ל-pipeline. חייבת להיות עמידה לכיבוי (לבלוע `CancelledError`). הודעת תיאור וכתוביות נכנסות לתקציב ההעלאה.
- **R11: M4, הודעת סיכום אמת.** מחיקת הודעת ההתקדמות רק בהצלחה מלאה. בכל מצב חלקי (פלייליסט מקוצץ, כשל בחלק, ביטול אחרי חלקים) ההודעה נשארת עם הסיכום. בדיקה ייעודית בשלב 5.
- **R12: פרטיות בארכיון ובמטמון.** המעבר מ-forward להעתקה סוגר דליפה של שם ערוץ הארכיון ושל פרטי משתמש אחר (שם, ID) שנכנסו לחתימת הארכיון. גם אחרי התיקון, בשליחה מהמטמון **אסור** להעתיק את חתימת הארכיון. חובה לבנות מחדש.
- **R13: `<blockquote expandable>` ב-Telethon.** לא בטוח שה-HTML parser בגרסה המותקנת תומך בו. לבדוק בשלב 3, ואם לא, `<blockquote>` רגיל (נראה כמעט זהה).
- **R14: thumb בפורמט PNG.** טלגרם מצפה ל-JPEG ≤200KB ו-≤320px. הישן שלח PNG ב-300px ולפעמים זה עבד. בחדש JPEG.

---

## 5. מחוץ להיקף (במפורש)

`/buy` (וכל אזכור שלו, כולל תיקון `credits/service.py:60`), `/torrent`, `/spdl`, `/direct` (זיהוי אוטומטי כבר קיים), `/adminpanel` (עתידי, לא בסבב הזה), כל אזכור JDownloader, ייחוס ליוצר המקורי (`BennyThink`/`ytdlbot`, הוסר לבקשת הבעלים), כפתור resume, aria2/gallery-dl, פיצול ZIP ל-MKV, והודעות FloodWait בקבצים.

---

## 6. החלטות שנדרשות מהבעלים לפני מימוש

- **Q1:** שורת "קרדיטים נותרים" בתוך החתימה (E22)? המלצה: לא. להסתפק בהודעה הזמנית של 5 שניות (B12).
- **Q2:** כפתור "💬 לרכישת קרדיטים" → `t.me/YD_IL` בהודעת נגמרו קרדיטים ובהודעת רוחב פס (במקום "שלח /buy")? המלצה: כן. זה כפתור קשר, לא `/buy`.
- **Q3:** `/stats` למשתמשים? המלצה: לא (לדחות ל-`/adminpanel`).
- **Q4:** האם AUTHORIZED_USER (בוט פרטי) בשימוש בפרודקשן?
- **Q5:** להגביל את הבוט לצ'אט פרטי כמו בישן? היום הוא יגיב לכל קישור בקבוצה. המלצה: כן.
- **Q6:** Telegraph ל"ללא הגבלה" (שירות חיצוני)? ואם כן, האם לתקן את נוסח ה-alert ("הקישור יישלח בהודעה נפרדת") להתנהגות בפועל (הקישור בתוך החתימה)?
- **Q7:** דיווח שגיאות מפורט לערוץ הארכיון (E23): עכשיו או עם `/adminpanel`?
- **Q8:** להשאיר את הגדרת האיכות בלי השפעה בפרטי (כמו בישן), או לסמן ב-✅ את כפתור ברירת המחדל בתפריט?
- **Q9:** "שליחה: קובץ" יחול גם על יוטיוב? בישן הוא נדרס ל-video ביוטיוב. המלצה: כן, זה מה שהמשתמש מצפה לו מהטקסט.
- **Q10:** כותרת ומשך אמיתיים בתפריט האיכות (בקשה נוספת ליוטיוב, עד ~8 שניות המתנה) או placeholders?


---

## התקדמות M7a (2026-09-25)

**בוצע (אלמנטים לפי המספור למעלה):**
- שלב 1 (Forwarded): E16, E17 (העתקה לארכיון עם כותרת מפעיל, לכל חלק), E18 (שליחה מהמטמון עם חתימה שנבנית למבקש), R12. `forward_messages` לא בשימוש בשום מקום.
- שלב 2 (סגנון שליחה): E1, E2, E3, E4 (המרת mp3 + מאפייני אודיו), E5 (ניסיון אחד כמסמך), E7, E8, E9 (כולל הגבלת 1024), E11, E12, E13, E14, E15, E20, R1, R2, R4, R13 (`<blockquote expandable>` נתמך בגרסה המותקנת).
- שלב 3 (הגדרות): F2 (כולל יוטיוב), F3, F4 (100-1000, 4000 כהודעה נפרדת), F1 (הגדרת האיכות מסמנת ✅ את כפתור ברירת המחדל בתפריט; הבחירה בתפריט גוברת, כמו בישן), R6 (מפתח המטמון כולל פורמט וכתוביות).
- החלטות בעלים: A5 (כותרת ומשך אמיתיים, תקציב 8 שניות), A9 (פרטי בלבד), B3, B4 (עריכת הודעת התפריט), C4, C5, D7 (כפתור קשר), D2 (מיפוי `audio`), E22 (לא נוסף, לפי החלטה).

**לא בוצע במכוון (נשאר לסבבים הבאים):** E6 אלבומים, E10 Telegraph (Q6 פתוח; באורך 0 הכותרת נחתכת ל-750 בלי קישור), E19 chat action, B6-B12 פס התקדמות/התקדמות העלאה/ביטול/מחיקת הודעת ההתקדמות/הודעת קרדיטים זמנית, A1, B1, B2, B5, C1, C2, D3, E21, E23.
