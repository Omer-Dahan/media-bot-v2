# Architectural Lessons Learned from the Legacy Bot

This document details the eight critical failure patterns identified in the production logs of the legacy downloader bot (`media-downloader-bot`), the defensive mechanisms implemented in `media-bot-v2` to prevent each issue, and the exact files and automated tests that verify each fix.

---

## Failure Mapping Matrix

| # | Legacy Bot Failure Pattern | v2 Prevention Mechanism | Implementation Files | Verification Tests |
|---|---|---|---|---|
| 1 | **Direct file entered wrong track:** 100GB `.zim` file routed to `yt-dlp` by default, failed with `format: None`, fell back to `gallery-dl` and `JDownloader`, consuming 20 minutes without diagnosing the real cause. | **Explicit capability declaration & pre-filtering:** Every engine and provider declares matching rules and supported platforms. Non-media direct links bypass YouTube/TikTok extractors and route straight to `DirectEngine`. Incompatible URLs fail immediately with `UnsupportedUrlError`. | `media_bot_v2/engines/base.py`<br>`media_bot_v2/engines/direct.py`<br>`media_bot_v2/engines/youtube.py`<br>`media_bot_v2/engines/tiktok.py`<br>`media_bot_v2/telegram/router.py` | `tests/test_lessons.py::test_lesson1_zim_link_matches_direct_engine_only`<br>`tests/test_lessons.py::test_lesson1_non_matching_url_rejected_by_engines_without_calling_ytdlp` |
| 2 | **Size check deferred until download starts:** Files were streamed to disk before discovering they exceeded limits. | **Preflight HEAD / Content-Length inspection:** `DirectEngine` performs a `HEAD` request before opening any `GET` stream. If declared `Content-Length` exceeds `max_download_size`, execution aborts immediately before allocating disk buffers, providing an exact Hebrew notification. | `media_bot_v2/engines/direct.py`<br>`media_bot_v2/telegram/texts.py`<br>`media_bot_v2/engines/base.py` | `tests/test_lessons.py::test_lesson2_direct_oversized_zim_rejected_on_head_before_get`<br>`tests/test_direct_engine.py::test_download_rejects_declared_content_length_before_writing_any_bytes` |
| 3 | **Duplicate attempts of same route:** JDownloader entered twice in a row for the same link, getting stuck 10 minutes each time. | **Strict single-attempt enforcement:** Candidate providers are deduplicated and tracked via `attempted_providers` set per request. Each provider or local engine route is attempted at most once per task, preventing retry loops. | `media_bot_v2/engines/youtube.py`<br>`media_bot_v2/engines/tiktok.py` | `tests/test_lessons.py::test_lesson3_failed_provider_tried_only_once_per_request` |
| 4 | **User left uninformed upon failure:** Detailed errors were logged internally, but the user received an opaque generic failure message. | **User-facing route attempt summary:** `RouteAttemptTracker` collects concise Hebrew failure reasons across all attempted routes (providers and local engine). When all candidates fail, the user receives an itemized summary showing which routes were tried and why they failed. | `media_bot_v2/engines/base.py`<br>`media_bot_v2/telegram/texts.py`<br>`media_bot_v2/engines/youtube.py`<br>`media_bot_v2/engines/tiktok.py`<br>`media_bot_v2/pipeline.py` | `tests/test_lessons.py::test_lesson4_failure_message_includes_routes_and_reasons`<br>`tests/test_lessons.py::test_lesson4_youtube_failure_message_includes_routes_and_reasons` |
| 5 | **Misleading error diagnostics:** `yt-dlp` was blamed for "outdated version / no formats" when the actual cause was an unsupported URL, oversized file, or missing JS runtime. | **Granular error classification:** `classify_youtube_error` cleanly distinguishes missing JS runtimes (Node.js/Deno), unsupported link types, PO token enforcement, and oversized downloads. Outdated version claims are eliminated. | `media_bot_v2/engines/youtube.py`<br>`media_bot_v2/telegram/texts.py` | `tests/test_lessons.py::test_lesson5_accurate_error_classification`<br>`tests/test_youtube_engine.py::test_classify_youtube_error_js_runtime_missing` |
| 6 | **Irrelevant routes executed & false failures:** `gallery-dl` attempted on unsupported URLs; `aria2` reported as failed when `aria2c` was not installed in PATH. | **Pre-execution availability and capability checks:** Providers are checked via `provider.matches(url)` before execution. Incompatible or unconfigured providers are skipped with structured log entries, never invoked, and never reported as failed to health tracking or the user. | `media_bot_v2/engines/youtube.py`<br>`media_bot_v2/engines/tiktok.py`<br>`media_bot_v2/providers/registry.py` | `tests/test_lessons.py::test_lesson6_incompatible_provider_skipped_without_recording_failure` |
| 7 | **Silent playlist trimming:** Playlists were truncated by credit limits without explicitly telling the user how many items were downloaded out of total. | **Explicit trimming notification:** When a playlist is limited by credit allowance, `DownloadResult` captures `playlist_total` and `playlist_downloaded`. The completion message states exactly how many items were delivered out of total. | `media_bot_v2/engines/base.py`<br>`media_bot_v2/engines/youtube.py`<br>`media_bot_v2/pipeline.py`<br>`media_bot_v2/telegram/texts.py` | `tests/test_lessons.py::test_lesson7_playlist_trimming_reports_downloaded_out_of_total` |
| 8 | **No request time budget:** Single requests could hang indefinitely (up to 20 minutes) across cascading engine attempts. | **Per-request timeout budget and cleanup:** Configurable `REQUEST_TIMEOUT` (default: 600s / 10 minutes) enforced via `asyncio.timeout` across the entire pipeline. On expiry, execution halts, the user is notified of timeout cancellation, and all temporary files are purged. | `media_bot_v2/config.py`<br>`media_bot_v2/pipeline.py`<br>`media_bot_v2/telegram/texts.py`<br>`media_bot_v2/bootstrap.py` | `tests/test_lessons.py::test_lesson8_request_timeout_stops_reports_and_cleans_up` |

