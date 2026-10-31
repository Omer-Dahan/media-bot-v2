# M3U8 / HLS Download Support Plan

> Plan only. No production code or tests are changed by this document.
> Baseline: `main` at `b3b4ff1d82246b83cb6d82951fa83be38f457d7c`.
> Evidence note: GitNexus has no index for this repository and `graphify-out/graph.json` is absent. Creating either would violate the one-file constraint, so architecture and impact findings below are source-derived and pinned to the baseline commit.

## 1. Recommendation

Implement HLS as a mode inside `DirectEngine`, backed by a new, small HLS module that owns playlist parsing and every HTTP fetch. It should fetch the master/media playlists, selected variants, audio renditions, initialization segments, AES-128 keys, and media segments through `ssrf_guard.safe_request`, write those resources into the task directory, rewrite all playlist references to local relative paths, and invoke ffmpeg only against the local media playlist. ffmpeg should remux with stream copy to MP4 and emit machine-readable progress. The existing pipeline then probes the result, calls `ensure_streamable` when it is sent as video, splits files over Telegram's limit, uploads, and charges by delivered bytes.

This is a controlled-fetch/native strategy with ffmpeg used only as a local decrypt/remux tool. Do not give ffmpeg or yt-dlp the remote playlist URL. Their internal network fetches do not pass through this repository's redirect-by-redirect SSRF guard.

Initial scope:

- Support finite VOD media playlists and master playlists whose selected path ends in a media playlist.
- Treat a media playlist with `EXT-X-ENDLIST` as finite VOD. Treat one without it as live, snapshot the segments listed at fetch time, and deliver that available window as a normal VOD file. Forward recording and continuous capture are out of scope.
- Support MPEG-TS and fMP4/CMAF, including `EXT-X-MAP`.
- Support clear streams and AES-128 identity-key encryption.
- Reject SAMPLE-AES and commercial DRM with classified, non-retryable errors.
- Use the direct-link route and cache namespace. Do not add a generic yt-dlp fallback.

## 2. What breaks today

### URL ending in `.m3u8`

1. The router classifies every host outside the four platform host sets as `direct` (`media_bot_v2/telegram/router.py:102-115`) and constructs `DirectEngine` (`media_bot_v2/telegram/router.py:462-464`).
2. `DirectEngine.matches` accepts any HTTP(S) URL outside `_KNOWN_PLATFORM_DOMAINS`; it does not inspect the path extension (`media_bot_v2/engines/direct.py:46-58`, `media_bot_v2/engines/direct.py:154-163`).
3. The best-effort HEAD request calls `reject_if_not_media_content_type` (`media_bot_v2/engines/direct.py:253-271`). That validator rejects `text/*`, selected document types, and `+json`/`+xml`, but not `application/vnd.apple.mpegurl` (`media_bot_v2/engines/content_check.py:20-27`, `media_bot_v2/engines/content_check.py:53-63`).
4. GET repeats the same header check, chooses a filename from `Content-Disposition` or the final URL, opens that path, and streams response bytes into it (`media_bot_v2/engines/direct.py:274-337`). The body sniffer only recognizes markup and JSON; `#EXTM3U` is not rejected (`media_bot_v2/engines/content_check.py:83-99`).
5. Therefore, today the bot silently saves the playlist text itself as a file named like `index.m3u8`, returns it as a successful `DownloadResult`, and never downloads the referenced media (`media_bot_v2/engines/direct.py:194-196`, `media_bot_v2/engines/direct.py:311-361`). The pipeline will subsequently probe it and normally treat it as a non-video document (`media_bot_v2/pipeline.py:323-410`).

### HLS MIME type with no extension

The route and validation are the same. The HLS MIME type is accepted by the header validator, while `extension_for_content_type` has no HLS override and only derives extensions for image/audio/video MIME families (`media_bot_v2/engines/content_check.py:102-141`). If no `Content-Disposition` or final-URL suffix supplies a name, `_filename_from_response` falls back to `.bin` (`media_bot_v2/engines/direct.py:227-250`). The playlist body is then silently saved as that file. The exact extension chosen for this MIME by the installed `filetype` registry was not verified; the `.bin` statement applies when that registry returns no type.

## 3. Detection before any body is written

Detection belongs in `DirectEngine._preflight_and_stream_to_file`, before `_stream_to_file` creates the destination file (`media_bot_v2/engines/direct.py:253-282`). Keep one catch-all engine and branch to the HLS helper after detection.

Use these signals in order:

1. **Final URL path extension:** case-insensitive `.m3u8` on the original URL or the final URL reached by HEAD is an HLS candidate. Query strings do not count as path extensions.
2. **HEAD content type:** recognize normalized `application/vnd.apple.mpegurl`, `application/x-mpegURL`, `audio/mpegurl`, and `audio/x-mpegurl`. A recognized HLS MIME type overrides the current generic media acceptance path.
3. **GET content type:** repeat the MIME check after redirects because GET may land elsewhere or disagree with HEAD.
4. **Buffered body signature:** when extension and headers are inconclusive, read a bounded prefix from GET without opening a destination file. Strip an optional UTF-8 BOM and leading ASCII whitespace; `#EXTM3U` at the start selects HLS. Preserve the prefix and remaining response body for playlist parsing rather than issuing an unnecessary second GET.

Precedence:

- The existing scheme/host match and `_KNOWN_PLATFORM_DOMAINS` exclusion remain first (`media_bot_v2/engines/direct.py:154-163`).
- `safe_request` remains mandatory before trusting any final URL or header (`media_bot_v2/engines/ssrf_guard.py:73-105`).
- Declared-size checks remain useful for ordinary direct files, but an HLS playlist's `Content-Length` describes only the manifest. Do not use it as the media total.
- HLS detection must run before `reject_if_not_media_body` and before any destination media file is opened. A body that is neither HLS nor rejected text continues through the existing direct-file stream using the already-buffered prefix.
- A `.m3u8` path whose body is not an `#EXTM3U` playlist fails as malformed HLS. It must not fall back to saving text as media.

## 4. Download strategy comparison

Scores are relative for this repository: 5 is best, 1 is worst.

