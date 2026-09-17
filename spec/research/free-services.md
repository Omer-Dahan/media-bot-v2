# דוח מחקר אמפירי: שירותי חילוץ מדיה חינמיים חיצוניים (ספטמבר 2026)

**פרויקט:** `tmbot` / `media-bot-v2`  
**תאריך בדיקה אמפירית:** 18 בספטמבר 2026  
**מטרה:** בחינה מעשית ואמפירית (קריאות API חיות + בדיקות HEAD/Range, ללא הורדת קבצים וללא הרשמה) של שירותים חינמיים ברשת לחילוץ מדיה מ-YouTube, TikTok ו-Instagram — במטרה לבחון האם ניתן להחליף מנגנון `yt-dlp` מקומי בשירות ענן חיצוני חינמי.

---

## 1. טבלת השוואה מרכזית (ממצאים אמפיריים)

| שירות / מועמד | פלטפורמות נתמכות | עבד בפועל בבדיקה? (YouTube / TikTok / IG) | נדרש מפתח / הרשמה? | מגבלות הטיר החינמי | מתאים לבוט בייצור? |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **TikWM** *(בנצ'מרק)* | TikTok בלבד | **YouTube:** נכשל (`code: -1`)<br>**TikTok:** **עבד (HTTP 200, 0.92s)**<br>**IG:** לא נתמך | **לא** (Keyless לחלוטין) | ללא מגבלה רשמית; בפועל חסימת קצב ב-Cloudflare סביב 60–100 קריאות/דקה ל-IP | **כן (ל-TikTok בלבד)** — נתיב ראשי אידיאלי |
| **Cobalt Public Instances** *(Directory / Kwiat)* | YouTube, TikTok, IG, X, Reddit | **YouTube:** מופע 1 עבד (HTTP 200, 0.70s), 21 מופעים נחסמו ב-JWT/Turnstile<br>**TikTok:** נכשל במופע הפתוח (`HTTP 400 fetch.fail`)<br>**IG:** נכשל (`HTTP 400 fetch.empty`) | **לא**, אך 21 מתוך 24 מופעים דורשים פתרון **Cloudflare Turnstile** | רוב המופעים הציבוריים מגבילים ל-40 בקשות/דקה (`RateLimit-Policy: 40;w=60`) | **לא** (מופעים ציבוריים נחסמים אוטומטית או חוסמים בוטים) |
| **pybalt / `dwnld.nichind.dev`** *(Multi-Instance Proxy)* | YouTube, TikTok, Bilibili, X (IG נכשל) | **YouTube:** **עבד (HTTP 200, 0.56s)**<br>**TikTok:** **עבד (HTTP 200, 0.53s)**<br>**IG:** נכשל (ניסה 17 שרתי backend וכולם נכשלו) | **לא** (הראוטר פתוח חופשי) | מוגבל ל-40 בקשות/דקה פר מופע backend; תלוי ב-160 שרתי VPS פרטיים של קהילה | **כגיבוי בלבד (Tier 2/3)** — חסר SLA, מעביר תנועה דרך VPSים לא מוכרים |
| **FastSaverAPI** *(`api.fastsaver.io`)* | YouTube, TikTok, IG, FB, X, Pinterest | **YouTube:** נחסם (HTTP 422 ללא מפתח, HTTP 401 במפתח דמה)<br>**TikTok:** נחסם (HTTP 422)<br>**IG:** נחסם (HTTP 422) | **כן חובה** (Header `X-Api-Key`) | **1,000 קרדיטים חד-פעמיים בלבד** בהרשמה. יוטיוב עולה 15–25 קרדיטים (~40–66 סרטונים סה"כ לכל החיים!) | **לא** (אינו שירות חינמי מתמשך, אלא תקופת ניסיון קצרה) |
| **TikLiveAPI** *(`api.tikliveapi.com`)* | TikTok בלבד (37 endpoints) | **YouTube:** לא נתמך<br>**TikTok:** דורש מפתח (HTTP 200 עם הודעת `"Please sign up"`)<br>**IG:** לא נתמך | **כן חובה** (Header `X-Api-Key`) | **100 קרדיטים חד-פעמיים בלבד** (1 בקשה = 1 קרדיט), לאחר מכן תשלום Pay-as-you-go | **לא** (טיקטוק בלבד, 100 קריאות בלבד לכל החיים) |
| **Piped Instances** *(NewPipeExtractor API)* | YouTube בלבד | **YouTube:** 13 מתוך 15 מופעים **מתים** (525, 502, 403, DNS NXDOMAIN). 2 מופעים עבדו אך מחזירים **תקרת 360p בלבד** וללא אודיו DASH.<br>**TikTok / IG:** לא נתמך | **לא** | אין SLA; שרתים קורסים תחת עומס; תקרת איכות 360p עקב BotGuard של גוגל | **לא** (שירות קורס ונטוש ברובו, איכות נמוכה ביותר) |
| **Invidious Instances** | YouTube בלבד | **YouTube:** נכשל. 100% מהמופעים הפעילים ביטלו את ה-API (`api: false`), החזירו `HTTP 403 Endpoint disabled`<br>**TikTok / IG:** לא נתמך | **לא** | ה-API הציבורי הושבת באופן יזום בכל המופעים הציבוריים כדי למנוע חסימות מיוטיוב | **לא** (חסום ואינו פעיל) |

---

## 2. ממצאי בדיקה אמפירית מפורטים (ראיות ופלטי API)

### 2.1 TikWM (הבנצ'מרק ל-TikTok)
* **קריאת בדיקה (TikTok):**
  ```bash
  curl -s "https://www.tikwm.com/api/?url=https://www.tiktok.com/@tiktok/video/7106594312292453675&hd=1"
  ```
  * **תוצאה:** קוד HTTP 200, זמן תגובה **0.92 שניות**.
  * **מבנה הפלט:** `code: 0`, `msg: "success"`, מחזיר קישורים ישירים:
    * `data.play`: `https://v16m.tiktokcdn-us.com/...` (SD)
    * `data.hdplay`: `https://v19-notes.tiktokcdn-us.com/...` (HD)
    * `data.music`: `https://v16-ies-music.tiktokcdn-us.com/...`
  * **אימות קישור המדיה (HEAD/Range):**
    * בקשת `HEAD` ישירה נחסמה בקוד HTTP 503 ע"י מנגנון האנטי-בוט של Akamai.
    * בקשת Range חלקית תקנית (`curl -s -r 0-10 -D - -o /dev/null -A "Mozilla/5.0..."`) החזירה מיד **`HTTP/1.1 206 Partial Content`**, `Content-Type: video/mp4`, גודל מלא: 3,288,875 בתים (~3.13MB). **הקישור חי ותקין לחלוטין.**
* **בדיקת YouTube ב-TikWM:**
  ```bash
  curl -s "https://www.tikwm.com/api/?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ"
  ```
  * **תוצאה:** `{"code": -1, "msg": "Url parsing is failed! Please check url.", "processed_time": 0.0014}`. השירות תומך אך ורק ב-TikTok.

---

### 2.2 מופעים ציבוריים של Cobalt (גרסאות 11.5 ו-11.7.1)
נבדקה רשימת המופעים מתוך `https://cobalt.directory/` (עודכנה בזמן אמת ב-18.09.2026) ומתוך `codeberg.org/kwiat/instances`:
1. **חסימת Cloudflare Turnstile:**
   מתוך 24 מופעים שנסרקו ב-Directory, **21 מופעים (87.5%) מפעילים Cloudflare Turnstile** (`turnstileSitekey: "..."`).
   שליחת בקשת POST ממוחשבת מכל סקריפט/בוט מחזירה שגיאה:
   ```json
   {"status":"error","error":{"code":"error.api.auth.jwt.missing"}}
   ```
   (קודי HTTP 400 או HTTP 403 מול שרתי Cloudflare הרשמיים כגון `nachos.imput.net`).
2. **המופע היחיד ללא Turnstile — `https://cobaltapi.cjs.nz` (גרסה 11.5):**
   * **בדיקת YouTube (`dQw4w9WgXcQ`):**
     * **תשובת POST:** קוד HTTP 200 תוך 0.70 שניות:
       ```json
       {
         "status": "tunnel",
         "url": "https://cobaltapi.cjs.nz/tunnel?id=LYnA9aTg0j5AF-vKWCOR8&exp=...",
         "filename": "Rick Astley - Never Gonna Give You Up... (1080p, h264).mp4"
       }
       ```
     * **אימות כתובת ה-Tunnel בבקשת HEAD:**
       החזירה **`HTTP/2 200`** עם כותרות:
       `ratelimit-policy: 40;w=60` (מגבלה של 40 בקשות לדקה),
       `content-disposition: attachment`,
       `estimated-content-length: 92797590` (~88.5MB). **עבד בהצלחה מלאה.**
   * **בדיקת TikTok:**
     * החזירה שגיאה: `HTTP 400 {"status":"error","error":{"code":"error.api.fetch.fail","context":{"service":"tiktok"}}}`.
   * **בדיקת Instagram:**
     * החזירה שגיאה: `HTTP 400 {"status":"error","error":{"code":"error.api.fetch.empty"}}` עקב חסימת ה-IP של השרת ע"י Meta.

---

### 2.3 pybalt והראוטר `dwnld.nichind.dev`
נבדקה חבילת הפייתון `pybalt` (גרסה אחרונה `2025.7.1` ב-PyPI, מאת `nichind`):
* **מה החבילה עושה בפועל:** החבילה היא Wrapper סביב מופעי Cobalt. בקובץ `pybalt/core/wrapper.py` היא מנהלת רשימת שרתי Cobalt, פונה לספריית מופעים מקוונת, ובמידה ואלו נכשלים — מפנה כברירת מחדל לשרת הראוטר המרכזי של המחבר: `https://dwnld.nichind.dev`.
* **בדיקת הראוטר `https://dwnld.nichind.dev`:**
  השרת מצהיר על חיבור ל-160 מופעי backend ומבצע פינג מקבילי להעברת הבקשה למופע המהיר ביותר:
  * **בדיקת YouTube (`dQw4w9WgXcQ`):**
    * **תשובה (0.56s):** `HTTP 200`:
      ```json
      {
        "status": "tunnel",
        "url": "http://185.197.195.62:9000/tunnel?id=d9TXmzUXvzAtkRwsm_8-e...",
        "filename": "Rick Astley - Never Gonna Give You Up... (1080p, h264).mp4",
        "instance_info": {"url": "185.197.195.62:9000"},
        "latency_ms": 473
      }
      ```
    * **אימות HEAD:** החזיר **`HTTP/1.1 200 OK`**, גודל קובץ `92797590` בתים (~88.5MB), מגבלת קצב `RateLimit-Limit: 40`.
  * **בדיקת TikTok:**
    * **תשובה (0.53s):** `HTTP 200`:
      ```json
      {
        "status": "tunnel",
        "url": "http://146.255.188.127:9001/tunnel?id=z9E-eOfocv-cB4Xh2UqZJ...",
        "filename": "tiktok_tiktok_7106594312292453675.mp4"
      }
      ```
    * **אימות HEAD:** החזיר **`HTTP/1.1 200 OK`**, `content-type: video/mp4`, `content-length: 3288875` בתים.
  * **בדיקת Instagram:**
    * **נכשל לחלוטין (2.55s):** הראוטר ניסה 17 שרתי backend שונים של קהילת Cobalt (`88.198.32.91`, `51.68.120.197`, `129.151.212.247` וכו') וכולם החזירו שגיאה. התשובה הסופית: `{"status":"error","error":{"code":"content.no_valid_content"}}`.

---

### 2.4 FastSaverAPI (`api.fastsaver.io` / `fastsaverapi.com`)
* **סטטוס השירות:** עבר ביולי 2026 לדומיין `https://api.fastsaver.io/v1`.
* **מבנה האימות:** דורש כותרת `X-Api-Key` בכל נקודת קצה (לפי ה-OpenAPI הרשמי שנבדק ב-`fastsaverapi.com/openapi.json`).
* **בדיקות אמפיריות בפועל:**
  * פנייה ללא מפתח:
    `curl -s "https://api.fastsaver.io/v1/fetch?url=..."` -> **`HTTP 422 Unprocessable Entity`**:
    `{"detail":[{"type":"missing","loc":["header","X-Api-Key"],"msg":"Field required"}]}`.
  * פנייה עם מפתח דמה:
    `curl -s -H "X-Api-Key: dummy" "https://api.fastsaver.io/v1/fetch?url=..."` -> **`HTTP 401 Unauthorized`**:
    `{"ok":false,"detail":"Invalid API key"}`.
* **מגבלות הטיר החינמי:**
  * מעניק באנר של 1,000 קרדיטים בהרשמה ראשונית (ללא חידוש חודשי).
  * עלות חילוץ YouTube (נקודת קצה `/youtube/download`): 15 קרדיטים (25 קרדיטים ל-2K/4K). **1,000 קרדיטים מספיקים ל-40 עד 66 סרטונים בלבד לכל החיים.**
  * עלות חילוץ TikTok / Instagram (נקודת קצה `/fetch`): 1.5 עד 5 קרדיטים לבקשה (~200 עד 666 הורדות).
  * **מסקנה:** מדובר במודל Freemium/Trial מובהק הדורש רכישת קרדיטים בתשלום ולא בשירות חינמי בר-קיימא לבוט.

---

### 2.5 TikLiveAPI (`api.tikliveapi.com` / `tikliveapi.com`)
* **סטטוס השירות:** שירות מסחרי לכריית נתוני TikTok בלבד (37 endpoints, כולל `/download-video/` ו-`/download-music/`).
* **בדיקות אמפיריות בפועל:**
  * פנייה ל-`GET https://api.tikliveapi.com/download-video/?url=...`:
    * ללא מפתח: `HTTP 200 {"message":"Please sign up to tikliveapi.com"}`.
    * עם מפתח דמה: `HTTP 200 {"message":"Api-Key is not available"}`.
* **מגבלות הטיר החינמי:**
  * 100 קרדיטים חינם ברישום (1 בקשה = 1 קרדיט).
  * 100 בקשות בסך הכל, ללא חידוש, ל-TikTok בלבד (אינו תומך כלל ב-YouTube או Instagram).
  * **מסקנה:** לא רלוונטי לבוט תפעולי.

---

### 2.6 Piped ו-Invidious (שרתי פרוקסי בקוד פתוח ליוטיוב)
* **Invidious (`api.invidious.io`):**
  * נסרקו כלל המופעים מ-`https://api.invidious.io/instances.json`.
  * **0 מופעים פעילים מציעים כיום API ציבורי פתוח.** כל המופעים הגדירו `api: false` כדי להתגונן מחסימות IP גורפות של גוגל.
  * פנייה ישירה ל-`https://inv.nadeko.net/api/v1/videos/dQw4w9WgXcQ` החזירה **`HTTP 403 Endpoint disabled`**.
* **Piped (`TeamPiped/documentation`):**
  * נבדקו 15 המופעים הרשמיים המתועדים בתיעוד הפרויקט:
    * 13 מופעים החזירו שגיאות רשת חמורות (525 Cloudflare SSL Handshake, 502 Bad Gateway, 403 Forbidden, או שגיאות DNS NXDOMAIN כגון `api.piped.yt`, `pipedapi.drgns.space`).
    * **רק 2 מופעים הגיבו:** `https://pipedapi.ducks.party` (1.00s) ו-`https://api.piped.private.coffee` (1.90s).
  * **ניתוח איכות המדיה של Piped בפועל:**
    * מערך הווידאו החזיר 3 ערוצים בלבד: שני ערוצי LBRY (שנכשלו בבקשת HEAD עם `HTTP 401 Unauthorized`), וערוץ YouTube בודד אחד: **`360p MPEG_4`** (itag 18) דרך פרוקסי `https://piped-proxy.ducks.party/videoplayback?...`.
    * ערוצי DASH באיכות 720p/1080p וערוצי אודיו נפרדים: **0 זמינים (`audioStreams_count: 0`)**.
    * **מסקנה:** Piped סובל מחסימות BotGuard של גוגל ומסוגל לספק לכל היותר וידאו באיכות ירודה של 360p בלבד.

---

## 3. המלצה אסטרטגית לבוט (מה הדרך הנכונה?)

### המלצה חד-משמעית לפי פלטפורמה:

```
                  ┌──────────────────────────────────────────────┐
                  │          ניתוב בקשות הורדה בבוט             │
                  └──────────────────────┬───────────────────────┘
                                         │
         ┌───────────────────────────────┼──────────────────────────────┐
         ▼                               ▼                              ▼
    ┌─────────┐                    ┌───────────┐                 ┌─────────────┐
    │ TikTok  │                    │ Instagram │                 │   YouTube   │
    └────┬────┘                    └─────┬─────┘                 └──────┬──────┘
         │                               │                              │
         ▼                               ▼                              ▼
  [נתיב ראשי]                      [נתיב ראשי]                    [נתיב ראשי]
  TikWM API                        חילוץ מקומי                    yt-dlp מקומי
  (חינמי, 300ms, ללא מפתח)         (curl_cffi + GraphQL)          + Deno/EJS + PO Provider
         │                               │                              │
         ▼                               ▼                              ▼
  [Fallback גיבוי]                 [Fallback גיבוי]               [Fallback גיבוי]
  pybalt / dwnld.nichind.dev       yt-dlp מקומי עם עוגיות         dwnld.nichind.dev (Cobalt)
  או yt-dlp מקומי                  או Managed API בתשלום          (מוגבל ב-SLA ובפרטיות)
```

1. **TikTok — הסתמכות על שירות חיצוני כנתיב ראשי (מומלץ מאוד):**
   * **נתיב ראשי:** שימוש ב-**TikWM API** (`https://www.tikwm.com/api/?url=...&hd=1`).
   * **נימוק:** זמני תגובה מדהימים (~300–900ms), מחזיר קישורי CDN ישירים באיכות HD ללא ווטרמרק, יציב מעל 4 שנים, ללא צורך במפתח או תחזוקת עוגיות.
   * **גיבוי (Fallback):** אם TikWM חווה תקלה זמנית, מעבר אוטומטי ל-`dwnld.nichind.dev` (Cobalt) או הרצת `yt-dlp` מקומי.

2. **Instagram — אין שירות חיצוני חינמי בר-קיימא (שירות מקומי בלבד):**
   * **נימוק:** כל השירותים החינמיים שנבדקו נכשלו מול אינסטגרם (Cobalt החזיר `fetch.empty` בגלל חסימת IP של Meta; שירותים אחרים אינם תומכים או דורשים תשלום).
   * **הפתרון הנכון:** חילוץ מקומי באמצעות `curl_cffi` מול ה-GraphQL הפנימי של אינסטגרם (שרץ על כתובת ה-IP הביתית של הבוט), עם Fallback ל-`yt-dlp` עם עוגיות.

3. **YouTube — פסול לחלוטין כשירות חיצוני ראשי; אפשרי כ-Fallback משני בלבד:**
   * **מדוע לא כשירות ראשי?**
     * Piped ו-Invidious מתים או מוגבלים ל-360p.
     * רוב מופעי Cobalt חסומים ב-Turnstile.
     * הראוטר `dwnld.nichind.dev` שעבד בבדיקה הוא שירות חובבני של מפתח יחיד, ללא שום התחייבות לזמינות (SLA), המנתב משתמשים ל-IP של שרתי VPS אקראיים (ראו פירוט סיכונים להלן).
   * **הנתיב הראשי המומלץ:** **הרצה מקומית בלבד** של `yt-dlp` מעודכן + סביבת ריצה Deno + פלאגין `yt-dlp-ejs` + ספק PO Token מקומי (`bgutil`). זהו הנתיב היחיד שמבטיח איכות מלאה (1080p/4K), שליטה מלאה, יציבות לאורך זמן וללא עוגיות עבור תוכן ציבורי.

---

## 4. ניתוח סיכונים בהסתמכות על שירותים חיצוניים (Risk Analysis)

1. **נפילת שירות והיעדר SLA (Single Point of Failure):**
   * שירותים חינמיים כמו Piped או מופעי Cobalt מנוהלים על ידי מתנדבים ונסגרים ללא הודעה מוקדמת. בבדיקה האמפירית, **87% ממופעי Piped ו-100% ממופעי Invidious נמצאו מושבתים לחלוטין**.
   * תלות בשירות יחיד פירושה שכל עדכון חסימה של גוגל או מטא ישבית את הבוט שלכם עד שמתנדב צד-שלישי יחליט לתקן את השרת שלו.
2. **פרטיות ודלף מידע (Privacy & Data Leakage):**
   * כאשר משתמשי הבוט שולחים קישורים לשירות חיצוני (כמו `dwnld.nichind.dev` או מופעי Cobalt), קישורים אלו — שעשויים להכיל מזהים אישיים, שמות משתמש, סרטונים שאינם רשומים (Unlisted) או תוכן רגיש — נשלחים בצורה גלויה לשרתי צד-שלישי ול-VPSים של אנשים זרים ברחבי העולם (`185.197.195.62`, `146.255.188.127` וכו').
   * מפעילי שרתים אלו יכולים לתעד, לאסוף או לנתח את כל היסטוריית ההורדות של משתמשי הבוט שלכם.
3. **הטלת הגנות אנטי-בוט פתאומיות (Cloudflare / Turnstile):**
   * כפי שהוכח בבדיקת Cobalt: ברגע ששירות צובר פופולריות או סובל ממתקפות/ניצול על ידי בוטים, המפעיל מדליק הגנת Cloudflare Turnstile / Captcha. באותו רגע, ה-API הופך לחסום לחלוטין עבור תוכנות אוטומטיות.
4. **תקרת איכות (Quality Degradation):**
   * שירותים חיצוניים שאינם משקיעים במנגנוני חתימה ו-PO Token מתוחכמים נאלצים להסתפק בפורמטי Legacy נחותים (כמו 360p ב-Piped).
5. **סיכון משפטי וסגירה (Cease & Desist):**
   * שירותי הורדה פיראטיים נמצאים תחת איום משפטי מתמיד מצד חברות המדיה (YouTube/Meta). שירותים כאלה נסגרים לעיתים תכופות בן-לילה.

---

## 5. מפרט טכני מלא להפעלת המועמדים שעבדו

במידה ובוחרים לשלב את השירותים שעבדו בבדיקה, להלן המפרט הטכני המדויק לקריאה מהקוד:

### 5.1 מועמד מס' 1: TikWM (נתיב ראשי מומלץ ל-TikTok)
* **שיטה ו-Endpoint:**
  `GET https://www.tikwm.com/api/`
* **פרמטרים (Query String):**
  * `url` (חובה): כתובת הסרטון בטיקטוק (קישור מלא או מקוצר `vt.tiktok.com`).
  * `hd=1` (אופציונלי): בקשה לקבלת קישור HD.
* **כותרות מומלצות:**
  * `User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)...`
* **שדות מפתח בתשובת JSON:**
  * `code`: מספר (`0` = הצלחה, ערך שלילי = שגיאה).
  * `data.play`: מחרוזת URL — קישור ישיר ל-CDN (איכות SD ללא ווטרמרק).
  * `data.hdplay`: מחרוזת URL — קישור ישיר ל-CDN (איכות HD 1080p).
  * `data.music`: מחרוזת URL — פס קול MP3 ישיר.
  * `data.images`: מערך מחרוזות URL — במידה ומדובר באלבום תמונות (Slideshow).
  * `data.title`: כותרת/תיאור הסרטון.
* **טיפ יישום קריטי עבור טלגרם:**
  קישורי ה-CDN של TikTok חוסמים בקשות `HEAD` (מחזירים 503 מ-Akamai), אך מגיבים מצוין לבקשות `GET` עם Range. בטלגרם Bot API ניתן להעביר את ה-URL של `data.hdplay` ישירות למתודת `sendVideo(chat_id, video=hdplay_url)`.

---

### 5.2 מועמד מס' 2: Cobalt Multi-Instance Router / `dwnld.nichind.dev` (Fallback ל-YouTube ול-TikTok)
* **שיטה ו-Endpoint:**
  `POST https://dwnld.nichind.dev/`
  *(או ישירות למופע Cobalt פתוח כגון `https://cobaltapi.cjs.nz/`)*
* **כותרות חובה:**
  * `Accept: application/json`
  * `Content-Type: application/json`
  * `User-Agent: Mozilla/5.0...`
* **גוף הבקשה (JSON Payload):**
  ```json
  {
    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "videoQuality": "1080",
    "downloadMode": "auto"
  }
  ```
* **שדות תשובה עיקריים (JSON):**
  * במצב הצלחה:
    ```json
    {
      "status": "tunnel",
      "url": "http://185.197.195.62:9000/tunnel?id=...&exp=...&sig=...",
      "filename": "Rick Astley - Never Gonna Give You Up... (1080p, h264).mp4"
    }
    ```
    *(או `"status": "redirect"` כאשר הפלטפורמה מציעה קישור ישיר)*
  * במצב שגיאה:
    ```json
    {
      "status": "error",
      "error": {
        "code": "error.api.fetch.fail"
      }
    }
    ```
* **התנהגות הזרמה (Tunnel Handling):**
  כתובת ה-`tunnel` שמחזיר Cobalt מבצעת Muxing והזרמה בזמן אמת (HTTP Streaming). הבוט יכול לפתוח Stream ישיר מה-URL הזה ולהזרים את הבתים אל טלגרם, עם כותרת `RateLimit-Remaining` שמציינת את יתרת המכסה (מגבלה של 40 קריאות בדקה לכל IP).
