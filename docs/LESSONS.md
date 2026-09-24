# Architectural Lessons Learned from the Legacy Bot

This document details the eight critical failure patterns identified in the production logs of the legacy downloader bot (`media-downloader-bot`), the defensive mechanisms implemented in `media-bot-v2` to prevent each issue, and the exact files and automated tests that verify each fix.

---

## Failure Mapping Matrix

| # | Legacy Bot Failure Pattern | v2 Prevention Mechanism | Implementation Files | Verification Tests |
|---|---|---|---|---|
| 1 | **Direct file entered wrong track:** 100GB `.zim` file routed to `yt-dlp` by default, failed with `format: None`, fell back to `gallery-dl` and `JDownloader`, consuming 20 minutes without diagnosing the real cause. HTML web pages downloaded and charged as media. | **Strict host-only parsing, capability pre-filtering & Content-Type validation:** Domain matching parses URL `netloc` and checks host and subdomains only—query params (`?ref=youtube.com`) or prefix domains (`notyoutube.com`) never trigger streaming extractors. `DirectEngine` validates `Content-Type` on HEAD/GET and sniffs initial bytes to immediately reject HTML pages (`UnsupportedUrlError`). | `media_bot_v2/engines/base.py`<br>`media_bot_v2/engines/direct.py`<br>`media_bot_v2/engines/youtube.py`<br>`media_bot_v2/engines/tiktok.py`<br>`media_bot_v2/telegram/router.py` | `tests/test_lessons.py::test_lesson1_zim_link_matches_direct_engine_only`<br>`tests/test_lessons.py::test_lesson1_non_matching_url_rejected_by_engines_without_calling_ytdlp`<br>`tests/test_lessons.py::test_m4_1_finding5_host_only_matching_rejects_query_and_prefix_spoofs`<br>`tests/test_lessons.py::test_m4_1_finding10_direct_engine_rejects_html_content_type` |
| 2 | **Size check deferred until download starts:** Files were streamed to disk before discovering they exceeded limits. | **Preflight HEAD / Content-Length inspection:** `DirectEngine` performs a `HEAD` request before opening any `GET` stream. If declared `Content-Length` exceeds `max_download_size`, execution aborts immediately before allocating disk buffers, providing an exact Hebrew notification. | `media_bot_v2/engines/direct.py`<br>`media_bot_v2/telegram/texts.py`<br>`media_bot_v2/engines/base.py` | `tests/test_lessons.py::test_lesson2_direct_oversized_zim_rejected_on_head_before_get`<br>`tests/test_direct_engine.py::test_download_rejects_declared_content_length_before_writing_any_bytes` |
| 3 | **Duplicate attempts of same route:** JDownloader entered twice in a row for the same link, getting stuck 10 minutes each time. | **Strict single-attempt enforcement:** Candidate providers are deduplicated and tracked via `attempted_providers` set per request. In `YouTubeEngine`, default `max_retries = 0` guarantees the local route is attempted at most once at orchestration level (transport-level transient socket drops are handled internally by yt-dlp `retries=3, fragment_retries=3`). | `media_bot_v2/engines/youtube.py`<br>`media_bot_v2/engines/tiktok.py` | `tests/test_lessons.py::test_lesson3_failed_provider_tried_only_once_per_request`<br>`tests/test_lessons.py::test_m4_1_finding6_local_route_default_max_retries_is_zero` |
| 4 | **User left uninformed upon failure / internal leaks:** Detailed errors were either hidden behind generic messages or leaked raw server paths (`/srv/media/tmp/...`) and internal hostnames to users. | **User-facing route attempt summary without internal leaks:** `RouteAttemptTracker` collects concise Hebrew failure reasons across all attempted routes. Internal server paths, IP addresses, and private hostnames are suppressed from user notifications and logged safely. `summarize_ytdlp_failure` accurately classifies both English source errors and translated strings. | `media_bot_v2/engines/base.py`<br>`media_bot_v2/telegram/texts.py`<br>`media_bot_v2/engines/youtube.py`<br>`media_bot_v2/engines/tiktok.py`<br>`media_bot_v2/pipeline.py` | `tests/test_lessons.py::test_lesson4_failure_message_includes_routes_and_reasons`<br>`tests/test_lessons.py::test_lesson4_youtube_failure_message_includes_routes_and_reasons`<br>`tests/test_lessons.py::test_m4_1_finding2_summarize_ytdlp_failure_preserves_english_and_handles_hebrew`<br>`tests/test_lessons.py::test_m4_1_finding4_no_internal_path_or_host_leak_in_error_messages` |
| 5 | **Misleading error diagnostics & raw oversized errors:** `yt-dlp` was blamed for "outdated version" or dumped raw English error strings with incorrect limits. | **Granular error classification & unified Hebrew sizing:** `classify_youtube_error` cleanly distinguishes missing JS runtimes (Node.js/Deno), unsupported link types, PO token enforcement, and oversized downloads. `_DownloadTooLargeSignal` captures actual file size and max limit, formatting uniformly via `texts.format_download_too_large` in human units across all engines. | `media_bot_v2/engines/youtube.py`<br>`media_bot_v2/engines/instagram.py`<br>`media_bot_v2/telegram/texts.py` | `tests/test_lessons.py::test_lesson5_accurate_error_classification`<br>`tests/test_youtube_engine.py::test_classify_youtube_error_js_runtime_missing`<br>`tests/test_lessons.py::test_m4_1_finding3_youtube_oversized_uses_exact_hebrew_and_real_limit` |
| 6 | **Irrelevant routes executed & false failures:** `gallery-dl` attempted on unsupported URLs; `aria2` reported as failed when `aria2c` was not installed in PATH. | **Pre-execution availability and capability checks:** Providers are checked via `provider.matches(url)` before execution. Incompatible or unconfigured providers are skipped with structured log entries, never invoked, and never reported as failed to health tracking or the user. | `media_bot_v2/engines/youtube.py`<br>`media_bot_v2/engines/tiktok.py`<br>`media_bot_v2/providers/registry.py` | `tests/test_lessons.py::test_lesson6_incompatible_provider_skipped_without_recording_failure` |
| 7 | **Silent playlist trimming & split upload failures:** Playlists were truncated by credit limits without stating item counts or attributed all trimming to credits; multi-part uploads charged nothing on late failure. | **Explicit trimming reason & incremental part delivery:** `DownloadResult` preserves `playlist_total`, `playlist_downloaded`, and `playlist_trimmed_reason` (distinguishing credit limit truncation from unavailable/deleted items). For multi-part uploads, credits and bandwidth are charged immediately per delivered part, and delivered parts are saved to cache. | `media_bot_v2/engines/base.py`<br>`media_bot_v2/engines/youtube.py`<br>`media_bot_v2/pipeline.py`<br>`media_bot_v2/telegram/texts.py` | `tests/test_lessons.py::test_lesson7_playlist_trimming_reports_downloaded_out_of_total`<br>`tests/test_lessons.py::test_m4_1_finding7_multipart_upload_failure_charges_and_caches_delivered_part`<br>`tests/test_lessons.py::test_m4_1_finding9_playlist_trimming_reasons` |
| 8 | **No request time budget & zombie downloads:** Requests hung indefinitely; coroutine cancellation did not stop active thread socket transfers. | **Independent budgets & active thread cooperative cancellation:** Separate `download_timeout` and `upload_timeout` prevent upload starvation. Thread-safe `CancellationToken` aborts active downloads in worker threads (`response.close()` on HTTP streams and raising `_DownloadCancelledSignal` in yt-dlp progress hooks). Foreign `TimeoutError` exceptions are not mistaken for budget timeouts. Verified with real atomic byte counters. | `media_bot_v2/config.py`<br>`media_bot_v2/pipeline.py`<br>`media_bot_v2/engines/base.py`<br>`media_bot_v2/engines/direct.py`<br>`media_bot_v2/engines/youtube.py`<br>`media_bot_v2/telegram/texts.py` | `tests/test_lessons.py::test_lesson8_active_transfer_actually_stops_on_timeout_with_byte_counter`<br>`tests/test_lessons.py::test_lesson8_ytdlp_progress_hook_raises_cancelled_signal_on_cancel_token`<br>`tests/test_lessons.py::test_m4_1_finding8_foreign_timeout_error_reports_download_failed_not_budget` |