| Criterion | ffmpeg on remote URL | yt-dlp on playlist URL | Controlled Python fetch + local ffmpeg |
|---|---:|---:|---:|
| AES-128 | 5 | 4 | 5, with ffmpeg decrypting local assets |
| fMP4/CMAF and TS | 5 | 5 | 5 |
| Master/variant selection | 2 | 5 | 4 |
| Resume | 1 | 4 | 2 initially |
| Progress granularity | 3 via `-progress` | 5 via hooks/fragments | 5 per fetch plus ffmpeg phase |
| Cancellation | 4 by terminating one child | 4 via hooks plus child cleanup | 5 across responses and child process |
| Code owned here | 5 | 4 | 2 |
| Failure diagnosis | 3 | 4 | 5 with URL class and phase recorded |
| Licensing/dependencies | 5, installed binary | 5, existing dependency | 5, standard library/requests plus installed ffmpeg |
| SSRF fit | 1 | 1 without a custom transport | 5 |

### (a) ffmpeg as a remote downloader

`ffmpeg -i <playlist> -c copy` is the smallest happy-path implementation and handles AES-128, TS, CMAF, nested playlists, and rendition muxing well. It is not acceptable as the network layer because ffmpeg would fetch every playlist, segment, redirect, and key outside `safe_request`. Protocol allowlists do not block private IP ranges or revalidate DNS/redirect destinations.

### (b) yt-dlp against the playlist URL

yt-dlp already exists and provides variant selection, fragment retries, progress hooks, and HLS handling. Its estimate is explicitly unreliable for fragmented HLS/DASH, and this repository already rejects based on observed bytes rather than a merely-over-cap estimate (`media_bot_v2/engines/ytdlp_support.py:9-24`, `media_bot_v2/engines/ytdlp_support.py:134-190`). The blocker is the same: its manifest, fragment, and key requests do not use `safe_request`. Adding a complete custom yt-dlp request handler would couple a security boundary to yt-dlp internals and still require redirect and byte-accounting tests. Do not select it for this feature.

### (c) controlled Python fetch, then local ffmpeg

This owns playlist validation, URI resolution, selection, fetch sequencing, byte accounting, and cleanup. It does not need to implement MPEG-TS/fMP4 concatenation or AES itself: after all remote resources are safely materialized and playlist URIs are rewritten to local names, ffmpeg performs local decrypt/remux. Invoke ffmpeg with a restrictive local protocol allowlist so a malformed rewrite cannot reintroduce network access.

**Recommendation:** choose (c). It is more application code, but it is the only option that directly preserves the existing SSRF contract without adding a proxy service or depending on private downloader internals.

`-c copy` preserves source codecs. It can therefore produce an MP4 containing HEVC, AV1, Opus, or another Telegram-incompatible combination. The pipeline already requires H.264 video, AAC/MP3 audio, MP4, and faststart (`media_bot_v2/upload/streamable.py:41-65`) and re-encodes only incompatible streams (`media_bot_v2/upload/streamable.py:140-160`). That fallback consumes CPU, temporary disk, and up to the existing `CONVERT_TIMEOUT` budget, default 180 seconds (`media_bot_v2/config.py:195-197`). On failure it returns the original and the pipeline may send it as a non-playable document (`media_bot_v2/upload/streamable.py:163-217`, `media_bot_v2/pipeline.py:364-376`).

## 5. Playlist behavior

### Master and media playlists

- Parse attribute lists with quoted-string awareness. Do not parse `EXT-X-STREAM-INF`, `EXT-X-MEDIA`, `EXT-X-KEY`, or `EXT-X-MAP` by splitting blindly on commas.
- A playlist with `EXT-X-STREAM-INF` is a master playlist. Select one video variant, then follow it. A playlist with segment URIs is a media playlist. Reject ambiguous/malformed mixtures.
- Limit playlist nesting and detect URL cycles. The implementation should choose and test a finite bound before coding; no bound was measured in this planning run.
- Resolve every relative URI with `urljoin` against the response's final post-redirect URL, not the originally requested URL. This follows the same final-URL principle used for direct filenames (`media_bot_v2/engines/direct.py:227-244`) and must happen before SSRF validation.
- Follow a selected URI that itself returns another playlist until a media playlist is reached. Apply the same final-URL and cycle rules at each level.

### Variant selection and the existing quality menu

The current menu is YouTube-specific: its callback is encoded as `ytq`, and its choices are 1080, 720, 480, 360, and audio (`media_bot_v2/telegram/quality_menu.py:51-72`). A direct HLS URL is not inspected until the pipeline starts, so the menu cannot be reused without moving a network preflight into router/menu handling and duplicating quota, cancellation, and SSRF lifecycle concerns.

Recommended first release: do not show a menu. Use a deterministic automatic policy that interprets the existing quality values if a quality is explicitly supplied by a future generalized callback: choose the highest variant whose declared `RESOLUTION` height is at or below the requested cap; if none is below it, choose the lowest available video variant; break ties by `BANDWIDTH`, then playlist order. With no explicit cap, default to 720p to match the router's existing YouTube default (`media_bot_v2/telegram/router.py:422-432`). If dimensions are absent, choose the highest declared `BANDWIDTH`; if bandwidth is also absent, choose the first variant and log that fallback. Do not download every variant to discover quality.

An explicit media-playlist URL is authoritative. If the URL already contains a provider-specific variant path such as `/1080p/index.m3u8` or `/720p/index.m3u8`, use that path as given. Do not guess or probe sibling directories. Apply the automatic master-playlist policy above only when the fetched document actually contains `EXT-X-STREAM-INF`.

### Audio renditions

- For a selected video variant with an `AUDIO` group, select the matching `EXT-X-MEDIA:TYPE=AUDIO` rendition. Prefer `DEFAULT=YES`, then `AUTOSELECT=YES`, then first playlist order. Prefer no-language-neutral guessing in v1.
- Materialize and rewrite both video and audio playlists, then map the primary video and selected audio in the local ffmpeg remux.
- An audio-only master is supported only when it exposes an audio rendition or an audio media playlist. The direct route does not expose the current `audio` menu choice in v1.
- `NAME` from the selected `EXT-X-MEDIA` item may be used as an audio title only when no better filename-derived title exists. It is not a reliable title for the overall video.

### Initialization and relative resources

