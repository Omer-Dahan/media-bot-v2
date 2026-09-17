# External Media Extraction Providers Reference

This document serves as a permanent reference for maintainers regarding external extraction providers, verified API endpoints, request flows, authentication mechanisms, and known limitations.

## Overview

Milestone M3 introduced an external extraction provider layer (`media_bot_v2/providers/`) with adaptive health tracking and automatic fallback. When users request media downloads, the bot tries active providers in priority order before falling back to local extractor engines (e.g. yt-dlp).

### Priority Order by Platform

- **TikTok:**
  1. `tikwm`
  2. `tikdownloader`
  3. `musicaldown`
  4. `cobalt` (if instance configured)
  5. Local engine (`yt-dlp`)
- **YouTube:**
  1. Local engine (`yt-dlp`) (preferred for quality selection, cookies, and PO token support)
  2. `ytmp3` (fallback for videos without commercial music copyright restrictions)
  3. `cobalt` (fallback if instance configured)
- **Instagram:**
  1. Local engine only (external scrapers fail reliably against Meta bot-defenses). Provider architecture leaves room for future additions.

---

## 1. TikWM

- **Platform:** TikTok
- **Base Endpoint:** `GET https://www.tikwm.com/api/?url=<URL>&hd=1`
- **Authentication:** None required. No API key needed.
- **Empirically Verified:** High reliability, response time ~0.9s, direct CDN link active and streamable.

### Request

```http
GET /api/?url=https%3A%2F%2Fwww.tiktok.com%2F%40user%2Fvideo%2F7123456789&hd=1 HTTP/1.1
Host: www.tikwm.com
User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 ...
```

### Response Schema

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "id": "7123456789",
    "title": "Video title here",
    "play": "https://www.tikwm.com/video/media/play/...",
    "hdplay": "https://www.tikwm.com/video/media/hdplay/...",
    "wmplay": "https://www.tikwm.com/video/media/wmplay/...",
    "music": "https://www.tikwm.com/video/music/...",
    "images": [
      "https://p16-sign.tiktokcdn.com/obj/photo1.jpg",
      "https://p16-sign.tiktokcdn.com/obj/photo2.jpg"
    ],
    "size": 1234567,
    "hd_size": 2345678
  }
}
```

### Extraction Logic

- If `data.images` is a non-empty array: treat as photo slideshow (`media_type="photo"`).
- Otherwise: video (`media_type="video"`). Prefer `hdplay`, fall back to `play`, then `wmplay`.
- Size hint from `data.hd_size` or `data.size`.

---

## 2. tikdownloader.io

- **Platform:** TikTok
- **Base Endpoint:** `POST https://tikdownloader.io/api/ajaxSearch`
- **Authentication:** None. Requires AJAX headers (`X-Requested-With` and `Referer`).
- **Empirically Verified:** Returns HTTP 200 with HTML payload containing `dl.snapcdn.app` JWT download links. The JWT returns HTTP 302 to TikTok's CDN.

### Request

```http
POST /api/ajaxSearch HTTP/1.1
Host: tikdownloader.io
User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 ...
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
X-Requested-With: XMLHttpRequest
Referer: https://tikdownloader.io/

q=https%3A%2F%2Fwww.tiktok.com%2F%40user%2Fvideo%2F7123456789&lang=en
```

### Response Schema

```json
{
  "status": "ok",
  "data": "<div class=\"video-info\"><h3>Title</h3><a href=\"https://dl.snapcdn.app/get?token=eyJ...\">Download MP4 HD</a></div>"
}
```

### Extraction Logic

- Status must equal `"ok"`.
- Parse HTML `<a>` tags for `href` containing `https://dl.snapcdn.app/get?token=`.
- Prefer HD / Without Watermark links.
- Title extracted from `<h3 class="title">` or similar heading.

---

## 3. musicaldown.com

- **Platform:** TikTok
- **Endpoints:**
  - `GET https://musicaldown.com/en`
  - `POST https://musicaldown.com/download`
- **Special Requirement:** Rotating form field names.
  The site randomizes the input field name (e.g. `k_exp_url`, `url_hash`, or random string) on every page render. A hidden verification/token field is also injected.
