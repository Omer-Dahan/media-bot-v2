# YouTube בלי להחליף את yt-dlp — מצב ל־18.09.2026

**היקף אימות:** לא הורדתי וידאו ולא הרצתי את הבוט; “עובד כיום” להלן פירושו code/docs/issues פעילים ועדכניים של הפרויקט, לא SLA או בדיקת end-to-end על כל סרטון.

## 1. שורה תחתונה

אין ב־2026 מחלץ ציבורי שהוא גם מהיר יותר וגם אמין יותר מ־**yt-dlp מעודכן + Deno + `yt-dlp-ejs` + ספק PO Token**. זו אינה הבטחה ש־YouTube לא ישבור דברים: YouTube משנה את ה־attestation ואת אכיפת ה־PO, וה־PO Token קשור לעתים לווידאו/סשן ובעל תוקף מוגבל. אבל זה הנתיב היחיד שנמצא כאן עם תיקון פעיל, תמיכה מפורשת ב־JS challenge ובמסגרת ספקי PO; ההמלצה הרשמית העכשווית היא `mweb` + ספק PO ל־GVS. [yt-dlp: Extractors](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#youtube) · [PO Token Guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide) · [EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS)

לוידאו ציבורי רגיל אפשר בדרך כלל **להימנע מ־cookies**; ה־PO provider מפחית משמעותית את הצורך בהם, אך אינו מבטל חסימות IP/קצב או גישה לתוכן שדורש התחברות (פרטי, members-only, גיל/חשבון). ה־cookies הם fallback לתוכן כזה, לא טיפול נכון ב־n-sig או ב־PO. [Extractors: cookies](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies) · [rate limits](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#common-youtube-errors)

## 2. השוואה

| פתרון | האם עובד כיום? | דרישות ומגבלות | עומס תחזוקה / מהירות | אינטגרציה מפייתון |
|---|---|---|---|---|
| **yt-dlp + Deno/EJS + bgutil** (המלצה) | כן, הנתיב המומלץ והמתוחזק ביותר; yt-dlp נדחף ב־16.09.2026. [repo](https://github.com/yt-dlp/yt-dlp) · [EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS) | Deno 2.3+ (מומלץ), `yt-dlp-ejs`, וספק PO; ללא cookies לתוכן ציבורי. `mweb` דורש GVS PO; אין צורך ב־cookies אלא אם התוכן/חשבון דורש זאת. [PO guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide) | נמוך־בינוני: לעדכן yt-dlp/EJS/provider. הורדה עצמה מ־googlevideo; לא נמצאה ראיה שאחר מהיר ממנה. | `asyncio` subprocess עם `--dump-single-json`; לקחת format URL או לתת ל־yt-dlp להזרים stdout. URL הוא חתום/קצר־חיים, לכן להוריד מיד. [embedding](https://github.com/yt-dlp/yt-dlp#embedding-yt-dlp) |
| **youtubei.js / YouTube.js** | פעיל מאוד (commits עד 16.09.2026) ותומך ב־signature ו־n-sig; מקורו מוסיף `pot` רק ל־URL שאינו SABR. [repo](https://github.com/LuanRT/YouTube.js) · [Player.ts](https://github.com/LuanRT/YouTube.js/blob/main/src/core/Player.ts) | Node/Deno; צריך לייצר ולהעביר PO + visitor data בעצמכם. ל־SABR אין "URL ישיר" רגיל: Issue פתוח מדווח שאין URL לפענח; משתמש שעובד מתאר `bgutils-js` + דפדפן אמיתי, לא Node/headless בלבד. cookies אינם הכרחיים ציבורית, אך אינם פותרים זאת. [#1123](https://github.com/LuanRT/YouTube.js/issues/1123) · [BgUtils](https://github.com/LuanRT/BgUtils) | גבוה; אתם הבעלים של attestation, cache, SABR/מיזוג, וכל שינוי פרוטוקול. מהירות: **not verified** מול yt-dlp. | subprocess Node או microservice HTTP; אפשר להחזיר URL רק בפורמטים שאינם SABR. ל־SABR נדרש client/decoder נוסף, ולכן אינו פתרון Telegram פשוט. |
| **Streamlink YouTube plugin** | פעיל, אך אינו פתרון VOD מוגן: הקוד מזהה URL חסר ומחזיר במפורש “try yt-dlp instead”; maintainers אומרים שתמיכה מוגנת אינה ישימה. [plugin](https://github.com/streamlink/streamlink/blob/master/src/streamlink/plugins/youtube.py#L400-L425) · [maintainer](https://github.com/streamlink/streamlink/issues/6685#issuecomment-3410337457) | Python; אין PO provider/פענוח מלא של protected/SABR. cookies עשויים עדיין לקבל `LOGIN_REQUIRED`. [#6940](https://github.com/streamlink/streamlink/issues/6940) | טוב ל־live/HLS פשוט; לא אמין להורדת YouTube כללית. מהירות: **not verified**. | library או CLI pipeline, אך לא מומלץ כ־fallback לבוט הורדות. [API](https://streamlink.github.io/api.html) |
| **NewPipe Extractor (Java)** | פעיל (push 17.09.2026), תומך YouTube ויודע לפענח חתימות; release 2026 כלל תיקוני n/signature. [repo](https://github.com/TeamNewPipe/NewPipeExtractor) · [release](https://github.com/TeamNewPipe/NewPipeExtractor/releases) | JVM/Java library; PO helper קיים באקוסיסטם אך יש issue פתוח של תקרת 360p גם עם PO helper. אין תיעוד מאומת כאן ל־SABR/PO מלא ואמין. [#1541](https://github.com/TeamNewPipe/NewPipeExtractor/issues/1541) | בינוני־גבוה: אותם שינויים ב־YouTube, ועוד bridge Java↔Python. מהירות: **not verified**. | שירות Java HTTP/JSON או subprocess; יכול למסור stream URLs כאשר זמינים, לא יתרון מוכח על yt-dlp. |
| **Piped / Invidious** | שניהם פעילים, אך הם שרתים/instances ולא מחלץ מקומי אמין. Piped תלוי ב־NewPipeExtractor ומפרסם JSON API; Invidious נדרש ל־companion/עזרי signature ונפגע מ־PO/IP. [Piped](https://github.com/TeamPiped/Piped) · [Invidious](https://github.com/iv-org/invidious) · [Invidious #5657](https://github.com/iv-org/invidious/issues/5657) | HTTP instance; אין cookies אצל הבוט אם instance עושה את העבודה, אבל החסימה/PO עוברים לבעל ה־instance. instance ציבורי אינו SLA ואינו מומלץ לבוט פרודקשן. | גבוה אם self-host (DB/שרת/עדכונים); תלוי בקצב וב־IP שלו. מהירות: **not verified**. | HTTP JSON; אפשר proxy/URL, אך URL של googlevideo עדיין תלוי בטוקן/IP ולעתים ייחסם. |
| **"googlevideo direct URL"** | לא מחלץ. הוא עובד רק אחרי שמחלץ אחר פתר n-sig/signature ו־PO. [youtubei Player](https://github.com/LuanRT/YouTube.js/blob/main/src/core/Player.ts#L129-L241) · [PO guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide) | אותו client/IP/PO ובזמן התוקף; SABR אינו URL מדיה רגיל. cookies לא מחליפים token. | אפס תחזוקת extraction רק אם מישהו אחר עושה אותה; אינו חלופה. | ניתן להזרים מיידית ל־Telegram אם זה progressive URL תקין; אין לה cache או למסור אותו מאוחר יותר. |

## 3. למה yt-dlp "נשבר" ומה פותר כל רכיב

* **n-sig/signature decipher ו־JS challenge:** ללא runtime, yt-dlp מאבד תמיכה הולכת ונעלמת; Deno + `yt-dlp-ejs` פותרים את שכבת ה־JS העכשווית. זה קבוע תפעולית, לא קבוע לנצח: צריך לעדכן את הזוג ביחד. [EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS) · [README](https://github.com/yt-dlp/yt-dlp#strongly-recommended)
* **PO/GVS/Player/Subtitles:** Deno לבדו **אינו** מנפיק PO. ספק PO אוטומטי פותר את ה־token per-video/client במקרים שהוא תומך בהם. [PO guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)
* **SABR:** `web`/`web_safari` יכולים להחזיר SABR בלבד; לכן קיום metadata או URL אינו הוכחת הורדה. PO provider + בחירת `mweb` הוא הנתיב המומלץ; אין "client magic" חסר־cookies שנותן את כל הווידאו. [client matrix](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide#current-po-token-enforcement)
* **IP/guest reputation וקצב:** גם token תקין לא מבטיח bypass של bot-check/403. yt-dlp מתעד כ־~300 video requests/hour guest וכ־~2000 account, וממליץ delay 5–10 שניות; ספק bgutil מזהיר במפורש שאינו מבטיח bypass. [limits](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#common-youtube-errors) · [bgutil](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)

### לקוחות ללא cookies (נכון לעכשיו)

| client | בלי cookies? | PO / תנאי מהותי |
|---|---|---|
| `mweb` | כן | GVS PO; זה ה־recommended path. [guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide) |
| `web`, `web_safari` | כן | GVS (ו־Subs ב־web) PO; web/web_safari עשויים להיות SABR-only; ל־Safari יש HLS חריג שכרגע לא דורש GVS PO. [guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide#current-po-token-enforcement) |
| `android`, `ios` | כן (אינם תומכים account cookies) | GVS או Player PO; לא fallback יציב. [guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide#current-po-token-enforcement) |
| `android_vr` | כן | כרגע ללא PO, אך חסרים “Made for kids”; כיסוי חלקי בלבד. [guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide#current-po-token-enforcement) |
| `tv` / `web_embedded` | כן, טכנית | tv בלי cookies יכול להיות DRM/SABR-only; embedded רק embeddable. לא בסיס להבטחת שירות. [guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide#current-po-token-enforcement) |

## 4. ההגדרה המומלצת וסקיצת קריאה מהבוט (10 שורות, לא קוד)

1. לקבע גרסת `yt-dlp` עדכנית ולעדכן אותה בשגרה מבוקרת.
2. להתקין Deno 2.3+ ולוודא שהוא ב־PATH. [EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS#deno)
3. להתקין `yt-dlp[default]` כדי לקבל `yt-dlp-ejs` תואם. [EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS#option-1-install-the-yt-dlp-ejs-python-package)
4. להפעיל `bgutil-ytdlp-pot-provider` כ־HTTP server מקומי; הוא עדיף במהירות ובעומס על script-per-request. [bgutil](https://github.com/Brainicism/bgutil-ytdlp-pot-provider#1-set-up-the-provider)
5. השרת דורש Node 20+ או Deno 2+ (או Docker); לקשור אותו ל־`127.0.0.1` בלבד. [bgutil](https://github.com/Brainicism/bgutil-ytdlp-pot-provider#base-requirements)
6. להגדיר ב־yt-dlp את `mweb` ואת ה־provider; לא לשמור cookies למסלול הציבורי.
7. ב־Python להפעיל subprocess `yt-dlp` לכל request, עם JSON מובנה ו־timeout/retry מוגבלים.
8. לבקש format progressive אם Telegram זקוק ל־stream יחיד; audio+video נפרדים דורשים mux ולכן אין “ישר ל־Telegram” אמין.
9. לפתוח את URL החתום מיד מאותו host/IP ולהזרים bytes ל־Telegram; לא לשמור URL.
10. אם bgutil נכשל, לנסות `yt-dlp-getpot-wpc`: Chrome/Chromium + nodriver, דפדפן שמושק בזמן הקריאה; הוא fallback כבד יותר אך מייצר גם guest tokens. [WPC](https://github.com/coletdjnz/yt-dlp-getpot-wpc)

`bgutil` אינו דורש cookies ומבוסס `BgUtils`; יש לו HTTP server מהיר או script איטי יותר לכל קריאה. `yt-dlp-getpot-wpc` הוא חלופה של yt-dlp core maintainer, דורש Chrome/Chromium, ומשתמש בדפדפן כדי למנט PO; גם הוא אינו מבטיח שה־IP לא יסומן. [bgutil](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) · [WPC](https://github.com/coletdjnz/yt-dlp-getpot-wpc) · [featured providers](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide#featured-plugins)

## 5. מה תמיד עלול להישבר, ואבחון מוקדם

YouTube שולטת בפרוטוקול: שינוי ב־player JS, policy של client, SABR, PO binding/expiry, bot detection ו־IP reputation ישבור כל מחלץ צד־שלישי. אין ספרייה שמבטלת זאת. [PO technical details](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide#technical-details) · [Streamlink maintainer](https://github.com/streamlink/streamlink/issues/6685#issuecomment-3410337457)

יש לשמור stderr/debug מובנה ומדדים לכל שלב, ולהציג למשתמש סיבה מדויקת:

* `no JS runtime` / `Failed to decipher n/sig` → תקלה ב־Deno/EJS או גרסאות לא תואמות; התראה לתחזוקה, לא “צריך cookies”. [EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS)
* `requires a GVS/Player PO Token`, `HTTP 403` אחרי בחירת format → provider לא זמין/טוקן לא תואם; בדיקת provider ו־`mweb`, לא ניסיון cookie אקראי. [PO guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)
* `Sign in to confirm you’re not a bot` או `This content isn't available` → IP/guest rate limit; להאט, לתור, או להחליף egress מורשה. cookies אינם remediation מובטח. [limits](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#common-youtube-errors) · [bgutil caveat](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
* `LOGIN_REQUIRED`, private/members/age-gated → תוכן שבאמת דורש חשבון; רק כאן להציע cookies של חשבון ייעודי, בקצב נמוך ובהבנת סיכון חסימה. [cookies](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies)
* `SABR` / URL חסר / stream נקטע → לא להעביר URL ל־Telegram; לבחור fallback מותר או להחזיר “פורמט זה אינו זמין כרגע”. [client matrix](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide#current-po-token-enforcement)

## מקורות ראשיים

* [yt-dlp YouTube Extractors](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#youtube), [EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS), [PO Token Guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide).
* [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider), [yt-dlp-getpot-wpc](https://github.com/coletdjnz/yt-dlp-getpot-wpc), [BgUtils](https://github.com/LuanRT/BgUtils).
* [youtubei.js](https://github.com/LuanRT/YouTube.js), [SABR/PoToken issue #1123](https://github.com/LuanRT/YouTube.js/issues/1123), [Player implementation](https://github.com/LuanRT/YouTube.js/blob/main/src/core/Player.ts).
* [Streamlink YouTube plugin](https://github.com/streamlink/streamlink/blob/master/src/streamlink/plugins/youtube.py), [NewPipe Extractor](https://github.com/TeamNewPipe/NewPipeExtractor), [Piped](https://github.com/TeamPiped/Piped), [Invidious](https://github.com/iv-org/invidious).