- Fetch every `EXT-X-MAP` URI through the same guarded fetcher, preserve `BYTERANGE` if present, and rewrite it to a local initialization file.
- Support segment `EXT-X-BYTERANGE` by issuing a guarded request with the appropriate `Range` header and counting returned bytes. Validate server behavior so a full-resource response cannot silently multiply disk use.
- Preserve media sequence, timestamps, and key changes in the rewritten playlist. Generate collision-free local filenames; never derive write paths directly from remote URI paths.
- Associate the active `EXT-X-KEY` with each segment when parsing. A live window may rotate keys, so a changed key URI or IV applies only from that point until the next key tag or `METHOD=NONE`.
- Partition a media playlist at each `EXT-X-DISCONTINUITY`. Remux each continuous run independently, then combine only compatible outputs. Never concatenate segment bytes blindly across a discontinuity.

## 6. Live policy

### Decision rule and user meaning

A media playlist with `EXT-X-ENDLIST` is finite VOD. A media playlist without `EXT-X-ENDLIST` is live. For live input, take one snapshot and download only the segments listed in that snapshot. The result is a VOD file containing the server's available history at fetch time, not a recording and never "the whole stream." `EXT-X-PLAYLIST-TYPE` does not override this rule.

Call this an **available window** in UI text, with the clarification "what is available now." A server may publish 600 segments or only a few. Nothing older than the first listed media sequence is available through that snapshot. A playlist with no usable listed segments is `HLS_LIVE_NO_WINDOW`; a selected range whose usable duration is below the implementation's minimum remuxable duration is `HLS_WINDOW_TOO_SHORT`. Both are hopeless for that snapshot and get no retry button.

### Measured acceptance shape, not a provider-wide assumption

The following facts were measured read-only on 2026-10-31 around 10:12 to 10:17 UTC against the single owner-supplied URL `https://guest.pomf.tv/live/6b11b16682374f0f09917ed37de234b5/1080p/index.m3u8`. They define the acceptance fixture shape and must not be generalized to other hosts:

- The response was `200`, `application/vnd.apple.mpegurl`, 21,710 bytes, nginx, and `Cache-Control: no-store`.
- It was a media playlist with 600 segments, `EXT-X-TARGETDURATION:2`, every `EXTINF` equal to 2.000000 seconds, and `EXT-X-MEDIA-SEQUENCE:1793378844`. Segment names were `seg-<n>.ts` with consecutive numbers.
- The listed window was exactly 1,200 seconds, or 20:00. A fetch 22 seconds later advanced by 11 media sequences while retaining 600 segments.
- The oldest observed segment was about 0.88 to 0.91 MB. `HEAD seg-1793378844.ts` returned `200`, `video/mp2t`, and 907,852 bytes at 10:12, then the same URL returned `404` at 10:17. Expiry is real and fast for this host.
- A live-segment `GET` with `Range: bytes=0-0` returned `206`.
- The playlist had no `EXT-X-KEY`, `EXT-X-PROGRAM-DATE-TIME`, `EXT-X-DISCONTINUITY`, `EXT-X-MAP`, `EXT-X-BYTERANGE`, `EXT-X-ENDLIST`, or `EXT-X-PLAYLIST-TYPE`.
- The measured 1080p rate was about 3.5 Mbit/s and the 20-minute window was roughly 525 MB. The explicit `/720p/` path existed, measured about 0.41 MB per two-second segment, about 1.65 Mbit/s and roughly 245 MB per window. The parent master URL returned `404`, so these variants were paths supplied by the owner, not `EXT-X-STREAM-INF` choices.
- The media-sequence values resembled Unix seconds but did not map to probe time. Never infer wall clock from media sequence.

### Snapshot, selection, ordering, and expiry recovery

Do not poll continuously in v1. One snapshot is sufficient when the transfer completes comfortably inside the published window. For the measured 1080p shape, conservative arithmetic is `600 * 0.9 MB = 540 MB`. Merely matching a 20-minute window requires 0.45 MB/s aggregate, or 3.6 Mbit/s, which leaves no expiry margin. Set the operational target to finish segment fetches within five minutes: 1.8 MB/s aggregate, or 14.4 Mbit/s. With `N` parallel fetches, the required average useful throughput per worker is `1.8 / N MB/s`, for example 0.225 MB/s with eight workers. Using the measured rough total of 525 MB gives 1.75 MB/s, or 14 Mbit/s. These are transfer-throughput targets, not invented benchmarks or guarantees.

Fetch selected segments oldest-first because the measured oldest edge expired first. The cost is that the newest portion can be truncated if the run is slow. The alternative, newest-first followed by backfill, protects the tail but gives the most expiry-sensitive segments the longest delay and therefore loses more of the published history on the measured shape. Oldest-first is the v1 policy.

Treat an individual segment `404` or `410` as a recorded gap, not an immediate job failure. Skip it, remove any `.part`, and continue up to a configured and tested maximum gap count and maximum missing-duration ratio. Exact bounds remain an owner decision. If either bound would be exceeded, re-fetch the same media playlist exactly once, through the guarded fetcher, and append only newly listed tail segments that are later than the snapshot's last media sequence. Do not turn this into polling and do not substitute segments with reused sequence numbers. Continue oldest-first for the refresh tail. If the bounded gap policy still fails, raise `HLS_WINDOW_EXPIRED`, report downloaded and missing duration, and deliver nothing. If it succeeds with gaps, remux continuous runs, deliver the available duration, and report the gap count and missing duration in the completion summary.

### Range selection and preflight confirmation

Without `EXT-X-PROGRAM-DATE-TIME`, only relative selection is valid. Offer "whole available window" and "last N minutes." Compute the latter by summing `EXTINF` values backward from the newest segment until the requested duration is met. Do not map media sequence to time. Offer an absolute time range only when the selected media playlist contains valid `EXT-X-PROGRAM-DATE-TIME` coverage for that range; absolute selection without PDT is deferred.

Before downloading full segments, show the available-window duration, selected variant, estimated output size, estimated credits, and explicit Start/Cancel controls. Derive size from a small, guarded sample of actual segment sizes, using `HEAD` when usable or `Range: bytes=0-0` and its validated `Content-Range`; sample across the selected window and treat the estimate as approximate. Do not estimate live size from manifest `BANDWIDTH` or manifest byte length. The measured 525 MB example estimates to `ceil(525 / 200) = 3` credits under the current 200 MiB-per-credit rule (`media_bot_v2/credits/service.py:32-48`), while the final charge still uses delivered bytes. Re-fetch the playlist after user confirmation and rebuild the snapshot because confirmation latency can expire the original edge; show changed duration/size only if it crosses a defined material-change threshold, which remains an owner decision.

Forward recording, continuous capture, recurring polling, and absolute time ranges without PDT are explicitly out of scope for v1.