---

## Detailed Architectural Implementations

### 1. Capability Pre-Filtering, Host Parsing and Content-Type Validation
- **Problem in legacy bot:** The legacy bot treated `yt-dlp` as a catch-all sink. Non-media URLs such as `.zim` dumps or query-string mimics (`files.com?ref=youtube.com`) entered `yt-dlp`, consuming resources. HTML web pages were saved and charged as files.
- **v2 Solution:**
  - Strict host-only matching: all routing (`matches_youtube_url`, `matches_tiktok_url`, `router._host_matches`) parses `urlparse(url).netloc` and checks only host domain and subdomains.
  - `DirectEngine` inspects `Content-Type` on `HEAD` and `GET`. Rejects `text/html` and `application/xhtml+xml`, and sniffs initial payload bytes for HTML doctype tags, raising `UnsupportedUrlError` before storing files.
  - Non-HTTP/HTTPS URI schemes (`ftp://`, `magnet:`, `torrent:`) are rejected at the gate.

### 2. Preflight Size Check via HEAD
- **Problem in legacy bot:** Large direct files were downloaded chunk by chunk before checking whether they complied with Telegram limits or bot storage limits.
- **v2 Solution:**
  - `DirectEngine` performs a `HEAD` request with timeout before opening a streaming `GET`.
  - If `Content-Length` exceeds `max_download_size`, `DownloadTooLargeError` is raised immediately.
  - The user receives an informative Hebrew message:
    `"⚠️ הקובץ גדול מדי (100.0GB). מגבלת ההורדה המרבית היא 4.0GB. טלגרם אינה תומכת בהעברת קבצים בגודל כזה."`
  - Zero bytes are written to disk and no fallback engines are called.