- **Empirically Verified:** Session cookies must be preserved from the initial GET to the subsequent POST. The POST returns HTML containing `fastdl.muscdn.app` media links.

### Flow

1. **Step 1 (GET):** Request `https://musicaldown.com/en` with a `requests.Session`.
   Parse the HTML to locate:
   - All `<input type="hidden">` name/value pairs.
   - The text/url `<input>` element to determine its dynamic name.
2. **Step 2 (POST):** Submit form data to `https://musicaldown.com/download` using the same session:
   - All parsed hidden fields.
   - `{dynamic_input_name: target_tiktok_url}`
   - Header `Referer: https://musicaldown.com/en`.
3. **Step 3 (Parse):** Extract `<a href="...">` matching `fastdl.muscdn.app` or `muscdn.app`.

---

## 4. ytmp3.gl (gamma.gammacloud.net flow)

- **Platform:** YouTube
- **Base Domain:** `gamma.gammacloud.net`
- **Headers:** `Referer: https://ytmp3.gl/` required on all requests.
- **Embedded API Key:** `9b0ed5dab31616027ad7154140b0272d` (embedded in ytmp3.gl homepage script).
- **Limitation:** Rejects videos with commercial music / copyright claims (returns error code e.g. 215 or 403). Not a universal extractor for every video, but effective as a fast secondary fallback for non-commercial content.

### Flow (4 GET Requests)

1. **Authorization:**
   ```http
   GET https://gamma.gammacloud.net/api/v1/auth?api_key=9b0ed5dab31616027ad7154140b0272d&_=<timestamp>
   Referer: https://ytmp3.gl/
   ```
   Returns: `{"error": 0, "key": "<bearer_token>"}`

2. **Initialization:**
   ```http
   GET https://gamma.gammacloud.net/api/v1/init?_=<timestamp>
   Authorization: Bearer <bearer_token>
   Referer: https://ytmp3.gl/
   ```
   Returns: `{"error": 0, "convertURL": "<convert_endpoint>"}`

3. **Convert:**
   ```http
   GET <convert_endpoint>&v=<video_id>&f=mp4&_=<timestamp>
   Referer: https://ytmp3.gl/
   ```
   Returns either `downloadURL`, `progressURL`, or `error`. If `error > 0`, raises copyright rejection.

4. **Progress / Download:**
   If `progressURL` was returned, poll every 1s until `status == "download"` or `progress >= 3`.
   Final download link format:
   `<downloadURL>&v=<video_id>&f=mp4&r=ytmp3.gl`
   Direct streaming of the media file must pass header `Referer: https://ytmp3.gl/`.

---

## 5. Cobalt Instances

- **Platforms:** YouTube, TikTok (and others)
- **Endpoint:** `POST <COBALT_URL>` (or `<COBALT_URL>/api/json`)
- **Headers:**
  ```http
  Accept: application/json
  Content-Type: application/json
  ```
- **Payload:**
  ```json
  { "url": "<target_url>" }
  ```
- **Response Handling:**
  - `status: "tunnel"` / `"stream"` / `"redirect"`: media URL in `data.url`.
  - `status: "picker"`: photo or item array in `data.picker`.
  - `status: "error"`: error details in `data.error.code`.
- **Note:** Only instances without Cloudflare Turnstile are usable programmatically without interactive browser sessions. The instance URL is configurable via `COBALT_URL`. If unset, the Cobalt provider is automatically skipped.

---

## 6. Services Verified as Incompatible / Broken

During empirical research, the following public web services were thoroughly tested and determined unusable:

| Service | Failure Reason |
|---|---|
| y2mate | Dead domain / blocked redirect |
| 9convert | Seized by IFPI / offline |
| yt5s | Discontinued YouTube extraction service |
| tikmate | Parked domain |
| ssstik | Cloudflare Bot Management returns empty response body to automated agents |
| snaptik | Requires complex encrypted challenge token per request |
| savefrom | Demands signed cryptographic token and CAPTCHA challenge |

These services must not be implemented, nor should workarounds be attempted against anti-bot challenges.