## 7. Security requirements

The playlist URL, every nested playlist/variant URL, every audio rendition, every segment, every `EXT-X-MAP` URI, and every `EXT-X-KEY` URI is a distinct untrusted fetch.

- Resolve relative references against the containing playlist's final URL first, then call `safe_request` for that absolute URL. `safe_request` permits only HTTP(S), fails closed on DNS errors, blocks loopback/private/link-local/multicast/reserved/unspecified addresses, and validates every redirect target (`media_bot_v2/engines/ssrf_guard.py:25-26`, `media_bot_v2/engines/ssrf_guard.py:73-105`).
- Use `safe_request` for the initial playlist and every segment, including the one-time live-tail refresh. Redirect validation is per hop. A redirect's final URL becomes the base for relative references in the returned playlist; a segment redirect does not change the containing playlist's base.
- Never validate only the first playlist. An internal key URI is an SSRF read primitive even if the playlist and segments are public. It could expose metadata-service or intranet bytes through ffmpeg output or logs.
- Default policy: reject a non-HTTPS AES key URI with `HLS_INSECURE_KEY` even when the playlist itself arrived over HTTP. A plain-HTTP key has neither confidentiality nor trustworthy origin. The URI still passes SSRF validation before classification, and key bytes are never logged. This strict default is an owner decision listed below because some legitimate legacy streams use HTTP keys.
- Store keys in the per-task directory, rely on the pipeline's 0700/0600 hardening (`media_bot_v2/pipeline.py:281-295`, `media_bot_v2/pipeline.py:412-417`), use opaque local filenames, and delete them in the existing task cleanup path. Do not include keys in `DownloadResult.file_paths`.
- Restrict ffmpeg input protocols to local file/crypto/data protocols. Any `http://`, `https://`, `file://` outside the task directory, absolute path, parent traversal, or unresolved URI left in a rewritten playlist is an implementation error and must abort before subprocess launch.
- Count actual response-body bytes for playlists, maps, keys, audio, and media segments against one request-wide counter before writing each chunk. Do not trust `Content-Length`, playlist duration, `BANDWIDTH`, or yt-dlp-style estimates. The existing HLS/DASH estimate warning confirms why (`media_bot_v2/engines/ytdlp_support.py:16-24`). Raise `DownloadTooLargeError`, remove all partial files, and stop immediately when observed bytes exceed `MAX_DOWNLOAD_SIZE`.
- The preflight estimate is advisory. The local counter remains authoritative and includes retries and range responses actually transferred. If the refreshed live tail or observed bytes would exceed `MAX_DOWNLOAD_SIZE`, raise the existing size-cap failure, stop all fetches, and remove partial resources. The configured cap defaults to 4 GiB and is operator-configurable (`media_bot_v2/config.py:199-220`).
- Write each segment to an opaque `.part`, validate status, expected range semantics, and non-empty completion, then atomically rename it. A tolerated `404/410`, cancellation, cap breach, or stream error must leave no partial segment file. Bound gap count and missing duration as described in section 6 so a malicious manifest cannot create an unlimited sparse job.
- Bound individual manifest/key bodies and the number/depth of referenced resources to prevent tiny-manifest expansion and memory abuse. Exact constants must be selected during implementation from protocol-valid fixtures and documented; none were measured here.
- Do not pass remote headers, cookies, credentials, or URLs to ffmpeg command output. Sanitize logged URLs consistently with current request logging policy.

## 8. Encryption and DRM

| Method | Decision | Behavior |
|---|---|---|
| `METHOD=NONE` | Support | Clear segments; clear prior key state. |
| `METHOD=AES-128`, identity key format | Support | Guard-fetch the 16-byte key, retain IV/key changes in the local playlist, and let local ffmpeg decrypt. Validate key length and IV syntax before launch. |
| `METHOD=SAMPLE-AES` or `SAMPLE-AES-CTR` | Reject | Raise `HLS_ENCRYPTION_UNSUPPORTED`; no retry. Do not attempt partial delivery. |
| FairPlay, Widevine, PlayReady, `skd://`, `com.apple.streamingkeydelivery`, or other non-identity key formats | Reject | Raise `HLS_DRM_UNSUPPORTED`; no retry. Do not download licenses or media segments. |

DRM/encryption classification should live in the new HLS parser before segment fetch. Define `HLSUnsupportedError(UnsupportedUrlError)` in `engines/base.py` or the HLS module for hopeless DRM/malformed-policy outcomes, and a separate `HLSDownloadError` hierarchy for live-window expiry and transient remote/ffmpeg failures. `pipeline.py` must render them as domain errors (`media_bot_v2/pipeline.py:690-707`), while `telegram/retry.py` must explicitly classify hopeless variants (`media_bot_v2/telegram/retry.py:28-96`).

Key state is per segment, not per playlist. A live window can contain several `EXT-X-KEY` changes; resolve and guard-fetch each distinct active key URI, preserve its IV semantics, and rewrite each run to the matching local key. Never apply the final key to the whole window. SAMPLE-AES and DRM remain all-or-nothing refusals before media download.

## 9. Output contract

- The HLS download stage produces one `.mp4` for video or `.m4a`/`.mp4` for audio-only. ffmpeg first attempts stream-copy remux with mapped video and chosen audio, `-sn -dn`, preserved useful metadata, and `-movflags +faststart`.
- For a DVR window with gaps or `EXT-X-DISCONTINUITY`, remux each continuous run separately and join only compatible runs at the container level. The delivered duration is the sum of successful runs, not the original manifest sum. Report omitted gap duration.
- The delivery contract remains H.264/yuv420p video, AAC or MP3 audio, MP4 container, and moov before mdat (`media_bot_v2/upload/streamable.py:41-65`). HEVC/AV1 video and Opus audio trigger the existing selective re-encode (`media_bot_v2/upload/streamable.py:140-160`). This may be much slower and larger than remuxing and is bounded by `CONVERT_TIMEOUT`; failure keeps the current send-as-is/document fallback.
- Title/filename priority: safe `Content-Disposition` name from the initial/final playlist response; otherwise final playlist URL basename with `.m3u8` replaced by `.mp4`; otherwise selected audio rendition `NAME` for an audio-only result; otherwise `hls-download.mp4`. A page title is unavailable because this is a direct playlist URL, not a webpage extraction flow.
- Keep `DownloadResult.title` as the sanitized display name and return only the final media path. Temporary playlists, segments, maps, keys, and ffmpeg partial output are internal cleanup artifacts.
- Continue using `compute_cache_key(job_url, "direct", delivery.send_as)` (`media_bot_v2/telegram/router.py:462-464`, `media_bot_v2/cache/video_cache.py:52-74`). The exact URL, including signed query parameters, prevents accidental cross-URL reuse. Expired signed URLs will miss rather than reuse a stable content identity; that limitation is accepted in v1.
- The final file enters the existing splitter unchanged. Files larger than 2000 MiB are split after streamability processing (`media_bot_v2/pipeline.py:323-410`, `media_bot_v2/upload/splitter.py:27-53`). Video splitting uses stream copy and puts faststart on MP4 parts (`media_bot_v2/upload/splitter.py:113-167`).
- A high-bitrate 20-minute window can approach or exceed 2,000 MiB even though the measured owner stream did not. Exercise this path with a reduced split threshold or mocks in tests; do not create a multi-gigabyte fixture.

