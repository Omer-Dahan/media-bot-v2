# בדיקת מסלולי הורדה לאינסטגרם — 2026-09-18

**קישור בדיקה:** ריל ציבורי אמיתי `https://www.instagram.com/reel/DX7PnqbFL50/` (חשבון NASA Internships), ועוד 5 רילים ציבוריים נוספים לבדיקת עקביות.
**מכונה:** ללא קוקיז, ללא רישום, ללא עקיפת אנטי-בוט. yt-dlp גלובלי קיים (`/home/vm/.agent-reach-venv`, גרסה `2026.07.04`) — לא הותקן דבר חדש.

---

## 1. טבלה מסכמת

| אתר / שיטה | עבד בפועל? | כתובת מדיה חזרה? | אנטי-בוט | זמן תגובה |
|---|---|---|---|---|
| **yt-dlp מקומי** (ללא קוקיז) | **כן**, 5/6 רילים ציבוריים | כן — קישור ישיר ל-`instagram.*.fna.fbcdn.net/...mp4` (אומת עם HEAD: `200`, `video/mp4`, 51MB) | לא נתקלנו בחסימה בריצה זו | ~2–3 שנ' לבקשה |
| **yt-dlp מקומי** — ריל בודד (`DCnOqnwMWnR`) | **לא**, עקבי לאורך 2 ניסיונות | לא — "Instagram API is not granting access" / "empty media response" | דורש קוקיז לפי הודעת השגיאה של yt-dlp עצמו | ~1 שנ' |
| **instaloader** | **לא אומת** — הכלי אינו מותקן באף venv זמין מחוץ לתיקיות האסורות, ואסור להתקין כלים חדשים | — | — | — |
| **snapinsta.app** | לא — הדומיין מת | — | — DNS לא נפתר (`Could not resolve host`) | — |
| **igram.io** | לא — הדומיין מת | — | — DNS לא נפתר | — |
| **snapinsta.to** | לא — חסום | — | Cloudflare JS Challenge ("Just a moment...") | 0.4 שנ' (עד לדף האתגר) |
| **save-free.com** | לא — חסום | — | Cloudflare JS Challenge | 0.3 שנ' |
| **igram.world** | לא נבדק סופית — HTML נטען (200) אך ללא endpoint גלוי בסטטי | — | Cloudflare (CDN, ללא אתגר גלוי בבדיקה הראשונית) | 0.36 שנ' |
| **savefrom.net** | לא — נדחה | לא — `"invalid_request":true,"success":false` | דורש הרצה מדפדפן אמיתי (בדיקת `window.location.hostname`, worker דרך `worker.savefrom.net`) | 0.1–0.12 שנ' |
| **instasupersave.com** (`/api/convert`) | לא — נדחה, אותה תשתית כמו savefrom.net | לא — אותה תשובת שגיאה בדיוק | קפצ'ה + "signed request body" נדרשים לפני הקריאה ל-`/api/convert` | 0.24 שנ' |
| **snapany.com** | לא אומת דרך ה-UI החינמי — נמצא רק ה-API הרשמי בתשלום | — | ה-API הרשמי (`api.snapany.com/openapi/v1/extract/post`) דורש `Authorization: Bearer sk_snapany_xxx` (נבדק: `401 invalid_api_key`) | 0.2–0.6 שנ' |
| **dolphinradar.com** | **לא רלוונטי** — זהו כלי אנליטיקס/מעקב פרופילים ("100% platform compliant"), לא מוריד רילים/פוסטים. לפי הצהרות שיווקיות באתר יש הורדת Stories/Highlights חינמית — לא אומת בפועל | — | — | — |

---

## 2. השורה התחתונה

**המסלול המקומי (yt-dlp, ללא קוקיז) הוא כרגע הדרך הסבירה ביותר להוריד ריל ציבורי מהמכונה הזו — לא שירות חיצוני.**

