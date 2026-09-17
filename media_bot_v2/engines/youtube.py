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
from urllib.parse import parse_qs, urlparse

import yt_dlp

from media_bot_v2.engines.base import BaseEngine, DownloadResult, DownloadTooLargeError
from media_bot_v2.telegram import texts

logger = logging.getLogger(__name__)

_QUALITY_HEIGHTS = {"1080": 1080, "720": 720, "480": 480, "360": 360}
_VIDEO_ID_RE = re.compile(r"^[0-9A-Za-z_-]{11}$")
_PROGRESS_THROTTLE_SECONDS = 2.0
_JS_RUNTIME_BINARIES = ("deno", "node", "bun")


class YouTubeDownloadError(Exception):
    """Raised with an already-classified, Hebrew, user-facing message."""


class _DownloadTooLargeSignal(Exception):
    """Raised from inside a yt-dlp progress hook to abort mid-download once
    the reported total exceeds the configured cap - converted to
    DownloadTooLargeError once it surfaces back in `_download_sync`."""


def matches_youtube_url(url: str) -> bool:
    lowered = url.lower()
    return "youtube.com" in lowered or "youtu.be" in lowered


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
        ),
        (
            "שגיאת פענוח ביוטיוב: חסר בשרת runtime של JavaScript (Node.js או Deno) הנדרש "
            "לפענוח חתימות יוטיוב.\nיש להתקין Node.js (גרסה 22 ומעלה) או Deno בשרת."
        ),
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
    if not message:
        return "ההורדה נכשלה: לא התקבל קובץ מדיה מיוטיוב."
    lowered = message.lower()
    for keywords, hebrew in _ERROR_PATTERNS:
        if any(keyword in lowered for keyword in keywords):
            return hebrew
    if any(pattern in lowered for pattern in _NETWORK_PATTERNS):
        return "שגיאת רשת בהורדה מיוטיוב. נסה שוב בעוד מספר רגעים."
    return f"ההורדה מיוטיוב נכשלה: {message[:200]}"


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


def _result_from_info(info: dict) -> DownloadResult:
    if info.get("_type") == "playlist" or "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        file_paths: list[str] = []
        for entry in entries:
            file_paths.extend(_extract_file_paths(entry))
        title = info.get("title") or "YouTube playlist"
    else:
        file_paths = _extract_file_paths(info)
        title = info.get("title") or "YouTube"

    if not file_paths:
        raise YouTubeDownloadError(classify_youtube_error(None))
    return DownloadResult(file_paths=file_paths, title=title)


class YouTubeEngine(BaseEngine):
    """One instance per download request - unlike DirectEngine, quality and
    the progress reporter vary per request, not per process, so the router
    constructs a fresh instance for each `ytq:` callback."""

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
        max_retries: int = 2,
        player_client: str | None = None,
        js_runtimes: dict[str, dict] | list[str] | str | None = None,
        remote_components: list[str] | set[str] | dict | str | None = None,
    ) -> None:
        if playlist_item_limit is not None:
            if playlist_item_limit <= 0:
                raise ValueError(f"playlist_item_limit must be positive, got {playlist_item_limit}")
            is_playlist = True
        self._quality = quality
        self._max_download_size = max_download_size
        self._progress = progress
        self._force_ipv4 = force_ipv4
        self._cookies_file = cookies_file
        self._po_token = po_token
        self._is_playlist = is_playlist
        self._playlist_item_limit = playlist_item_limit
        self._max_retries = max_retries
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

    async def download(self, url: str, *, dest_dir: Path) -> DownloadResult:
        dest_dir.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()
        return await asyncio.to_thread(self._download_sync, url, dest_dir, loop)

    def _build_ydl_opts(self, dest_dir: Path, loop: asyncio.AbstractEventLoop) -> dict:
        is_playlist_request = self._is_playlist
        opts: dict = {
            "format": build_format_selector(self._quality),
            "outtmpl": str(dest_dir / "%(title).150s [%(id)s].%(ext)s"),
            "merge_output_format": "mp4",
            "noplaylist": not is_playlist_request,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [self._make_progress_hook(loop)],
            "max_filesize": self._max_download_size,
            "retries": 3,
            "fragment_retries": 3,
            "ignoreerrors": "only_download" if is_playlist_request else False,
            "js_runtimes": self._js_runtimes,
        }
        if self._remote_components:
            opts["remote_components"] = self._remote_components
        if is_playlist_request and self._playlist_item_limit is not None:
            opts["playlistend"] = self._playlist_item_limit
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

    def _make_progress_hook(self, loop: asyncio.AbstractEventLoop):
        state = {"last_forward": 0.0}

        def hook(d: dict) -> None:
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total and total > self._max_download_size:
                    raise _DownloadTooLargeSignal(
                        f"{total} bytes exceeds the {self._max_download_size} byte limit"
                    )
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

    def _download_sync(self, url: str, dest_dir: Path, loop: asyncio.AbstractEventLoop) -> DownloadResult:
        ydl_opts = self._build_ydl_opts(dest_dir, loop)
        last_message: str | None = None
        for attempt in range(self._max_retries + 1):
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                return _result_from_info(info)
            except _DownloadTooLargeSignal as exc:
                raise DownloadTooLargeError(str(exc)) from exc
            except yt_dlp.utils.DownloadError as exc:
                if isinstance(exc.__cause__, _DownloadTooLargeSignal):
                    raise DownloadTooLargeError(str(exc.__cause__)) from exc
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
                raise YouTubeDownloadError(classify_youtube_error(last_message)) from exc
        raise YouTubeDownloadError(classify_youtube_error(last_message))