## 10. Progress, cancellation, cleanup, and credits

### Progress

Use three download subphases while preserving the single progress message:

1. **Preflight:** show "available window," selected relative range, selected variant, sampled size estimate, estimated credits, and Start/Cancel. State that the window can move before confirmation.
2. **Fetch:** report cumulative actual bytes, rolling speed, `segments completed / selected`, gaps, and fetched media duration. Do not turn variant `BANDWIDTH * duration` into a byte total. `format_progress` already renders transferred bytes and speed without a fake percent or ETA (`media_bot_v2/telegram/progress_format.py:145-205`). ETA may use observed mean segment time after a representative sample, clearly labeled as an estimate.
3. **Remux:** run ffmpeg with `-progress pipe:1 -nostats` and parse `out_time_ms`/`out_time_us`, `total_size`, `speed`, and terminal `progress=end`. Use selected `EXTINF` sum minus known gaps for a time-based percentage. Across discontinuity runs, aggregate completed run duration; if duration is malformed, show phase/speed only.

Throttle updates through the existing progress reporter pattern rather than adding messages. Direct streaming currently throttles to one update every two seconds (`media_bot_v2/engines/direct.py:59-61`, `media_bot_v2/engines/direct.py:111-141`).

### Cancellation and cleanup

- Check `CancellationToken` before every fetch, during every streamed response chunk, between playlist/segment operations, and while waiting for ffmpeg.
- Cancellation is valid during confirmation, parallel segment fetch, the one-time refresh, remux, split, and upload. Stop scheduling new segment work first, then close active responses and clean partials.
- Register callbacks that close the active response and terminate the ffmpeg process. On cancellation send SIGTERM/`terminate`, wait briefly, then kill if it does not exit. Reap the child in all cases to avoid zombies.
- Write each remote resource and final output to a `.part`/temporary name, rename only after complete validation, and unlink partials on error. The pipeline sets the cancellation token and removes the task directory in its `finally` path (`media_bot_v2/pipeline.py:732-741`). Unit tests must also exercise helper-level cleanup before the pipeline owns the directory.

### Credits

No final credit rule changes. Preflight uses `ceil(estimated MiB / MB_PER_CREDIT)` only as an estimate. The pipeline appends each successfully uploaded part's real `stat().st_size`, records bandwidth, and charges after delivery (`media_bot_v2/pipeline.py:509-522`, `media_bot_v2/pipeline.py:556-575`). Credits remain based on total delivered bytes, summed before rounding; splitting does not change the price (`media_bot_v2/credits/service.py:36-48`). If upload fails after some parts, the existing cleanup-time charge for delivered parts remains in force (`media_bot_v2/pipeline.py:732-740`). No download, manifest, segment, key, remux overhead, skipped gap, or failed upload byte is charged.

## 11. Touch points

The table lists proposed changes. “No change” rows document reviewed configuration/dependency surfaces required by this feature.

| File | Function/symbol | Planned change | Risk if missed |
|---|---|---|---|
| `media_bot_v2/engines/hls.py` (new) | `HLSDownloader`, parser/value objects, guarded fetcher, DVR selector, local rewrite, ffmpeg runner | Own detection helpers after response acquisition, master/media parsing, relative-range selection, snapshot/revalidation, oldest-first bounded parallel fetch, gap accounting, byte cap, AES-128, discontinuity runs, local-only remux, progress, cancellation, cleanup. | Remote subresources bypass SSRF; expired-edge loss is hidden; incorrect ordering/key/map handling; leaked partials. |
| `media_bot_v2/engines/direct.py` | `_preflight_and_stream_to_file`, `_stream_to_file`, `DirectEngine.download` | Detect extension/MIME/signature before writing; pass final URL and buffered playlist to HLS; preserve ordinary direct streaming. | Playlist text is still silently delivered; GET is duplicated; direct downloads regress. |
| `media_bot_v2/engines/base.py` | HLS error classes, `DownloadResult` contract documentation | Add classified live-window, unsupported, size, and retryable HLS errors without changing result shape. | Pipeline emits generic failures or offers retry for hopeless cases. |
| `media_bot_v2/pipeline.py` | domain-error tuple and processing contract | Classify retryable HLS failures; rely on existing probe/streamable/split/upload/charge path. | Correct HLS errors become generic; cleanup/retry behavior drifts. |
| `media_bot_v2/telegram/retry.py` | `HOPELESS_EXCEPTIONS`, `HOPELESS_TEXTS` | Mark no-window, too-short, too-many-gap expiry, DRM, SAMPLE-AES, malformed, and size-cap outcomes as no-retry; leave genuinely transient HTTP/ffmpeg errors retryable. | Useless retry buttons or suppressed retries for transient failures. |
| `media_bot_v2/telegram/texts.py` | HLS window, confirmation, completion, and failure families | Add "available window" preflight/summary copy and canonical errors for `HLS_WINDOW_TOO_SHORT`, `HLS_WINDOW_EXPIRED`, `HLS_LIVE_NO_WINDOW`, DRM, size cap, encryption, insecure key, and malformed input. Use `❌ מה קרה / ➡️ מה לעשות`; add hopeless variants. | UI claims the whole stream, gaps are hidden, or retry classification differs from canonical text. |
| `media_bot_v2/telegram/router.py` and callback data | guarded HLS confirmation lifecycle | Pause after preflight, bind Start/Cancel to the requesting user and job, expire stale callbacks, and resume with a fresh snapshot after Start. Current direct jobs otherwise call the engine from the pipeline without an HLS confirmation stage (`media_bot_v2/pipeline.py:302-321`). | Large DVR pulls start before informed consent; stale callbacks fetch moved windows. |
| `README.md` | supported sources, limits, environment/config table | Document HLS VOD/AES-128 and available-window support, relative selection, DRM/forward-recording exclusions, codec conversion cost, and unchanged ffmpeg requirement. | Operators/users mistake a window for the whole stream or expect recording. |
| `tests/test_hls_engine.py` (new) | local fixture server and HLS unit/integration cases | Prove parsing, guarded fan-out, VOD/live decision, oldest-first scheduling, gaps, refresh, relative selection, size sampling, key rotation, discontinuity runs, remux, cap, progress, cancellation, and cleanup offline. | The security boundary and DVR semantics remain unproved. |
| `tests/test_direct_engine.py` | HLS detection regression cases | Prove `.m3u8`, MIME-only, signature-only, false suffix, final redirect URL, and ordinary direct file behavior. Reuse `local_server` and `bypass_ssrf_guard` (`tests/test_direct_engine.py:48-157`, `tests/conftest.py:19-29`). | Detection order or buffered-prefix handling regresses direct downloads. |
| `tests/test_pipeline.py` | HLS result integration | Prove confirmation handoff, streamability fallback, >2GB splitter handoff with a reduced test limit/mocks, delivered-duration summary, delivered-byte accounting, charge timing, and task cleanup. | HLS works in isolation but violates delivery/billing lifecycle. |
| `tests/test_retry_button.py` | HLS failure classification | Assert no retry for empty/too-short/expired windows, DRM, SAMPLE-AES, insecure key, and size cap; retry only transient fetch/remux failure. | User retry semantics are wrong. |
| `tests/test_text_variants.py` | new HLS canonical/variant families | Assert category membership and hopeless coverage, not one fixed Hebrew sentence. | Randomized variants escape retry classification. |