זה היפוך של המסקנה מהבדיקה הקודמת: נכון לעכשיו (ספטמבר 2026), עבור **תוכן פומבי**, `yt-dlp` מצליח לחלץ קישור מדיה ישיר ב-83% מהמקרים שנבדקו (5/6) **בלי שום קוקיז, בלי חשבון, ובלי עקיפת הגנה**. לעומת זאת, כל אתרי ה"קלון" החיצוניים שנבדקו נכשלו: חלקם מתים ברמת ה-DNS, חלקם חסומים ב-Cloudflare, וחלקם דוחים את הבקשה במפורש כי הם דורשים הרצה אמיתית בדפדפן (JS execution, קפצ'ה, "signed request body") שקשה לזייף בסקריפט שרת פשוט. ה-API החוקי היחיד שנמצא (snapany) דורש הרשמה ומפתח בתשלום — מחוץ לתחום המשימה.

**הסתייגות חשובה:** הצלחת yt-dlp אינה 100%. ריל אחד מתוך שישה נכשל בעקביות עם "Instagram API is not granting access" ודרש קוקיז. כלומר יש להטמיע **נפילה חזרה לקוקיז** (ולא רק ל-yt-dlp אנונימי) לפוסטים שנכשלים — כנראה תלוי בגיל/סוג הפוסט או במגבלות שמטא מטילה נקודתית, לא בזיהוי בוט גורף.

---

## 3. אם צריך קוקיז — מה נדרש והסיכון

כשההרצה האנונימית נכשלת (כמו ב-`DCnOqnwMWnR` למעלה), ל-yt-dlp יש נתיב חלופי מלא-הרשאות: `--cookies-from-browser` או `--cookies cookies.txt` עם עוגיות session אמיתיות (`sessionid`, `csrftoken`, `ds_user_id` וכו') מחשבון אינסטגרם מחובר.

**הסיכון לחשבון:**
- שימוש חוזר/אוטומטי בעוגיות `sessionid` משרת חיצוני (לא מהדפדפן שבו הן נוצרו) הוא בדיוק הדפוס שמטא מזהה כ"session hijacking" ומוביל להתנתקות אוטומטית / דרישת אימות מחדש / חסימה זמנית.
- ריבוי בקשות עם אותן עוגיות (הורדות רבות דרך בוט) מגביר סיכון ל"Suspicious Login" ולנעילת החשבון, בפרט אם ה-IP של השרת שונה מה-IP הרגיל של המשתמש.
- ההמלצה המקובלת: להשתמש בחשבון "מבודד" (לא אישי) ייעודי להורדות אם בכלל הולכים בכיוון הזה, ולצפות לצורך בהחלפת עוגיות מדי פעם.

מכיוון שכללי המשימה אסרו על יצירת/שימוש בחשבון, **לא בוצעה בדיקה עם קוקיז בפועל** — הסעיף הזה מבוסס על התנהגות מתועדת של yt-dlp ועל ידע כללי על מדיניות מטא, לא על ניסוי שבוצע כאן.

---

## 4. הפירוט המדויק למימוש

### א. הפקודה שעבדה (ללא קוקיז)
```bash
yt-dlp -g "https://www.instagram.com/reel/<SHORTCODE>/"
# או לכל השדות:
yt-dlp --simulate -j "https://www.instagram.com/reel/<SHORTCODE>/"
```
`-g` מחזיר ישירות את כתובות ה-DASH הנפרדות (וידאו + אודיו) בפורמט:
```
https://instagram.<host>.fna.fbcdn.net/o1/v/t2/f2/m.../<blob>.mp4?_nc_cat=...&_nc_oc=...&oh=...&oe=...
```
כתובות אלה הן ישירות מ-CDN של פייסבוק/מטא (`fbcdn.net`), פגות תוקף (`oe=` פרמטר epoch), ואינן דורשות שום auth headers בבקשת ה-GET עצמה — אומת עם `curl -I` שהחזיר `200 video/mp4`.

### ב. מה קורה מתחת למכסה (מקוד המקור של yt-dlp — `yt_dlp/extractor/instagram.py`)
1. **בדיקת נגישות (ללא צורך בקוקיז):**
   `GET https://i.instagram.com/api/v1/web/get_ruling_for_content/?content_type=MEDIA&target_id=<media_pk>`
   עם header `X-IG-App-ID` (ID קבוע של אפליקציית הווב של אינסטגרם).
2. **אם יש curl_cffi מותקן (impersonation של דפדפן אמיתי ברמת TLS/JA3)** — קריאת GraphQL אנונימית:
   `POST https://www.instagram.com/api/graphql`
   עם `doc_id=27130156389949648`, `variables={"media_id": ...}`, `fb_api_req_friendly_name=PolarisLoggedOutDesktopWWWPostRootContentQuery`, ו-`X-CSRFToken` מהעוגיה שהתקבלה בשלב 1.
3. **בסביבה הזו `curl_cffi` אינו מותקן** (נבדק: `pip show curl_cffi` → not found), כך ש-yt-dlp נופל אוטומטית למסלול השלישי: הורדת עמוד הפוסט הרגיל (`https://www.instagram.com/p/<id>`) וחילוץ JSON מוטמע מתוך תגיות `<script data-sjs>` (מטמון `RelayPrefetchedStreamCache` → `data.xig_polaris_media.if_not_gated_logged_out`).
4. **חשוב לדעת:** ניסינו לשחזר שלב 3 עם `curl` רגיל (כולל cookie jar אמיתי מ-`https://www.instagram.com/`) והתשובה **לא** הכילה את ה-JSON הזה — כלומר ההצלחה של yt-dlp תלויה ברצף בקשות/headers/state פנימי מדויק שהוא מנהל (session bootstrap, סדר קריאות, כותרות ספציפיות), לא רק ב"לעשות GET לעמוד". **המסקנה המעשית: אל תנסו לשחזר את הפרוטוקול בעצמכם — הפעילו את yt-dlp כתת-תהליך** (או דרך ה-Python API שלו) ותנו לו לנהל את כל השרשרת.

### ג. מבנה קלט/פלט מומלץ למימוש בבוט
```python
import yt_dlp

def get_instagram_media_url(post_url: str) -> dict:
    opts = {"quiet": True, "skip_download": True, "simulate": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(post_url, download=False)
    return info  # info["url"] / info["requested_formats"][i]["url"]
```
טיפול בכישלון: לתפוס `yt_dlp.utils.ExtractorError`, ואם ההודעה מכילה `"empty media response"` / `"not granting access"` — ליפול חזרה לנתיב עם קוקיז (אם קיים חשבון ייעודי) או לדווח "נדרש חיבור".

### ד. אתרי חוץ — endpoints שזוהו (לתיעוד בלבד, לא לשימוש בפועל)
- `savefrom.net`: `<form action="/savefrom.php" method="POST">` עם שדות `sf_url, new=2, lang=en, app=`, אך הביצוע האמיתי קורה ב-JS מול `https://worker.savefrom.net/savefrom.php` (worker נפרד, בודק `window.location.hostname`).
- `instasupersave.com`: `POST /api/convert` עם גוף `{"target_url": "<url>"}`, אך מוגן בזרימת קפצ'ה (`GET/POST /api/captcha`) ו-"signed request body" (`subscribeSignedRequestBody`) לפני שהשרת מקבל את הבקשה.
- `snapany.com`: ה-UI החינמי לא חשף endpoint סטטי בבנדל ה-JS (כנראה Next.js Server Actions, לא REST רגיל). ה-API המתועד היחיד הוא בתשלום: `POST https://api.snapany.com/openapi/v1/extract/post` עם `Authorization: Bearer sk_snapany_xxx`.

---

## 5. המלצה

1. **ליישם ראשית: `yt-dlp` מקומי, אנונימי, כתת-תהליך/ספריית Python**, בדיוק כמו במסלולים שכבר עובדים לטיקטוק/יוטיוב. זה המסלול היחיד שאומת כעובד בפועל היום, ללא סיכון לחשבון וללא תלות בצד שלישי לא אמין.
2. **גיבוי לכישלונות:** לזהות הודעות שגיאה ספציפיות של yt-dlp (`"not granting access"`, `"empty media response"`) ולהציע נתיב שני עם `--cookies` מחשבון ייעודי (לא אישי) — מודעים לסיכון חסימה שתואר בסעיף 3. לתעד את זה כתכונה "best effort", לא הבטחה.
3. **לא לבנות תלות באתרי הורדה חיצוניים** — כולם נכשלו בבדיקה האמיתית (DNS מת / Cloudflare / דחייה מפורשת / API בתשלום). אם רוצים "רשת ביטחון" נוספת, `dolphinradar.com` ראוי לבדיקה עתידית ממוקדת (טוען הורדת Stories חינמית) — אך זה לא כלי הורדת רילים/פוסטים, ודורש בדיקה נפרדת עם דפדפן אמיתי (לא בוצעה כאן).
4. **לנטר גרסת yt-dlp:** ההצלחה כרגע תלויה ב"webpage fallback" ולא בקריאת GraphQL עם impersonation — זה עלול להישבר אם מטא תשנה את מבנה ה-`data-sjs`. מומלץ `yt-dlp -U` תקופתי ומעקב אחר issues בפרויקט.
