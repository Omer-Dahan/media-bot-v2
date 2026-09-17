# מחקר אמפירי: אתרי חילוץ מדיה (TikTok / YouTube / Instagram)

תאריך הרצה: 2026-09-18. שיטה: קריאת HTML/JS בפועל של כל אתר לזיהוי ה-endpoint הפנימי, שליחת בקשה אמיתית עם קישור ציבורי תקף, ואימות כתובת המדיה שחזרה ב-HEAD. לא הורד קובץ מדיה בפועל, לא נפתח מפתח API, לא בוצעה עקיפת אנטי-בוט.

**קישורי בדיקה שאומתו כתקפים לפני השימוש:**
- TikTok: `https://www.tiktok.com/@tiktok/video/7106594312292453675` (אומת מול oEmbed הרשמי של טיקטוק)
- YouTube: `https://www.youtube.com/watch?v=YE7VzlLtp-4` ("Big Buck Bunny", ערוץ Blender הרשמי, קריאייטיב קומונס — נבחר במקום סרטון מסחרי אחרי שהתגלה שסרטונים עם מוזיקה מסחרית (Rick Astley) נחסמים ע"י אחד מהשירותים בגלל זכויות יוצרים ולא בגלל תקלה טכנית)
- Instagram: `https://www.instagram.com/p/DS-p4rvCJhr/` (אותר ואומת כנגיש - HTTP 200 - אך **לא נוצל בפועל**, ראו פערי כיסוי למטה)

**פערי כיסוי חשובים (יש לדעת לפני שמסתמכים על הדוח):**
- **אינסטגרם: אף אתר לא נבדק בפועל** בריצה זו (snapinsta.app, igram.io, dolphinradar.com, snapany.com, savefrom.net) — לא נשלחה אף בקשה אמיתית לאף אחד מהם. זהו החור המשמעותי ביותר בדוח.
- **loader.to**: מופו ה-endpoints וה-domain/API-key מתוך ה-JS, אך **לא בוצעה בקשה חיה מלאה עם וידאו אמיתי** (המחקר נעצר באמצע שרשרת הבקשות). המידע למטה הוא ניתוח סטטי בלבד, לא אומת קצה-לקצה.
- **yout.com, ssyoutube.com**: נשלפו רק דפי הבית (200 OK), לא בוצע ניתוח endpoint ולא בקשה אמיתית — "לא נבדק".
- **snapany.com**: נבדק רק במסלול הכללי (לא ספציפית טיקטוק/יוטיוב/אינסטגרם) — ה-API הציבורי המתועד דורש מפתח בתשלום (אומת 401 בלי להירשם), אך ה-endpoint החינמי שמשמש את אתר האינטרנט לא אותר (קוד ה-JS שלו מפוצל דינמית ונטען רק בזמן ריצה בדפדפן אמיתי).

---

## 1. טבלה מרכזית

| אתר | פלטפורמות שנבדקו | עבד בפועל? | כתובת מדיה חזרה? | אנטי-בוט | זמן תגובה |
|---|---|---|---|---|---|
| **musicaldown.com** | TikTok | ✅ כן (200) | ✅ כן — אומת HEAD 200, 30MB | לא (רק שמות שדה טופס אקראיים לכל טעינה) | ~0.9s |
| **tikdownloader.io** | TikTok | ✅ כן (200) | ✅ כן — אומת HEAD/redirect 200, 30MB | לא | ~1.4s |
| **ssstik.io** | TikTok | ❌ לא (200 אך גוף תשובה ריק) | לא | כן — כנראה Cloudflare bot-management (ללא Set-Cookie גלוי, ייתכן fingerprint ב-JS) | 0.15s |
| **snaptik.app** | TikTok | ❌ לא (403 "Verification failed") | לא | כן — אתגר קריפטוגרפי לכל בקשה (ראו פירוט) | 0.2s |
| **tikmate.online** | TikTok | ❌ לא — דומיין "חטוף" | לא רלוונטי | לא רלוונטי | — |
| **savefrom.net** | TikTok | ❌ לא (200 אך `invalid_request:true`) | לא | כן — חתימת בקשה מחושבת בצד לקוח + CAPTCHA בחשד לבוט | 0.4s |
| **snapany.com** | כללי (לא נבדק לפלטפורמה ספציפית) | ⚠️ לא נקבע | לא | API הציבורי: כן (401 ללא מפתח). ה-UI החינמי: לא ידוע (קוד דינמי) | — |
| **ytmp3.la → ytmp3.gl** | YouTube | ✅ כן (200, מלא) | ⚠️ HEAD חזר 405 (לא נתמך), אך זרימת ה-JSON הסתיימה בהצלחה עם הכותרת הנכונה | Referer/Origin חובה (403 בלעדיו), לא CAPTCHA | ~1s לכל שלב, ~4s סה"כ |
| **y2mate.com** | YouTube | ❌ לא — הדומיין לא קיים ברשת (NXDOMAIN) | לא רלוונטי | לא רלוונטי | — |
| **yt5s.io** | YouTube | ❌ לא — השירות הודיע רשמית שהפסיק תמיכה ביוטיוב (מדצמבר 2024) | לא | לא (זו הודעת מדיניות, לא חסימה טכנית) | 0.36s |
| **9convert.com** | YouTube | ❌ לא — הדומיין נסגר ע"י IFPI (הפרת זכויות יוצרים) | לא רלוונטי | לא רלוונטי | — |
| **loader.to** | YouTube | ⚠️ לא נבדק סופית (רק ניתוח סטטי של ה-JS) | לא אומת | לא ידוע | — |
| **yout.com** | YouTube | ⚠️ לא נבדק (רק דף הבית נשלף) | לא אומת | לא ידוע | — |
| **ssyoutube.com** | YouTube | ⚠️ לא נבדק (רק דף הבית נשלף) | לא אומת | לא ידוע | — |
| **snapinsta.app** | Instagram | ⚠️ לא נבדק כלל | לא אומת | לא ידוע | — |
| **igram.io** | Instagram | ⚠️ לא נבדק כלל | לא אומת | לא ידוע | — |
| **dolphinradar.com** | Instagram | ⚠️ לא נבדק כלל | לא אומת | לא ידוע | — |

---

## 2. הפירוט למנצח בכל פלטפורמה

### TikTok — שני מנצחים שעבדו בפועל

**א. tikdownloader.io (הפשוט והמהיר מבין השניים)**
- **Endpoint:** `POST https://tikdownloader.io/api/ajaxSearch`
- **Headers:** `X-Requested-With: XMLHttpRequest`, `Referer: https://tikdownloader.io/en` (לא חובה קפדנית, אך מומלץ)
- **גוף הבקשה (`application/x-www-form-urlencoded`):** `q=<כתובת הטיקטוק המלאה>&lang=en`
- **אין טוקן/CSRF/חתימה נדרשים** — בקשה "נקייה" לגמרי.
- **מבנה התשובה:** JSON: `{"status":"ok","data":"<HTML string>"}`. בתוך שדה ה-`data` (HTML גולמי) יש קישורי `<a href="https://dl.snapcdn.app/get?token=<JWT>">` עבור: תמונה ממוזערת, MP4 (עם סימן מים), MP4 HD (בלי סימן מים), ו-MP3 (השמע בלבד).
- **JWT** ב-`dl.snapcdn.app/get?token=...` מקודד בתוכו את כתובת ה-CDN האמיתית של טיקטוק, שם קובץ, ותוקף (`exp`). קריאת GET לכתובת הזו מחזירה 302 redirect לכתובת ה-CDN הישירה של טיקטוק (`v16.tokcdn.com/...`), ומשם 200 עם הקובץ.
- **אומת:** HEAD/redirect chain החזיר 200, `content-length: 30294767`, `content-type: application/octet-stream`.

**ב. musicaldown.com (חלופה, קצת יותר מסובך)**
- **Endpoint:** `POST https://musicaldown.com/download`
- **מלכודת:** שמות שדות הטופס **מוחלפים אקראית בכל טעינת דף** (למשל `_utX`→`_hew`→`_Kkr`), וכך גם שם וערך של שדה טוקן נסתר (MD5-looking, 32 hex chars). **חובה** קודם GET לדף (`/en`), לפרסר את ה-HTML הטרי ולחלץ את שמות/ערכי השדות הנוכחיים, ורק אז לשלוח את ה-POST עם אותה session (cookie jar). זה לא אתגר קריפטוגרפי אמיתי — רק obfuscation נגד סקרייפרים סטטיים שמצפים לשמות שדה קבועים.
- **גוף הבקשה:** `<url_field>=<כתובת הטיקטוק>&<token_field>=<ערך הטוקן שנשלף>&verify=1`
- **מבנה התשובה:** דף HTML מלא (לא JSON) עם קישורי `<a href="https://fastdl.muscdn.app/v3?token=<JWT>">` ל-MP4 רגיל, HD, עם סימן מים, ו-MP3.
- **אומת:** HEAD על קישור ה-HD החזיר 200, `content-length: 30294767` (אותו קובץ בדיוק כמו ב-tikdownloader — כנראה משתמשים באותו CDN/backend מאחורי הקלעים).

**החסומים (תועדו, לא נפרצו):**
- **ssstik.io** — טופס htmx שולח `POST /abc?url=dl` עם `id=<url>&locale=en`. ללא ריצת JS בדפדפן אמיתי, השרת מחזיר 200 עם **גוף ריק לחלוטין**. אין Set-Cookie גלוי בתגובת ה-GET הראשונית, כך שסביר שמדובר ב-Cloudflare Bot Management שמזהה טביעת אצבע דפדפן חסרה ומחזיר תשובה "שקטה" (לא שגיאה מפורשת) — קשה/לא כדאי לעקוף בלי דפדפן אמיתי.
- **snaptik.app** — מנגנון אנטי-בוט קריפטוגרפי אמיתי, לא CAPTCHA: `POST /api/token` מחזיר `{id, p}`; `p` הוא Base64 שמפוענח ל-16 בתים IV + ciphertext. המפתח ל-AES-CBC הוא `SHA256(secret+":"+id)` כאשר `secret` הוא מחרוזת קבועה בקוד ("sn4pt1k_v3r1fy2026" בהרכבה מפוצלת). אחרי פענוח מקבלים JSON עם אחד מ-5 סוגי "חידה" אריתמטית פשוטה (חיבור/XOR/הזזת ביטים), שהתשובה לה + מזהים נוספים (`_e`,`_h`) מורכבים לכותרת `X-Verify: id:answer:_e:_h` שנדרשת ב-`GET /api/extract?url=...`. בלי הכותרת: **403 "Verification failed"**. זהו טוקן מחושב-מחדש לכל בקשה — ניתן טכנית ל"פתור" בסקריפט (זו לא הגנת דפדפן אמיתית, רק חשבון), אבל זה בדיוק סוג ה"אתגר" שההנחיה אסרה לפרוץ, ולכן לא מומש/נבדק בפועל.
- **savefrom.net** — הטופס הגלוי שולח `POST /savefrom.php` עם `sf_url,new=2,lang=en,app=`, אך זו רק מעטפת: JS מריץ `workerApi()` שמפנה בפועל ל-`POST https://worker.savefrom.net/savefrom.php` עם אותם פרמטרים + `sf-nomad=1`, ולפני השליחה קורא למודול נטען-דינמית `initSignedRequestBody(url)` שמוסיף שדה חתימה נוסף לגוף הבקשה. בלי החתימה הזו קיבלנו תשובה "רכה" — HTTP 200 אבל `{"invalid_request":true,"success":false}`. קוד ה-JS גם מראה שבתשובת 422 חוזרת נפתח דיאלוג CAPTCHA (`sf.captcha.showDialog()`). לא מומש הפתרון.
- **tikmate.online** — ה-DNS עדיין עונה אבל הדף עצמו הוא ProdPage/Domain-Parking גנרי (שירות "ParkLogic": `router.parklogic.com`) שמזהה geo/adblock ומפנה לפרסומות. **הדומיין נחטף/נמכר ואינו מפעיל שירות הורדה כלל.**

### YouTube — מנצח יחיד שעבד: ytmp3.la (מתפנה ל-ytmp3.gl)

- **דומיין:** `ytmp3.la` עושה 301 קבוע ל-`ytmp3.gl` (מיתוג/מיגרציה, לא נפילה).
- **Backend נפרד לגמרי מהדומיין הפרונטי:** `gamma.gammacloud.net` (שם מוסתר ב-Base64 בקוד: `atob("Z2FtbWFjbG91ZC5uZXQ=")`).
- **זרימה בת 4 שלבים, כולם GET:**
  1. `GET https://gamma.gammacloud.net/api/v1/auth?api_key=<key>&_=<timestamp>` — **`api_key` הוא ערך קבוע** שמוטמע כטקסט גלוי בדף הבית (`apiKey='9b0ed5dab31616027ad7154140b0272d'`), לא מחושב. **דורש Header `Referer: https://ytmp3.gl/`** — בלעדיו 403 Forbidden ברמת nginx (בדקנו את שניהם). מחזיר `{"geo":"0","key":"<session_key>","err":0}`.
  2. `GET https://gamma.gammacloud.net/api/v1/init?_=<timestamp>` עם Header `Authorization: Bearer <session_key>` — מחזיר `{"convertURL":"https://<תת-דומיין אקראי>.gammacloud.net/api/v1/convert?sig=<חתימה ארוכה שהשרת כבר חתם עליה>"}`. **החתימה מיוצרת ע"י השרת ומוחזרת מוכנה — אין צורך לחשב שום דבר בצד הלקוח.**
  3. `GET <convertURL>&v=<youtube_video_id>&f=<mp3|mp4>&_=<timestamp>` — מחזיר `{"progressURL":...,"downloadURL":...,"redirect":0,"title":""}`.
  4. Polling: `GET <progressURL>&_=<timestamp>` כל 3 שניות עד `"progress":3`. בבדיקה שלנו זה הסתיים **מיידית בפעם הראשונה** עם `{"progress":3,"error":0,"title":"Big Buck Bunny"}`.
  5. הורדה סופית: `GET <downloadURL>&v=<id>&f=<format>&r=<hostname>`.
- **חשוב — בדיקת זכויות יוצרים:** כשניסינו עם סרטון עם מוזיקה מסחרית (Rick Astley) קיבלנו `{"progress":-1,"error":645}` שלא התקדם אף פעם — סימן לחסימת תוכן ולא לתקלה טכנית. עם סרטון קריאייטיב-קומונס (Big Buck Bunny) הכל עבד מושלם. **המשמעות התפעולית:** בבוט אמיתי צריך לצפות שחלק מהבקשות ייכשלו ספציפית בגלל הגנת זכויות-יוצרים של השירות עצמו, לא בגלל הבוט.
- **אימות מדיה:** בקשת `HEAD` על ה-`downloadURL` הסופי החזירה **405 Method Not Allowed** (השרת לא תומך ב-HEAD על endpoint זה) — **לא הצלחנו לאמת עם HEAD כנדרש בפרוטוקול**. עם זאת, השלמת ה-polling עם `progress:3` וכותרת נכונה ("Big Buck Bunny") היא אינדיקציה חזקה עקיפה שהקישור תקף.
- **חוסן פנימי מעניין:** קוד ה-JS מכיל fallback ל-Cloudflare Worker (`fancy-sea-*.workers.dev`) אם ה-auth הראשי נכשל, וגם קישור חלופי ל"TubeAPI" (`tubeapi.org`) עבור קודי שגיאה מסוימים — סימן ששירות הליבה כבר נופל מדי פעם וזה טופל מראש.

**הכשלים המתועדים (לא כתוצאה מאנטי-בוט טכני אלא ממדיניות/מוות של האתר):**
- **y2mate.com** — לא עונה ל-DNS כלל (`NXDOMAIN`, אומת גם ב-`getent`/`socket.gethostbyname`). מת לגמרי.
- **9convert.com** — מציג במפורש דף "This site has been shutdown for copyright infringement" מטעם IFPI (איגוד תעשיית התקליטים הבינלאומי). **נתפס/הוסר אכיפתית.**
- **yt5s.io** — ה-API עצמו עונה תקין (`POST /api/ajaxSearch`, אותה ארכיטקטורה בדיוק כמו tikdownloader.io — כולל אותו מזהה מפרסם AdSense `ca-pub-3831853078543758`, מה שמרמז על אותו מפעיל) אבל מחזיר הודעה מפורשת: *"Starting from December 1, 2024, YT5s has discontinued support for downloading videos from Youtube."* — כלומר הפסיקו יוטיוב ביודעין (סביר שבגלל לחץ משפטי).

### Instagram — לא נבדק כלל

לא בוצעה אף בקשה לאף אחד מ-snapinsta.app / igram.io / dolphinradar.com / snapany.com / savefrom.net עבור אינסטגרם בריצה זו. יש להריץ סבב בדיקה נפרד לפני שמממשים תמיכה באינסטגרם בבוט. קישור בדיקה תקף אותר ומאומת מראש (`instagram.com/p/DS-p4rvCJhr/`, HTTP 200) ומוכן לשימוש בסבב הבא.

---

## 3. הערכת עמידות

| אתר | הערכה |
|---|---|
| tikdownloader.io | **יציב יחסית.** בקשה נקייה בלי טוקנים, ארכיטקטורת proxy-CDN (snapcdn.app) שנראית מתוחזקת. הסיכון העיקרי: אותו מפעיל מריץ גם yt5s.io ש"ויתר" על יוטיוב תחת לחץ — כלומר המפעיל *כן* מגיב ללחץ משפטי ועלול לסגור גם את שירות הטיקטוק בבת אחת. |
| musicaldown.com | **בינוני.** האתגר היחיד (שמות שדה אקראיים) הוא קוסמטי ולא אמיתי, קל לתחזק בקוד לקוח. אבל תלוי בפרסינג HTML חי בכל בקשה, כך שכל שינוי מבנה בדף ישבור את הקוד בלי אזהרה. |
| ytmp3.la/gl | **בינוני-נמוך.** תלוי בבדיקת Referer קשיחה (קל לזייף, אבל דורש לזכור להוסיף), ותלוי גם ב"אישור זכויות יוצרים" פנימי של השירות שיכול לחסום סרטונים ספציפיים באופן בלתי צפוי. הארכיטקטורה מפוזרת (gammacloud.net + Cloudflare Worker fallback) מרמזת שהם כבר נופלים לפעמים ובנו תוכניות חירום — סימן שהשירות תחת עומס/רדיפה משפטית מסוימת. |
| snaptik.app / ssstik.io / savefrom.net (TikTok) | **לא רלוונטי להטמעה ללא דפדפן אמיתי** — כולם משקיעים אקטיבית בהגנה (קריפטו, Cloudflare Bot Management, CAPTCHA), מה שאומר שהמפעילים מודעים לגרידה ומשקיעים במניעתה; לא כדאי להתבסס עליהם. |
| y2mate.com, 9convert.com, tikmate.online | **מתים.** אל תתכננו סביבם בכלל. |
| yt5s.io | חי אך **חסר תועלת ליוטיוב במפורש** (הפסיקו ביוזמתם). |
| loader.to, yout.com, ssyoutube.com, כל אתרי אינסטגרם | **לא ידוע** — נדרשת בדיקה נוספת לפני שמסתמכים עליהם. |

---

## 4. סיכון תפעולי

- **חסימת IP:** לא נצפתה חסימה על בסיס IP בודד בבדיקות שלנו (כל האתרים שעבדו ענו על בקשה בודדת ללא בעיה). עם זאת, כל האתרים שרצים מאחורי Cloudflare (רובם) יכולים להפעיל חסימת IP/rate-limit דינמית על נפח בקשות חוזר מאותה כתובת — עם "מעט הורדות ביום" הסיכון נמוך.
- **מגבלות קצב:** לא נבדקו ישירות (לא שלחנו בקשות חוזרות מספיק כדי לגלות סף), אבל savefrom.net מפעיל CAPTCHA בזיהוי חשד לבוט, ו-tikdownloader.io/yt5s.io כוללים בקוד שלהם התייחסות מפורשת לקוד תשובה 429 ("You have performed the action too quickly") — כלומר יש rate-limit קיים בפועל שם.
- **האם אפשר להגיש קישור בלי לפתוח דפדפן:** **כן, במלואו** עבור tikdownloader.io, musicaldown.com, ו-ytmp3.la/gl — כל השלושה נבדקו ועבדו עם `curl`/HTTP client רגיל, בלי JavaScript engine ובלי headless browser. זה בדיוק המודל שהבוט צריך.
- **תלות בצד שלישי בתוך התשובה עצמה:** גם tikdownloader.io וגם musicaldown.com מחזירים קישורי מדיה עטופים ב-JWT דרך CDN-proxy משלהם (snapcdn.app / muscdn.app) ולא ישירות מ-TikTok CDN — כלומר יש עוד שכבת תלות (אם ה-proxy הזה נופל, הבוט נופל גם אם tiktok.com תקין). לזה יש תוקף פקיעה (`exp` ב-JWT, ~1 שעה) — **אסור לשמור/לקאש כתובות מדיה לטווח ארוך, יש להוריד מיד.**

---

## 5. המלצה

**למימוש ראשון:** `tikdownloader.io` לטיקטוק — הבקשה הכי פשוטה (POST אחד בלי טוקנים), הכי מהירה, ואומתה קצה-לקצה. עבור יוטיוב, `ytmp3.la`/`ytmp3.gl` הוא הבחירה היחידה שעבדה בפועל — יש לממש את שרשרת ה-4 השלבים (auth→init→convert→progress→download) ולזכור: (א) חובה Header Referer תואם, (ב) לצפות לכישלונות על תוכן עם זכויות יוצרים מסחריות ולא לפרש את זה כבאג, (ג) HEAD לא נתמך על ה-download endpoint הסופי שלהם — יש להסתמך על `progress:3` בתגובת ה-JSON כאישור.

**גיבוי:** `musicaldown.com` כגיבוי לטיקטוק (אם tikdownloader.io נופל) — דורש רק parsing קל של שמות שדה דינמיים בכל בקשה, לא אתגר אמיתי.

**לפני שממשים אינסטגרם בכלל:** צריך סבב בדיקה נפרד ומלא (snapinsta.app, igram.io, dolphinradar.com לפחות) — הדוח הזה לא נותן שום בסיס אמפירי להחלטה שם. מומלץ גם לבדוק את loader.to (הארכיטקטורה שמופתה כאן — `p.savenow.to` + `SHARED_FRONTEND_API_KEY` סטטי — נראית מבטיחה אך לא אומתה בפועל) ואת yout.com/ssyoutube.com כחלופות נוספות ליוטיוב, כדי שלא להיות תלויים במקור בודד (ytmp3.gl) שההודעה `error:645` שלו מראה שהוא כבר מפעיל חסימות סלקטיביות.