Configuration and dependency decision:

- Reuse `MAX_DOWNLOAD_SIZE`, `REQUEST_TIMEOUT`, and `CONVERT_TIMEOUT`; do not add HLS-specific environment keys in v1 (`media_bot_v2/config.py:186-213`). `MAX_DOWNLOAD_SIZE` applies to cumulative observed remote bytes and final output must also remain under the normal pipeline limits.
- `.env.example`: no change because there is no new key. Its existing `MAX_DOWNLOAD_SIZE` entry remains authoritative (`.env.example:157`).
- `pyproject.toml`: no change. The project already declares Python 3.13+, `ffmpeg-python`, and `requests`; the plan adds no parser or crypto dependency (`pyproject.toml:6-27`).
- `deploy/`: no change. ffmpeg is already an operational requirement (`spec/SPEC.md:111-113`).

Expected implementation footprint is unverified until the confirmation callback design is chosen. It includes the HLS engine, direct integration, pipeline/error/text/retry surfaces, router/callback confirmation, README, and the five test areas above. This count excludes the explicitly reviewed no-change files above.

### Failure text and retry taxonomy

- `HLS_WINDOW_TOO_SHORT`: no remuxable selected duration. Explain that only a very short window is available now and suggest trying later or selecting the whole available window. No retry button on the same snapshot.
- `HLS_WINDOW_EXPIRED`: the bounded gap count or missing-duration ratio was exceeded after the single refresh. Report missing versus requested duration and suggest retrying from a fresh window. No automatic retry button because the exact requested snapshot cannot be recovered.
- `HLS_LIVE_NO_WINDOW`: live playlist has no usable segment window. Suggest checking the URL or trying when the stream publishes segments. No retry button.
- `HLS_DRM_UNSUPPORTED`: DRM or SAMPLE-AES requires an unsupported protected-media path. No retry button.
- Existing `DownloadTooLargeError`: observed bytes crossed `MAX_DOWNLOAD_SIZE`. State the cap and suggest a lower-bitrate explicit variant path or shorter relative range. No retry button.

Every canonical text and variant follows `❌ מה קרה / ➡️ מה לעשות`. Do not show a retry button for an outcome that cannot recover the same window. Transport failures before meaningful expiry remain retryable.

## 12. Offline test plan

Build a `ThreadingHTTPServer` fixture modeled on `tests/test_direct_engine.py:48-157` and use `bypass_ssrf_guard` only for positive loopback fetch mechanics (`tests/conftest.py:19-29`). Keep separate unit tests of URL validation/redirect blocking without that bypass, following `tests/test_ssrf_guard.py:91-144`.

Generate media fixtures during the test session with the installed ffmpeg, never download fixtures:

- A short H.264/AAC source, segmented into two TS variants with distinct resolutions/bitrates.
- A master playlist pointing to both variants with relative URIs.
- A separate audio rendition for at least one master.
- A short fMP4/CMAF playlist with `EXT-X-MAP`.
- An AES-128 variant with a generated 16-byte test key and deterministic IV. Patch only the HTTPS-key policy in this local-HTTP positive test; do not bypass URL resolution/fetch accounting.
- A sliding live media playlist with the measured acceptance shape: 600 tiny two-second TS segments, no PDT and no endlist, whose oldest window edge expires during the test. Keep payloads tiny while preserving durations, sequence movement, and expiry behavior.
- A key-rotation playlist with at least two AES-128 key URIs and a discontinuity playlist whose continuous runs can be validated independently.

Server routes and assertions:

