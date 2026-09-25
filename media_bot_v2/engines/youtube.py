"""YouTube download engine backed by yt-dlp.

Format strategy (the milestone's main speed lever): prefer a single
*progressive* stream - one file with video and audio already muxed together
- over yt-dlp's usual `bestvideo+bestaudio` pair whenever a progressive
stream exists at or under the requested height. A progressive hit needs no
ffmpeg merge step at all; a `bestvideo+bestaudio` pair needs one, and even
though yt-dlp's default merger stream-copies (`-c copy`, no re-encode) when
the codecs allow it, skipping the merge entirely is strictly faster than
running it. `build_format_selector` encodes this as a yt-dlp format-selector
fallback chain: progressive-at-height, then split-then-merge-at-height,
then whatever `best` resolves to.

Error classification (`classify_youtube_error`) is a Hebrew-language port of
the old bot's `classify_download_error` (src/engine/generic.py) - same
category list (JS runtime missing, bot detection, cookies, private/removed,
geo-restriction, live, format unavailable), reimplemented against this
project's own exception types rather than copied verbatim, plus two new
buckets this project's spec calls out explicitly: PO token required and
playlist-unavailable.

Retries (`is_retryable_error`) only cover the network-pattern bucket - a
"video is private" or "PO token required" error will never succeed on
retry, so retrying it just delays the (correct) failure message.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import yt_dlp

from media_bot_v2.engines.base import (
    BaseEngine,
    CancellationToken,
    DownloadResult,
    DownloadTooLargeError,
    NotMediaContentError,
    RouteAttemptTracker,
    UnsupportedUrlError,
)
from media_bot_v2.engines.ytdlp_support import (
    TRANSPORT_RETRIES,
    DownloadCancelledSignal,
    DownloadGuard,
    DownloadTooLargeSignal,
    remove_partial_files,
    too_large_error,
)
from media_bot_v2.providers.downloader import download_provider_media
from media_bot_v2.telegram import texts

if TYPE_CHECKING:
    from media_bot_v2.providers.health import ProviderHealthTracker
    from media_bot_v2.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_QUALITY_HEIGHTS = {"1080": 1080, "720": 720, "480": 480, "360": 360}
_VIDEO_ID_RE = re.compile(r"^[0-9A-Za-z_-]{11}$")
_PROGRESS_THROTTLE_SECONDS = 2.0
_JS_RUNTIME_BINARIES = ("deno", "node", "bun")
YOUTUBE_DOMAINS = ("youtube.com", "youtu.be")


class YouTubeDownloadError(Exception):
    """Raised with an already-classified, Hebrew, user-facing message."""

    def __init__(self, message: str, original_error: str | Exception | None = None) -> None:
        super().__init__(message)
        self.original_error = str(original_error) if original_error is not None else message


_DownloadTooLargeSignal = DownloadTooLargeSignal
_DownloadCancelledSignal = DownloadCancelledSignal


def matches_youtube_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.split(":")[0].lower()
    except (ValueError, AttributeError):
        return False
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in YOUTUBE_DOMAINS)


def extract_video_id(url: str) -> str | None:
    """Best-effort stable id for the cache key - youtu.be/X, watch?v=X,
    /shorts/X, /embed/X, /live/X all resolve to the same id so the cache
    hits regardless of which URL form a user pastes. Returns None (caller
    falls back to the raw URL) for anything not recognized."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    if "youtu.be" in host:
        candidate = parsed.path.strip("/").split("/")[0]
        return candidate if _VIDEO_ID_RE.match(candidate) else None

    if "youtube.com" in host:
        if parsed.path == "/watch":
            values = parse_qs(parsed.query).get("v")
            if values and _VIDEO_ID_RE.match(values[0]):
                return values[0]
        for prefix in ("/shorts/", "/embed/", "/live/"):
            if parsed.path.startswith(prefix):
                candidate = parsed.path[len(prefix):].strip("/").split("/")[0]
                if _VIDEO_ID_RE.match(candidate):
                    return candidate
    return None