---

## Detailed Architectural Implementations

### 1. Capability Pre-Filtering and Correct Routing
- **Problem in legacy bot:** The legacy bot treated `yt-dlp` as a catch-all sink. Non-media URLs such as `.zim` dumps or arbitrary file links entered `yt-dlp`, which spent minutes attempting format extraction, failed, and triggered cascading fallbacks.
- **v2 Solution:**
  - `BaseEngine` and `BaseProvider` contracts enforce `matches(url: str) -> bool`.
  - `DirectEngine` handles HTTP/HTTPS direct downloads and rejects known streaming domains (`youtube.com`, `tiktok.com`, `instagram.com`).
  - `YouTubeEngine` and `TikTokEngine` verify host patterns before processing and raise `UnsupportedUrlError` immediately if invoked on foreign URLs.
  - The Telegram router pre-filters incoming messages: non-HTTP/HTTPS URI schemes (`ftp://`, `magnet:`, `torrent:`) are rejected at the gate without invoking any engine or background task.

### 2. Preflight Size Check via HEAD
- **Problem in legacy bot:** Large direct files were downloaded chunk by chunk before checking whether they complied with Telegram limits or bot storage limits.
- **v2 Solution:**
  - `DirectEngine` performs a `HEAD` request with timeout before opening a streaming `GET`.
  - If `Content-Length` exceeds `max_download_size`, `DownloadTooLargeError` is raised immediately.
  - The user receives an informative Hebrew message:
    `"❌ הקובץ גדול מדי (100.0GB). מגבלת ההורדה המרבית היא 4.0GB. טלגרם אינה תומכת בהעברת קבצים בגודל כזה."`
  - Zero bytes are written to disk and no fallback engines are called.