### 3. Single Attempt Per Provider / Route
- **Problem in legacy bot:** Duplicate retries of the same slow provider or local engine occurred sequentially, multiplying timeout delays.
- **v2 Solution:**
  - In `YouTubeEngine._try_fallback_providers` and `TikTokEngine.download`, an in-memory set `attempted_providers` records tried provider names.
  - At the route orchestration level, `YouTubeEngine` has default `max_retries = 0`: the local route is attempted at most once before falling back to providers.
  - Transport-level transient network errors (e.g. dropped chunks or connection resets) are handled strictly inside yt-dlp via `retries=3, fragment_retries=3`.

### 4. Transparent User-Facing Attempt Summary (No Internal Leaks)
- **Problem in legacy bot:** When all fallback mechanisms failed, the user received an opaque error like "Download failed", or internal server paths (`/srv/media/tmp/abc123.part`) leaked to the UI.
- **v2 Solution:**
  - `RouteAttemptTracker` records each attempted provider/engine and a concise Hebrew reason for failure.
  - All error messages are sanitized: any message containing server paths (`/srv/`, `/tmp/`, `/home/`), IP addresses, or internal hostnames is suppressed from the user and logged safely with `logger.warning`.
  - `summarize_ytdlp_failure` inspects the original English error before translation (as well as translated Hebrew fallback) to correctly summarize failures like missing JS runtimes.

### 5. Accurate Diagnostics and Unified Sizing
- **Problem in legacy bot:** Format extraction failures were erroneously blamed on "outdated yt-dlp version", and size limits in YouTube surfaced raw English errors with incorrect byte numbers.
- **v2 Solution:**
  - `classify_youtube_error` accurately maps JavaScript runtime errors (Node.js/Deno missing for n-challenge decryption) to `texts.YOUTUBE_JS_RUNTIME_MISSING`.
  - `_DownloadTooLargeSignal` captures actual byte sizes and formats them into Hebrew via `texts.format_download_too_large` with actual human units (GB/MB) matching direct downloads.

### 6. Skipping Irrelevant Routes Without False Failures
- **Problem in legacy bot:** `gallery-dl` ran on URLs it had no extractor for, and `aria2` was reported as failed when the executable was absent from PATH.
- **v2 Solution:**
  - Providers verify `matches(url)` before fetch. Incompatible providers are skipped and logged at `INFO` level.
  - `ProviderHealthTracker.record_failure()` is only called when a capable provider actually encounters an error during execution.
  - Skipped providers are never included in the user-facing failure summary.

### 7. Explicit Item Count on Playlist Trimming & Incremental Split Uploads
- **Problem in legacy bot:** Trimming reasons were unstated or attributed exclusively to credit depletion even when items failed or were private; in multi-part uploads, if part 2 failed, the user was charged nothing and part 1 was lost.
- **v2 Solution:**
  - `YouTubeEngine` records `playlist_trimmed_reason`: distinguishes between reaching `playlist_item_limit` (`"ההורדה הוגבלה לפי יתרת הקרדיטים"`) versus unavailable items (`"חלק מהפריטים אינם זמינים או נכשלו"`).
  - Multi-part split files charge credits and bandwidth per delivered part immediately upon successful delivery, and incrementally store delivered parts in cache.

### 8. Total Request Time Budget, Separate Upload Timeout, and Active Thread Cancellation
- **Problem in legacy bot:** Requests had no overall time budget. In v2, coroutine cancellation via `asyncio.to_thread` did not kill running threads, allowing background byte transfers to continue consuming network bandwidth after cancellation.
- **v2 Solution:**
  - Independent budgets: `download_timeout` and `upload_timeout` (both defaulting to `request_timeout`) ensure heavy uploads are not starved by download duration.
  - Active cooperative cancellation: `CancellationToken` coordinates thread termination. In `DirectEngine` and provider downloader, `cancel_token.on_cancel(response.close)` immediately closes the underlying socket, breaking out of streaming loops. In `YouTubeEngine`, the progress hook checks `cancel_token.is_set()` and raises `_DownloadCancelledSignal` to abort yt-dlp immediately.
  - Distinguishing budget timeouts: only when the pipeline context manager actually expired (`cm.expired() == True`) is `texts.REQUEST_TIMEOUT_EXCEEDED` reported; foreign socket/HTTP `TimeoutError` exceptions report `texts.DOWNLOAD_FAILED`.
  - Verified by tests using simulated slow streaming sources with atomic byte counters showing byte transfer halts immediately upon cancellation.