def is_playlist_url(url: str) -> bool:
    """A `/playlist` path, or a `list=` param with no `v=` (a playlist link
    proper), counts as a playlist request. A `watch?v=X&list=Y` URL (sharing
    a single video while it happens to be playing inside a playlist) does
    not - that must download exactly the one video, matching what a user
    who pasted a single-video link expects."""
    parsed = urlparse(url)
    path = parsed.path.lower()
    qs = parse_qs(parsed.query)
    if "/playlist" in path:
        return True
    if "v" in qs:
        return False
    return "list" in qs


# Subtitle languages requested when the user enabled subtitles: the old bot's
# English set. Kept narrow on purpose - `writeautomaticsub` with no language
# filter would download every auto-translated track.
SUBTITLE_LANGS = ["en", "en-orig", "en-US", "en-GB"]


def build_format_selector(quality: str) -> str:
    if quality == "audio":
        return "bestaudio/best"
    height = _QUALITY_HEIGHTS.get(quality)
    if height is None:
        raise ValueError(f"Unknown YouTube quality: {quality!r}")
    return (
        f"best[vcodec!=none][acodec!=none][height<={height}]/"
        f"bestvideo[height<={height}]+bestaudio/"
        "best"
    )


def check_js_runtime() -> bool:
    """Logged, not enforced: the rest of the bot works fine without a JS
    runtime (direct links, settings, TikTok/Instagram in later milestones);
    only YouTube extraction needs one to solve the n-challenge signature
    puzzle. Call this once at startup so a missing runtime is a visible
    warning in the logs, not a silent "YouTube always fails" mystery."""
    for binary in _JS_RUNTIME_BINARIES:
        path = shutil.which(binary)
        if path:
            logger.info("JavaScript runtime for yt-dlp: %s (%s)", binary, path)
            return True
    logger.warning(
        "No JavaScript runtime found in PATH (checked: %s). yt-dlp needs one to solve "
        "YouTube's n-challenge signatures - YouTube downloads will fail with a "
        "'runtime' error until Node.js (>=22) or Deno (>=2.3) is installed and reachable "
        "in PATH (including under systemd - ExecStart does not inherit an interactive "
        "shell's PATH by default).",
        ", ".join(_JS_RUNTIME_BINARIES),
    )
    return False


DEFAULT_PLAYER_CLIENT_NO_COOKIES = "mweb"
DEFAULT_PLAYER_CLIENT_WITH_COOKIES = "web,default"
DEFAULT_JS_RUNTIMES: dict[str, dict] = {"deno": {}, "node": {}}


def parse_js_runtimes(
    runtimes: dict[str, dict] | list[str] | str | None,
) -> dict[str, dict]:
    """Parse JS runtimes into the dictionary format yt-dlp expects.

    Defaults to enabling both Deno and Node.js so whichever is present in PATH
    can solve YouTube signatures. Can be overridden via explicit argument or
    the YOUTUBE_JS_RUNTIMES / JS_RUNTIMES environment variable.
    """
    if runtimes is None:
        return dict(DEFAULT_JS_RUNTIMES)
    if isinstance(runtimes, dict):
        return runtimes
    if isinstance(runtimes, str):
        items = [item.strip() for item in runtimes.split(",") if item.strip()]
    else:
        items = list(runtimes)

    result: dict[str, dict] = {}
    for item in items:
        if ":" in item:
            name, path = item.split(":", 1)
            result[name.strip().lower()] = {"path": path.strip()}
        else:
            result[item.strip().lower()] = {}
    return result or dict(DEFAULT_JS_RUNTIMES)