| Case | Fixture behavior | Assertions |
|---|---|---|
| Extension detection | `/master.m3u8` | HLS path selected before file write; no `.m3u8` delivered. |
| MIME-only detection | `/watch` returns HLS MIME | Same result; initial body prefix is not lost. |
| Signature detection | `/opaque` returns octet-stream and `#EXTM3U` | Same result; ordinary octet-stream regression still passes. |
| Master + variants | Two variants and audio rendition | Deterministic 720-cap selection, selected audio muxed, unselected variant segments never requested. |
| Relative/final URL | Initial playlist redirects to another directory, containing relative nested playlist/map/segments | Resolution uses final redirect URL; ffprobe succeeds. |
| AES-128 rotation | Two or more AES-128 keys/IV states inside one window | Every segment uses its active key; output decrypts; key bytes are absent from result/logs and cleaned. |
| CMAF | `EXT-X-MAP` plus fMP4 segments | Init fetched once as required; output probes successfully. |
| Redirect | Segment route redirects to another allowed test route | Redirect is followed and counted once. |
| Blocked segment host | Public-looking manifest references `127.0.0.1`, metadata IP, or mocked DNS-to-private host | Fetch is refused before the segment request; classified error; no output/partials. |
| Blocked key host | Public playlist references an internal key URI | Refused before key read; proves key URI is not an SSRF primitive. |
| Plain-HTTP key | AES key uses HTTP | `HLS_INSECURE_KEY`, no segment fetch, no retry. |
| Sliding live window | 600 tiny two-second segments; on re-fetch advance sequence while retaining 600 | Exactly the listed snapshot is selected; UI calls it the available window; whole-window duration is 1,200 seconds; no forward polling occurs. |
| Oldest-first expiry | Request log blocks selected segment responses so scheduling is observable; oldest routes expire first | Initial dispatch and subsequent queue order are increasing media sequence despite parallel completion order; newest-first is never used. |
| Expired old segments | Oldest routes return `404/410` within the allowed gap bound | Gaps are recorded, `.part` files are absent, successful run duration equals delivered `EXTINF` sum, and completion reports gap count/duration. |
| Tail refresh | Gap bound is crossed; one refreshed playlist exposes later tail segments | Exactly one refresh occurs; only new sequences are added; no recurring polling; output ordering and delivered duration are correct. |
| Window already expired | Snapshot segments return enough `404/410` responses to exceed bounds after refresh | `HLS_WINDOW_EXPIRED`, no output or partials, missing duration reported, no retry button. |
| No/short window | Live playlist has zero usable segments or less than minimum remuxable duration | `HLS_LIVE_NO_WINDOW` or `HLS_WINDOW_TOO_SHORT`; no segment/ffmpeg work; no retry. |
| Relative range | No PDT; request whole window and last N minutes | Whole window selects all listed segments; last N walks `EXTINF` backward; no absolute-time or sequence-to-clock mapping is offered. |
| Range | Segment size preflight and an `EXT-X-BYTERANGE` segment return valid `206`; companion route ignores Range with `200` | Estimate uses validated `Content-Range`; byte-range payload is correct; full-response fallback cannot multiply disk use; all bytes count toward cap. |
| Discontinuity | One live window contains `EXT-X-DISCONTINUITY` between incompatible or separately valid runs | Fetch remains ordered; remux splits at the boundary; no blind byte concatenation; compatible final output reports the sum of delivered run durations. |
| DRM | SAMPLE-AES-CTR/FairPlay, plus PlayReady key format | `HLS_DRM_UNSUPPORTED`, no license/key/segment fetch, no retry. |
| Unsupported encryption | SAMPLE-AES identity format | `HLS_ENCRYPTION_UNSUPPORTED`, no segment fetch, no retry. |
| Malformed | bad header, invalid attributes, nested cycle, missing URI, invalid key/IV | `HLS_MALFORMED`, bounded failure, no partial files. |
| Size cap | omitted/false `Content-Length`, cumulative chunks cross a small injected cap | Exact observed byte count on `DownloadTooLargeError`; response closed; no partials. |
| Cancellation | slow segment and slow mocked ffmpeg child | Active response closes; child terminates and is reaped; no result/partials. |
| Codec fallback | HEVC/AV1 or mocked incompatible ffprobe profile | `ensure_streamable` command/path invoked; H.264/AAC MP4 with faststart when conversion succeeds. |
| Billing | pipeline upload succeeds, partially succeeds, or fails before upload | Charge equals delivered part bytes only; successful split does not change total credits; no delivery means no charge. |
| Preflight credits | Sampled size estimates 525 MiB at 200 MiB per credit; then actual delivery differs | UI estimate is 3 credits; final charge uses actual delivered bytes and existing sum-before-rounding behavior (`media_bot_v2/credits/service.py:36-48`). |
| Large-window split | Mock/reduced threshold represents a high-bitrate 20-minute output above 2,000 MiB | Existing split path runs, every part is within limit, MP4 parts are faststart, and summed delivered bytes drive credits (`media_bot_v2/upload/splitter.py:27-53`, `media_bot_v2/upload/splitter.py:113-167`). |

For successful outputs, assert with ffprobe that video is H.264, audio is AAC where present, the container is MP4, and use `inspect_streams`/`moov_before_mdat` to assert faststart (`media_bot_v2/upload/streamable.py:68-137`). Assert the server's per-route request log to prove every expected resource was fetched once and forbidden/unselected resources were never touched. For all failure cases, assert the error class, retry classification, exact observed delivered-byte list where applicable, and an empty task directory after cleanup.

Verification commands for implementation runs:

```text
/home/vm/projects/media-bot-v2/.venv/bin/python -m pytest -q tests/test_hls_engine.py tests/test_direct_engine.py tests/test_pipeline.py tests/test_retry_button.py tests/test_text_variants.py
/home/vm/projects/media-bot-v2/.venv/bin/python -m pytest -q
uv run ruff check
```

## 13. Milestones and effort

Effort is expressed as one focused agent run and one commit per milestone, not elapsed-hour estimates. No timing measurement was performed.

### Milestone 1: detection, clear VOD, and local remux

Scope: add extension/MIME/signature detection; parse finite master/media playlists; deterministic variant/audio selection; guarded playlist/map/segment fetches; cumulative byte cap; local rewrite; TS and CMAF stream-copy remux; output contract; direct/pipeline integration.

Acceptance: generated finite clear TS and CMAF fixtures produce a playable local MP4; every remote request is observed through the guarded fetch layer; extensionless HLS no longer becomes a `.bin`; ordinary direct downloads remain green; malformed/live/encrypted playlists may return a temporary explicit “unsupported in this milestone” class.

Risk: highest-risk milestone because it establishes the network boundary and local rewrite. Commit independently after focused tests and the full suite pass.

### Milestone 2: DVR available-window snapshot

Scope: classify no-endlist media playlists as live; select whole window or last N minutes; sample segment sizes; show variant/window/size/credit confirmation; re-snapshot on Start; download with bounded parallelism in oldest-first queue order; tolerate and account for bounded `404/410` gaps; refresh the tail once; split discontinuity runs; report delivered and missing duration; cancel and clean partials.