### 3. Single Attempt Per Provider / Route
- **Problem in legacy bot:** Duplicate retries of the same slow provider (e.g. JDownloader) occurred sequentially, multiplying timeout delays.
- **v2 Solution:**
  - In `YouTubeEngine._try_fallback_providers` and `TikTokEngine.download`, an in-memory set `attempted_providers` records tried provider names.
  - If a provider is listed multiple times in the registry or fallback order, it is executed once only.
  - If a route fails, the task advances to the next candidate without restarting failed routes.

### 4. Transparent User-Facing Attempt Summary
- **Problem in legacy bot:** When all fallback mechanisms failed, the user received an opaque error like "Download failed", hiding what actually happened.
- **v2 Solution:**
  - `RouteAttemptTracker` records each attempted provider/engine and a concise Hebrew reason for failure (e.g., `"ספק אינו מוגדר"`, `"לא נמצאה כתובת להורדה"`, `"שגיאת שרת של הספק"`, `"סרטון דורש התחברות או אימות"`).
  - The final exception surfaces this breakdown in the progress message:
    ```
    ❌ ההורדה נכשלה. נסה שוב או שלח קישור אחר.
    פירוט הניסיונות:
    • TikWM: לא נמצאה כתובת להורדה
    • MusicalDown: שגיאת שרת של הספק
    • מנוע מקומי (yt-dlp): סרטון דורש התחברות או אימות
    ```

### 5. Accurate Diagnostics (No Outdated Version Blame)
- **Problem in legacy bot:** Format extraction failures or missing external binaries were erroneously classified as "outdated yt-dlp version".
- **v2 Solution:**
  - `classify_youtube_error` accurately maps JavaScript runtime errors (e.g. Node.js or Deno missing for n-challenge decryption) to `texts.YOUTUBE_JS_RUNTIME_MISSING`.
  - Unsupported URLs are explicitly identified as `texts.UNSUPPORTED_URL`.
  - Oversized downloads raise `DownloadTooLargeError` with exact capacity comparisons.
  - Bogus version warnings are completely eliminated.

### 6. Skipping Irrelevant Routes Without False Failures
- **Problem in legacy bot:** `gallery-dl` ran on URLs it had no extractor for, and `aria2` was reported as failed when the executable was absent from PATH.
- **v2 Solution:**
  - Providers verify `matches(url)` before fetch. Incompatible providers are skipped and logged at `INFO` level.
  - `ProviderHealthTracker.record_failure()` is only called when a capable provider actually encounters an error during execution.
  - Skipped providers are never included in the user-facing failure summary.

### 7. Explicit Item Count on Playlist Trimming
- **Problem in legacy bot:** When playlists were truncated to match available user credits, the user was left in the dark about how many items were omitted.
- **v2 Solution:**
  - `YouTubeEngine` and `DownloadResult` preserve both `playlist_total` and `playlist_downloaded`.
  - If `playlist_downloaded < playlist_total`, `DownloadPipeline` reports:
    `"הושלם ✅\nהורדו {downloaded} מתוך {total} פריטים בפלייליסט (ההורדה הוגבלה לפי יתרת הקרדיטים)."`
  - Users know exactly what was delivered and why it was truncated.

### 8. Total Request Time Budget and File Cleanup
- **Problem in legacy bot:** Requests had no overall time budget and could run for 20+ minutes when multiple slow engines timed out sequentially.
- **v2 Solution:**
  - `Settings.request_timeout` sets an overall time ceiling (default: 600.0s).
  - `DownloadPipeline.run` executes inside `asyncio.timeout(self._request_timeout)`.
  - If the timeout expires:
    1. Execution cancels cleanly.
    2. User receives `texts.REQUEST_TIMEOUT_EXCEEDED`:
       `"⏱️ הבקשה בוטלה עקב חריגה ממגבלת הזמן (timeout). נסה שוב מאוחר יותר או הורד קובץ קטן יותר."`
    3. The `finally: self._cleanup(task_dir)` block deletes all in-flight files from disk.
    4. No credits or bandwidth are deducted.