def parse_remote_components(
    components: list[str] | set[str] | dict | str | None,
) -> list[str] | None:
    """Parse remote components list for yt-dlp.

    Defaults to empty (None) because yt-dlp-ejs is installed and bundles solver
    scripts locally, avoiding unneeded network calls to GitHub or npm.
    """
    if not components:
        return None
    if isinstance(components, (list, set, tuple)):
        parsed = [str(c).strip() for c in components if str(c).strip()]
        return parsed or None
    if isinstance(components, dict):
        return list(components.keys()) or None
    if isinstance(components, str):
        parsed = [c.strip() for c in components.split(",") if c.strip()]
        return parsed or None
    return None


def resolve_player_client(
    player_client: str | None = None,
    *,
    has_cookies: bool = False,
) -> str:
    """Select the Innertube player client sequence according to PO Token guidelines.

    - Without cookies: 'mweb' is recommended when running with a PO token provider.
    - With cookies: 'web,default' is preferred so cookies/authenticated formats take effect.
    - Can be overridden via player_client argument or YOUTUBE_PLAYER_CLIENT / PLAYER_CLIENT env var.
    """
    if player_client and player_client.strip():
        return player_client.strip()
    env_override = os.getenv("YOUTUBE_PLAYER_CLIENT") or os.getenv("PLAYER_CLIENT")
    if env_override and env_override.strip():
        return env_override.strip()
    if has_cookies:
        return DEFAULT_PLAYER_CLIENT_WITH_COOKIES
    return DEFAULT_PLAYER_CLIENT_NO_COOKIES


_ERROR_PATTERNS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "the page needs to be reloaded",
            "n challenge solving failed",
            "challenge solver script",
            "supported javascript runtime",
            "signature solving failed",
            "javascript runtime",
            "js runtime",
            "no supported javascript runtime",
            "a javascript runtime is required",
        ),
        (
            "שגיאת פענוח ביוטיוב: חסר בשרת runtime של JavaScript (Node.js או Deno) הנדרש "
            "לפענוח חתימות יוטיוב.\nיש להתקין Node.js (גרסה 22 ומעלה) או Deno בשרת."
        ),
    ),
    (
        (
            "unsupported url",
            "is not a valid url",
            "unknown url type",
            "url is not supported",
        ),
        texts.UNSUPPORTED_URL,
    ),
    (
        ("po token", "po_token", "missing a required po token"),
        "יוטיוב דורש PO token עבור סרטון זה.\nיש להגדיר טוקן תקף בשרת (משתנה הסביבה POTOKEN).",
    ),
    (
        (
            "sign in to confirm you're not a bot",
            "confirm you're not a bot",
            "bot detection",
            "automated queries",
            "unusual traffic",
            "sign in to confirm your age",
            "requires authentication",
            "login required",
            "http error 429",
        ),
        (
            "ההורדה מיוטיוב נחסמה (זיהוי בוט או נדרש אימות).\n"
            "יש לעדכן את קובץ ה-cookies בשרת או להמתין להסרת החסימה."
        ),
    ),
    (
        ("cookie", "cookies"),
        (
            "שגיאת אימות מול יוטיוב: קובץ ה-cookies אינו תקין או שפג תוקפו.\n"
            "יש לרענן את קובץ ה-cookies בשרת."
        ),
    ),
    (
        (
            "this video is unavailable",
            "video unavailable",
            "this video is private",
            "private video",
            "has been removed",
            "members-only content",
            "video is no longer available",
        ),
        "הסרטון אינו זמין (סרטון פרטי, נמחק, או דורש מנוי לערוץ).",
    ),
    (
        (
            "not available in your country",
            "available in your country",
            "geographic restriction",
            "blocked in your country",
            "georestricted",
        ),
        "הסרטון חסום לצפייה במדינה שבה נמצא השרת (הגבלה גיאוגרפית).",
    ),
    (
        ("this live event", "live stream", "premieres in"),
        "לא ניתן להוריד שידור חי פעיל. נסה שוב לאחר סיום השידור.",
    ),
    (
        (
            "this playlist type is unviewable",
            "playlist does not exist",
            "the playlist is private",
            "playlist unavailable",
        ),
        "הפלייליסט אינו זמין (פרטי, נמחק, או שאינו קיים).",
    ),
    (
        ("requested format is not available", "no video formats found", "format not available"),
        "ההורדה נכשלה: הפורמט המבוקש אינו זמין עבור סרטון זה. נסה איכות אחרת.",
    ),
)