Acceptance for one focused agent run: the offline 600-segment, 1,200-second sliding fixture completes from one snapshot with oldest-first request order, no recurring polling, correct three-credit estimate for a mocked 525 MiB sample, and correct delivered duration. A companion expiry case performs exactly one tail refresh, leaves no `.part` files, and either reports bounded gaps or refuses with `HLS_WINDOW_EXPIRED` when the configured bound is exceeded.

Explicitly deferred: forward recording, continuous capture, recurring polling, probing sibling variant paths, and absolute time ranges without PDT.

Risk: confirmation latency and the expiring oldest edge can make the preflight snapshot stale, so Start must rebuild the snapshot and never promise the original exact segment set.

### Milestone 3: AES-128 and resource hardening

Scope: identity AES-128 key/IV validation; HTTPS-key policy; per-segment key rotation; local key rewrite; byte ranges; playlist cycles/depth/resource bounds; cancellation and partial cleanup; detailed progress.

Acceptance: generated AES-128 fixture decrypts to the expected ffprobe profile; internal/plain-HTTP keys are refused as designed; key bytes never appear in logs/results; cap/cancel tests leave no files or child processes.

Risk: key handling and byte-range accounting. Commit independently after security-focused tests and the full suite pass.

### Milestone 4: final taxonomy, retry behavior, splitting, and documentation

Scope: detect SAMPLE-AES and FairPlay/Widevine/PlayReady markers before segment fetch; add final Hebrew window/DRM/size text families; hopeless classification; README; pipeline/billing/high-bitrate split regression coverage.

Acceptance: every hopeless refusal maps to its named error/text family with no retry button, transient transport/remux failures still get retry, no forbidden resource is requested, the simulated output above 2,000 MiB takes the existing split path, and README states the exact support boundary.

Risk: string variants can accidentally escape hopeless classification; existing tests require category assertions rather than fixed copy (`CLAUDE.md`, “Working with agents”). Commit independently after focused tests, full suite, and ruff.

## 14. Risks

1. **SSRF fan-out:** one public manifest can reference many private or redirecting resources. Missing validation on even one URI class breaks the security boundary.
2. **Playlist correctness:** nested masters, audio groups, byte ranges, key rotation, discontinuities, and CMAF init maps make a hand-written parser error-prone. Keep scope to tags required above, reject unknown security-sensitive encryption, and prove behavior with request logs and ffprobe.
3. **Disk/size amplification:** manifest metadata cannot be trusted. Count every downloaded byte, bound fan-out, and include temporary encrypted bytes plus final output in disk-capacity planning even though credits use delivered bytes only.
4. **Sliding expiry and incomplete delivery:** the oldest edge can disappear during confirmation or transfer. Re-snapshot on Start, fetch oldest-first, bound gaps, refresh once, and report delivered versus missing duration rather than implying completeness.
5. **Throughput variance:** 14.4 Mbit/s is the five-minute target for the conservative measured 540 MB shape, not a guarantee. A slow origin or bot link can exceed the expiry margin even with parallelism; cap concurrency and fail honestly when gap bounds are exceeded.
6. **Codec/transcode cost:** stream copy may preserve HEVC/AV1/Opus. Existing fallback can consume significant CPU and hit its 180-second default before sending as a document (`media_bot_v2/config.py:195-197`).
7. **Signed URL behavior:** relative resolution must preserve query semantics, while local rewrites must not leak signed URLs or headers. Cache keys based on full signed URLs will have low hit rates after expiry.

## 15. Open questions for the owner

1. **Automatic quality cap:** default to 720p, matching the existing route default, or choose the highest available variant? **Recommended default:** 720p. Choosing highest improves source fidelity but raises download size, transcode load, split frequency, and credits without an HLS-specific confirmation menu.
2. **Plain-HTTP AES keys:** reject them or allow them when the playlist is also HTTP? **Recommended default:** reject. Allowing them supports more legacy streams but exposes key material to interception and needs a clearly named opt-in configuration plus warning documentation.
3. **Gap bounds:** what maximum missing segment count and missing-duration ratio may still be delivered? **Recommended default:** at most 3 missing segments and at most 1% of selected duration, with either bound stopping delivery after the one refresh. This keeps small edge races visible without normalizing materially incomplete files. This default is proposed, not measured.
4. **Confirmation drift:** how much may duration, sampled size, or estimated credits change after Start before asking again? **Recommended default:** reconfirm only if estimated credits increase or selected duration changes by more than 10%. The threshold is proposed, not measured.
5. **HLS quality menu:** keep automatic master selection in v1 or generalize the YouTube `ytq` menu? **Recommended default:** automatic selection for real master playlists and use explicit media-playlist paths exactly as supplied. A generalized menu needs a guarded preflight lifecycle, cached manifest metadata, callback changes, and expiry handling.

These five choices should be confirmed before coding. The plan's recommended defaults are internally consistent and can be implemented without new dependencies or configuration keys.

## 16. Definition of done

- `.m3u8`, HLS MIME, and `#EXTM3U` inputs are identified before playlist bytes are written as delivered media.
- Finite clear and AES-128 TS/CMAF playlists produce a final H.264/AAC MP4 with faststart, directly or through the existing streamability conversion.
- A no-endlist media playlist downloads only its listed available window, or the last N minutes selected by reverse `EXTINF` summation, and never claims to capture the whole stream.
- The measured acceptance fixture has 600 two-second clear TS segments, a 1,200-second sliding window, no PDT, and expiring oldest segments; it verifies oldest-first scheduling, one refresh at most, bounded gap accounting, and delivered-duration reporting.
- Preflight samples real segment sizes and shows window length, selected variant, estimated size, and estimated credits with Start/Cancel before full segment transfer.
- Every remote playlist, rendition, map, segment, key, and redirect passes the SSRF guard after relative resolution against the containing playlist's final URL.
- Actual fetched bytes enforce `MAX_DOWNLOAD_SIZE`; no manifest estimate can reject or permit a download by itself.
- AES-128 key rotation is resolved per segment. SAMPLE-AES and DRM fail before media/key/license fetch with classified Hebrew text and no retry button.
- Discontinuities and tolerated gaps are remuxed as separate continuous runs; no partial segment files survive failure or cancellation.
- Cancellation closes active responses, terminates/reaps ffmpeg, and leaves no partials or keys.
- Existing split, upload, cache, progress-message, and delivered-byte credit semantics remain unchanged; the simulated high-bitrate window exercises the over-2,000-MiB split path.
- All offline fixture assertions, the full pytest suite, and ruff pass without internet or Telegram access.