_NETWORK_PATTERNS = (
    "connection reset",
    "connection refused",
    "connection timed out",
    "timed out",
    "urlopen error",
    "network is unreachable",
    "no route to host",
    "temporary failure in name resolution",
    "read timed out",
    "connection aborted",
    "remotedisconnected",
    "incompleteread",
)


def classify_youtube_error(message: str | None) -> str:
    """Map a raw yt-dlp/network error to a user-facing Hebrew message.

    Whitelist only: the user sees a message from the fixed tables above (or the
    generic one) and never any part of `message`, which can carry file paths,
    hostnames, IPs, tokens or API keys. The raw text goes to the log only.
    """
    if not message:
        return "ההורדה נכשלה: לא התקבל קובץ מדיה מיוטיוב."
    lowered = message.lower()
    for keywords, hebrew in _ERROR_PATTERNS:
        if any(keyword in lowered for keyword in keywords):
            return hebrew
    if any(pattern in lowered for pattern in _NETWORK_PATTERNS):
        return "שגיאת רשת בהורדה מיוטיוב. נסה שוב בעוד מספר רגעים."
    logger.warning("Unclassified YouTube error (details withheld from user): %s", message)
    return texts.YOUTUBE_GENERIC_FAILURE


def is_retryable_error(message: str | None) -> bool:
    if not message:
        return False
    lowered = message.lower()
    return any(pattern in lowered for pattern in _NETWORK_PATTERNS) or "http error 5" in lowered


def _human_size(num_bytes: float | None) -> str:
    if not num_bytes:
        return "0B"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def _human_eta(seconds: float | None) -> str:
    if not seconds:
        return "לא ידוע"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} שניות"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}:{secs:02d} דקות"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d} שעות"


def format_progress_text(d: dict) -> str | None:
    status = d.get("status")
    if status == "downloading":
        downloaded = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        percent = int(downloaded / total * 100) if total else 0
        size_text = f"{_human_size(downloaded)}/{_human_size(total)}" if total else _human_size(downloaded)
        speed_text = f"{_human_size(d.get('speed'))}/s" if d.get("speed") else "לא ידוע"
        eta_text = _human_eta(d.get("eta"))
        return f"{texts.DOWNLOADING}\n{percent}% ({size_text})\n⚡ מהירות: {speed_text}\n⏱️ זמן משוער: {eta_text}"
    if status == "finished":
        return texts.PROCESSING
    return None


def _extract_file_paths(entry: dict) -> list[str]:
    downloads = entry.get("requested_downloads")
    if downloads:
        return [d["filepath"] for d in downloads if d.get("filepath")]
    filename = entry.get("filepath") or entry.get("_filename")
    return [filename] if filename else []


def _extract_subtitle_paths(entry: dict) -> list[str]:
    """Subtitle files yt-dlp wrote, in the order it reports them."""
    requested = entry.get("requested_subtitles")
    if not requested:
        downloads = entry.get("requested_downloads") or []
        requested = downloads[0].get("requested_subtitles") if downloads else None
    paths: list[str] = []
    for sub in (requested or {}).values():
        filepath = sub.get("filepath") if isinstance(sub, dict) else None
        if filepath and filepath not in paths and Path(filepath).exists():
            paths.append(filepath)
    return paths


def summarize_ytdlp_failure(exc: Exception | str) -> str:
    original = getattr(exc, "original_error", None)
    msg = str(original) if original else str(exc)
    lowered = msg.lower()
    if any(k in lowered for k in ("javascript runtime", "js runtime", "n challenge", "runtime", "javascript", "פענוח")):
        return "חסר runtime של JavaScript"
    if any(k in lowered for k in ("sign in", "bot detection", "login required", "authentication", "confirm you're not a bot", "זיהוי בוט", "נדרש אימות", "התחברות")):
        return "סרטון דורש התחברות או אימות"
    if any(k in lowered for k in ("po token", "po_token", "טוקן")):
        return "נדרש PO token"
    if any(k in lowered for k in ("private", "unavailable", "removed", "members-only", "פרטי", "אינו זמין", "נמחק")):
        return "סרטון פרטי או אינו זמין"
    if any(k in lowered for k in ("format not available", "no video formats", "requested format", "פורמט")):
        return "פורמט מבוקש אינו זמין"
    if any(k in lowered for k in ("timeout", "connection", "שגיאת רשת")):
        return "שגיאת רשת"
    return "שגיאה במנוע המקומי"


def summarize_provider_failure(exc: Exception | str) -> str:
    if isinstance(exc, NotMediaContentError):
        return "הספק החזיר תוכן שאינו מדיה"
    msg = str(exc)
    lowered = msg.lower()
    if "unavailable" in lowered or "not configured" in lowered:
        return "ספק אינו מוגדר"
    if "timeout" in lowered or "timed out" in lowered:
        return "פסק זמן בחיבור"
    if "403" in lowered or "rate limit" in lowered or "429" in lowered:
        return "חסימת גישה או הגבלת קצב"
    if "404" in lowered or "no media" in lowered or "empty" in lowered:
        return "לא נמצאה כתובת להורדה"
    if "500" in lowered or "502" in lowered or "503" in lowered or "server error" in lowered:
        return "שגיאת שרת של הספק"
    return "שגיאה בספק"


def _result_from_info(
    info: dict,
    *,
    playlist_item_limit: int | None = None,
    too_large_count: int = 0,
) -> DownloadResult:
    if info.get("_type") == "playlist" or "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        file_paths: list[str] = []
        for entry in entries:
            file_paths.extend(_extract_file_paths(entry))
        title = info.get("title") or "YouTube playlist"
        description = None
        subtitle_paths = []
        playlist_count = info.get("playlist_count") or info.get("n_entries")
        playlist_total = int(playlist_count) if playlist_count else None
        playlist_downloaded = len(file_paths)
        playlist_trimmed_reason = None
        if playlist_total is not None and playlist_downloaded < playlist_total:
            # yt-dlp only walks the first `playlist_item_limit` entries; the
            # rest were never attempted, whatever their availability.
            attempted = playlist_total
            if playlist_item_limit is not None:
                attempted = min(playlist_total, playlist_item_limit)
            playlist_trimmed_reason = texts.format_playlist_trim_reason(
                skipped_for_credits=playlist_total - attempted,
                too_large=min(too_large_count, max(attempted - playlist_downloaded, 0)),
                failed=max(attempted - playlist_downloaded - too_large_count, 0),
            )
    else:
        file_paths = _extract_file_paths(info)
        title = info.get("title") or "YouTube"
        description = info.get("description") or None
        subtitle_paths = _extract_subtitle_paths(info)
        playlist_total = None
        playlist_downloaded = None
        playlist_trimmed_reason = None

    if not file_paths:
        raise YouTubeDownloadError(classify_youtube_error(None))
    return DownloadResult(
        file_paths=file_paths,
        title=title,
        description=description,
        subtitle_paths=subtitle_paths,
        playlist_total=playlist_total,
        playlist_downloaded=playlist_downloaded,
        playlist_trimmed_reason=playlist_trimmed_reason,
    )


def format_duration(seconds: float | None) -> str | None:
    if not seconds or seconds < 0:
        return None
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


async def fetch_title_duration(url: str, *, opts: dict, timeout: float) -> tuple[str | None, str | None]:
    """Title and formatted duration for the quality menu, or (None, None).

    A bounded, best-effort lookup: the menu must appear promptly whatever
    happens here, so any failure or a blown `timeout` just means the caller
    shows its placeholders. It is not a download attempt - it does not touch
    the provider health tracker or a route-attempt summary.
    """

    def _extract() -> dict | None:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.wait_for(asyncio.to_thread(_extract), timeout=timeout)
    except Exception:
        logger.info("Title/duration lookup failed or timed out for %s", url, exc_info=True)
        return None, None
    if not isinstance(info, dict):
        return None, None
    title = info.get("title")
    return (title.strip() or None) if isinstance(title, str) else None, format_duration(info.get("duration"))


class YouTubeEngine(BaseEngine):
    """One instance per download request - unlike DirectEngine, quality and
    the progress reporter vary per request, not per process, so the router
    constructs a fresh instance for each `ytq:` callback."""

    name: str = "youtube"
    supported_platforms: tuple[str, ...] = ("youtube",)

    def __init__(
        self,
        *,
        quality: str,
        max_download_size: int,
        progress=None,
        force_ipv4: bool = False,
        cookies_file: str | None = None,
        po_token: str | None = None,
        is_playlist: bool = False,
        playlist_item_limit: int | None = None,
        max_retries: int = 0,
        player_client: str | None = None,
        js_runtimes: dict[str, dict] | list[str] | str | None = None,
        remote_components: list[str] | set[str] | dict | str | None = None,
        registry: ProviderRegistry | None = None,
        health_tracker: ProviderHealthTracker | None = None,
        subtitles: bool = False,
    ) -> None:
        if playlist_item_limit is not None:
            if playlist_item_limit <= 0:
                raise ValueError(f"playlist_item_limit must be positive, got {playlist_item_limit}")
            is_playlist = True
        self._quality = quality
        self._subtitles = subtitles
        self._max_download_size = max_download_size
        self._progress = progress
        self._force_ipv4 = force_ipv4
        self._cookies_file = cookies_file
        self._po_token = po_token
        self._is_playlist = is_playlist
        self._playlist_item_limit = playlist_item_limit
        self._max_retries = max_retries
        self._registry = registry
        self._health_tracker = health_tracker
        self._player_client = resolve_player_client(
            player_client,
            has_cookies=bool(cookies_file),
        )
        env_js = os.getenv("YOUTUBE_JS_RUNTIMES") or os.getenv("JS_RUNTIMES")
        self._js_runtimes = parse_js_runtimes(js_runtimes if js_runtimes is not None else env_js)
        env_remote = os.getenv("YOUTUBE_REMOTE_COMPONENTS") or os.getenv("REMOTE_COMPONENTS")
        self._remote_components = parse_remote_components(
            remote_components if remote_components is not None else env_remote
        )

    def matches(self, url: str) -> bool:
        return matches_youtube_url(url)

    async def download(
        self,
        url: str,
        *,
        dest_dir: Path,
        cancel_token: CancellationToken | None = None,
    ) -> DownloadResult:
        if not self.matches(url):
            raise UnsupportedUrlError(texts.UNSUPPORTED_URL)
        dest_dir.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.to_thread(self._download_sync, url, dest_dir, loop, cancel_token)
        except DownloadTooLargeError:
            raise
        except UnsupportedUrlError:
            raise
        except YouTubeDownloadError as exc:
            if self._registry is None or self._health_tracker is None or self._is_playlist:
                raise
            logger.info("Local YouTube engine failed (%s); attempting provider fallback", exc)
            fallback_res = await self._try_fallback_providers(
                url, dest_dir, initial_error=exc, cancel_token=cancel_token
            )
            if fallback_res is not None:
                return fallback_res
            raise

    async def _try_fallback_providers(
        self,
        url: str,
        dest_dir: Path,
        initial_error: YouTubeDownloadError | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> DownloadResult | None:
        if self._registry is None or self._health_tracker is None:
            return None
        candidates = self._registry.get_providers_for_platform("youtube")
        ordered_providers = self._health_tracker.order_for("youtube", candidates)

        tracker = RouteAttemptTracker()
        if initial_error:
            tracker.record("מנוע מקומי (yt-dlp)", summarize_ytdlp_failure(initial_error))

        attempted_providers: set[str] = set()

        for provider in ordered_providers:
            if cancel_token is not None and cancel_token.is_set():
                break
            if not provider.matches(url):
                logger.info("Skipping YouTube provider %s for %s: URL capability not supported", provider.name, url)
                continue
            if provider.name.lower() in attempted_providers:
                continue
            attempted_providers.add(provider.name.lower())

            start_time = time.monotonic()
            try:
                logger.info("Attempting YouTube provider %s for %s", provider.name, url)
                res = await provider.fetch(url)
                dl_result = await download_provider_media(
                    res,
                    dest_dir=dest_dir,
                    max_size=self._max_download_size,
                    cancel_token=cancel_token,
                )
                elapsed = time.monotonic() - start_time
                self._health_tracker.record_success(provider.name, "youtube", elapsed)
                logger.info(
                    "YouTube provider %s succeeded in %.2fs for %s",
                    provider.name,
                    elapsed,
                    url,
                )
                return dl_result
            except DownloadTooLargeError:
                raise
            except Exception as prov_exc:  # noqa: BLE001 - any provider failure must fall through to next candidate
                elapsed = time.monotonic() - start_time
                logger.warning(
                    "YouTube provider %s failed for %s (took %.2fs): %s",
                    provider.name,
                    url,
                    elapsed,
                    prov_exc,
                )
                self._health_tracker.record_failure(provider.name, "youtube", str(prov_exc))
                tracker.record(provider.name, summarize_provider_failure(prov_exc))

        if tracker.attempts:
            raise YouTubeDownloadError(tracker.format_summary())
        return None

    def _build_ydl_opts(
        self,
        dest_dir: Path,
        loop: asyncio.AbstractEventLoop,
        cancel_token: CancellationToken | None = None,
        guard: DownloadGuard | None = None,
    ) -> dict:
        is_playlist_request = self._is_playlist
        guard = guard or DownloadGuard(self._max_download_size, cancel_token)
        opts: dict = {
            "format": build_format_selector(self._quality),
            "outtmpl": str(dest_dir / "%(title).150s [%(id)s].%(ext)s"),
            "merge_output_format": "mp4",
            "noplaylist": not is_playlist_request,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "progress_hooks": [self._make_progress_hook(loop, cancel_token, guard)],
            "match_filter": guard.match_filter(),
            "retries": TRANSPORT_RETRIES,
            "fragment_retries": TRANSPORT_RETRIES,
            "ignoreerrors": "only_download" if is_playlist_request else False,
        }
        opts.update(self._connection_opts())
        if is_playlist_request and self._playlist_item_limit is not None:
            opts["playlistend"] = self._playlist_item_limit
        if self._quality == "audio":
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ]
        if self._subtitles and not is_playlist_request and self._quality != "audio":
            # A subtitle that cannot be fetched is a yt-dlp warning, not a
            # download failure, so it cannot cost the user their video.
            opts["writesubtitles"] = True
            opts["writeautomaticsub"] = True
            opts["subtitleslangs"] = list(SUBTITLE_LANGS)
            opts["subtitlesformat"] = "srt/best"
            opts.setdefault("postprocessors", []).append(
                {"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"}
            )
        return opts

    def _connection_opts(self) -> dict:
        """The yt-dlp options that decide *how we reach YouTube* (JS runtime,
        cookies, IPv4, player client, PO token) - shared by the real download
        and the metadata-only lookup so both look like the same client."""
        opts: dict = {"js_runtimes": self._js_runtimes}
        if self._remote_components:
            opts["remote_components"] = self._remote_components
        if self._force_ipv4:
            opts["source_address"] = "0.0.0.0"
        if self._cookies_file:
            opts["cookiefile"] = self._cookies_file

        client = self._player_client
        youtube_args = [f"player_client={client}"]
        if self._po_token:
            if "+" in self._po_token:
                youtube_args.append(f"po_token={self._po_token}")
            else:
                primary_client = client.split(",")[0].strip()
                youtube_args.append(f"po_token={primary_client}+{self._po_token}")
        opts["extractor_args"] = {"youtube": youtube_args}
        return opts

    def info_opts(self, url: str) -> dict:
        """Options for a metadata-only lookup (no download, no progress hooks)."""
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "skip_download": True,
            "noplaylist": not is_playlist_url(url),
            "socket_timeout": 10,
            "retries": 0,
        }
        if is_playlist_url(url):
            opts["extract_flat"] = "in_playlist"
        opts.update(self._connection_opts())
        return opts

    def _make_progress_hook(
        self,
        loop: asyncio.AbstractEventLoop,
        cancel_token: CancellationToken | None = None,
        guard: DownloadGuard | None = None,
    ):
        state = {"last_forward": 0.0}
        guard = guard or DownloadGuard(self._max_download_size, cancel_token)

        def hook(d: dict) -> None:
            guard.check(d)
            if self._progress is None:
                return
            text = format_progress_text(d)
            if text is None:
                return
            now = time.monotonic()
            if d.get("status") != "downloading" or now - state["last_forward"] >= _PROGRESS_THROTTLE_SECONDS:
                state["last_forward"] = now
                asyncio.run_coroutine_threadsafe(self._progress.update(text), loop)

        return hook

    def _download_sync(
        self,
        url: str,
        dest_dir: Path,
        loop: asyncio.AbstractEventLoop,
        cancel_token: CancellationToken | None = None,
    ) -> DownloadResult:
        guard = DownloadGuard(self._max_download_size, cancel_token)
        ydl_opts = self._build_ydl_opts(dest_dir, loop, cancel_token, guard)
        last_message: str | None = None
        try:
            for attempt in range(self._max_retries + 1):
                if cancel_token is not None and cancel_token.is_set():
                    return DownloadResult(file_paths=[])
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                    return self._result_or_too_large(info, guard)
                except DownloadCancelledSignal:
                    logger.info("YouTube download cancelled by timeout budget")
                    return DownloadResult(file_paths=[])
                except DownloadTooLargeSignal as exc:
                    raise too_large_error(exc) from exc
                except yt_dlp.utils.DownloadError as exc:
                    if isinstance(exc.__cause__, DownloadCancelledSignal):
                        logger.info("YouTube download cancelled by timeout budget")
                        return DownloadResult(file_paths=[])
                    if isinstance(exc.__cause__, DownloadTooLargeSignal):
                        raise too_large_error(exc.__cause__) from exc
                    if guard.oversize:
                        raise too_large_error(guard.oversize[0]) from exc
                    last_message = str(exc)
                    if attempt < self._max_retries and is_retryable_error(last_message):
                        logger.warning(
                            "Retrying YouTube download (attempt %s/%s) for %s: %s",
                            attempt + 1,
                            self._max_retries,
                            url,
                            last_message,
                        )
                        time.sleep(min(2**attempt, 8))
                        continue
                    raise YouTubeDownloadError(
                        classify_youtube_error(last_message), original_error=last_message
                    ) from exc
            raise YouTubeDownloadError(classify_youtube_error(last_message), original_error=last_message)
        finally:
            remove_partial_files(dest_dir)

    def _result_or_too_large(self, info: dict, guard: DownloadGuard) -> DownloadResult:
        try:
            return _result_from_info(
                info,
                playlist_item_limit=self._playlist_item_limit,
                too_large_count=len(guard.oversize),
            )
        except YouTubeDownloadError:
            # Nothing was downloaded. If that is because everything hit the
            # size cap (yt-dlp swallowed the abort under a playlist's
            # ignoreerrors, or the single video was skipped), say so, with the
            # detected size and the configured limit, instead of "no media".
            if guard.oversize:
                raise too_large_error(guard.oversize[0]) from None
            raise
