"""HLS (m3u8) download engine core: playlist parsing, guarded fetch, local
rewrite, and local ffmpeg remux.

Milestone 1 scope only (spec/PLAN-M3U8.md section 13): finite
(EXT-X-ENDLIST) clear-stream VOD playlists, MPEG-TS and fMP4/CMAF. Live
playlists (no EXT-X-ENDLIST) and SAMPLE-AES/DRM/AES-128-protected playlists
raise HlsUnsupportedError rather than being downloaded. Byte-range-aware
discontinuity splitting and DVR/live-window handling are later milestones.

Not wired into the router/DirectEngine yet - this module is exercised
directly by tests/test_hls_engine.py.

Every playlist, variant, init-map, and segment fetch goes through
ssrf_guard.safe_request, with relative URIs resolved against the response's
final (post-redirect) URL before that next fetch. ffmpeg only ever sees
local file paths inside the task directory, never a remote URL.
"""

from __future__ import annotations

import logging
import re
import shutil
import statistics
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

from media_bot_v2.engines.base import (
    CancellationToken,
    DownloadResult,
    DownloadTooLargeError,
    UnsupportedUrlError,
)
from media_bot_v2.engines.ssrf_guard import safe_request

logger = logging.getLogger(__name__)

_HLS_CONTENT_TYPES = frozenset(
    {
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
        "audio/mpegurl",
        "audio/x-mpegurl",
    }
)
_REQUEST_TIMEOUT = 30
_CHUNK_SIZE = 1024 * 1024
_MAX_NESTED_HOPS = 5
_DEFAULT_FFMPEG_TIMEOUT = 180.0
_DEFAULT_MAX_HEIGHT = 720
_DEFAULT_SEGMENT_WORKERS = 8
_DEFAULT_MAX_GAP_RATIO = 0.02
_DEFAULT_WINDOW_SAMPLE_REQUESTS = 6
_BOM_UTF8 = b"\xef\xbb\xbf"


class HlsUnsupportedError(UnsupportedUrlError):
    """Raised for an HLS input this milestone does not handle: live (no
    EXT-X-ENDLIST), SAMPLE-AES/DRM/AES-128-protected, or malformed. `kind`
    is a stable machine-readable string for telegram/retry.py to classify."""

    def __init__(self, kind: str, message: str | None = None) -> None:
        self.kind = kind
        super().__init__(message or f"Unsupported HLS input: {kind}")


class HlsDownloadError(Exception):
    """Raised for a transient HLS fetch/remux failure - still worth a retry."""


# ---------------------------------------------------------------------------
# Detection (no body written as media until one of these says so)
# ---------------------------------------------------------------------------


def is_hls_path(url: str) -> bool:
    """Case-insensitive `.m3u8` on the URL's path; query strings don't count."""
    return urlparse(url).path.lower().endswith(".m3u8")


def is_hls_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    return content_type.split(";")[0].strip().lower() in _HLS_CONTENT_TYPES


def looks_like_hls_body(head: bytes) -> bool:
    """True if head starts with `#EXTM3U` after an optional UTF-8 BOM and
    leading ASCII whitespace."""
    body = head.removeprefix(_BOM_UTF8)
    return body.lstrip(b" \t\r\n").startswith(b"#EXTM3U")


def detect_hls(url: str, content_type: str | None = None, body_head: bytes | None = None) -> bool:
    """Combines all three detection signals in the order section 3 of the
    plan specifies: URL extension, then declared Content-Type, then a
    buffered body signature."""
    if is_hls_path(url):
        return True
    if is_hls_content_type(content_type):
        return True
    return body_head is not None and looks_like_hls_body(body_head)


# ---------------------------------------------------------------------------
# Playlist model
# ---------------------------------------------------------------------------


@dataclass
class Variant:
    uri: str
    bandwidth: int | None
    height: int | None
    width: int | None
    audio_group: str | None


@dataclass
class MasterPlaylist:
    variants: list[Variant]
    media_groups: dict[str, list[dict[str, str]]] = field(default_factory=dict)


@dataclass
class KeyInfo:
    method: str
    uri: str | None
    iv: str | None
    keyformat: str | None


@dataclass
class Segment:
    uri: str
    duration: float
    byterange: tuple[int, int] | None
    key: KeyInfo | None
    discontinuity: bool
    sequence: int = 0
    program_date_time: str | None = None


@dataclass
class MediaPlaylist:
    segments: list[Segment]
    has_endlist: bool
    map_uri: str | None
    map_byterange: tuple[int, int] | None
    media_sequence: int = 0
    has_program_date_time: bool = False


@dataclass(frozen=True)
class HlsWindowInfo:
    segment_count: int
    window_duration: float
    first_media_sequence: int
    last_media_sequence: int
    selected_variant_uri: str | None
    selected_variant_resolution: tuple[int, int] | None
    selected_variant_bandwidth: int | None
    has_program_date_time: bool
    estimated_bytes: int | None
    size_is_estimate: bool = True


@dataclass(frozen=True)
class HlsWindowMetadata:
    selected_duration: float
    delivered_duration: float
    gap_count: int
    missing_duration: float


@dataclass
class HlsWindowDownloadResult(DownloadResult):
    window: HlsWindowMetadata | None = None


_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _parse_attributes(rest: str) -> dict[str, str]:
    """Parses a comma-separated attribute list honouring quoted strings -
    a naive split(",") would break on a quoted value that itself contains a
    comma (e.g. CODECS="avc1.4d001f,mp4a.40.2")."""
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(rest):
        value = match.group(2)
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        attrs[match.group(1)] = value
    return attrs


def _parse_byterange(value: str) -> tuple[int, int]:
    value = value.strip()
    try:
        if "@" in value:
            length_str, offset_str = value.split("@", 1)
            return int(length_str), int(offset_str)
        return int(value), 0
    except ValueError as exc:
        raise HlsUnsupportedError("malformed", f"invalid BYTERANGE {value!r}") from exc


def _variant_from_attrs(attrs: dict[str, str], uri: str) -> Variant:
    height = width = None
    resolution = attrs.get("RESOLUTION")
    if resolution and "x" in resolution.lower():
        try:
            width_str, height_str = resolution.lower().split("x", 1)
            width, height = int(width_str), int(height_str)
        except ValueError:
            width = height = None
    bandwidth = None
    if attrs.get("BANDWIDTH"):
        try:
            bandwidth = int(attrs["BANDWIDTH"])
        except ValueError:
            bandwidth = None
    return Variant(
        uri=uri, bandwidth=bandwidth, height=height, width=width, audio_group=attrs.get("AUDIO")
    )


def parse_playlist(text: str) -> MasterPlaylist | MediaPlaylist:
    """Parses a master or media playlist body. Raises HlsUnsupportedError
    (kind="malformed") for a missing header, an invalid attribute value, a
    URI line with no preceding EXT-X-STREAM-INF/EXTINF, or an ambiguous
    master/media tag mixture."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise HlsUnsupportedError("malformed", "missing #EXTM3U header")

    variants: list[Variant] = []
    media_groups: dict[str, list[dict[str, str]]] = {}
    segments: list[Segment] = []
    has_endlist = False
    has_stream_inf = False
    has_segment_tag = False
    map_uri: str | None = None
    map_byterange: tuple[int, int] | None = None
    pending_stream_inf: dict[str, str] | None = None
    pending_extinf: float | None = None
    pending_byterange: tuple[int, int] | None = None
    pending_discontinuity = False
    active_key: KeyInfo | None = None
    media_sequence = 0
    pending_program_date_time: str | None = None
    has_program_date_time = False

    for line in lines[1:]:
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending_stream_inf = _parse_attributes(line[len("#EXT-X-STREAM-INF:") :])
            has_stream_inf = True
        elif line.startswith("#EXT-X-MEDIA:"):
            attrs = _parse_attributes(line[len("#EXT-X-MEDIA:") :])
            group_id = attrs.get("GROUP-ID")
            if group_id:
                media_groups.setdefault(group_id, []).append(attrs)
        elif line.startswith("#EXT-X-KEY:"):
            attrs = _parse_attributes(line[len("#EXT-X-KEY:") :])
            method = attrs.get("METHOD", "NONE").upper()
            active_key = (
                None
                if method == "NONE"
                else KeyInfo(
                    method=method,
                    uri=attrs.get("URI"),
                    iv=attrs.get("IV"),
                    keyformat=attrs.get("KEYFORMAT"),
                )
            )
        elif line.startswith("#EXT-X-MAP:"):
            attrs = _parse_attributes(line[len("#EXT-X-MAP:") :])
            map_uri = attrs.get("URI")
            if not map_uri:
                raise HlsUnsupportedError("malformed", "EXT-X-MAP without URI")
            if attrs.get("BYTERANGE"):
                map_byterange = _parse_byterange(attrs["BYTERANGE"])
        elif line.startswith("#EXTINF:"):
            duration_str = line[len("#EXTINF:") :].split(",", 1)[0]
            try:
                pending_extinf = float(duration_str)
            except ValueError as exc:
                raise HlsUnsupportedError("malformed", f"invalid EXTINF {duration_str!r}") from exc
            has_segment_tag = True
        elif line.startswith("#EXT-X-BYTERANGE:"):
            pending_byterange = _parse_byterange(line[len("#EXT-X-BYTERANGE:") :])
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line[len("#EXT-X-MEDIA-SEQUENCE:") :])
            except ValueError as exc:
                raise HlsUnsupportedError("malformed", "invalid media sequence") from exc
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pending_program_date_time = line[len("#EXT-X-PROGRAM-DATE-TIME:") :]
            has_program_date_time = True
        elif line.startswith("#EXT-X-DISCONTINUITY"):
            pending_discontinuity = True
        elif line.startswith("#EXT-X-ENDLIST"):
            has_endlist = True
        elif line.startswith("#"):
            continue
        else:
            if pending_stream_inf is not None:
                variants.append(_variant_from_attrs(pending_stream_inf, line))
                pending_stream_inf = None
            elif pending_extinf is not None:
                segments.append(
                    Segment(
                        uri=line,
                        duration=pending_extinf,
                        byterange=pending_byterange,
                        key=active_key,
                        discontinuity=pending_discontinuity,
                        sequence=media_sequence + len(segments),
                        program_date_time=pending_program_date_time,
                    )
                )
                pending_extinf = None
                pending_byterange = None
                pending_discontinuity = False
                pending_program_date_time = None
            else:
                raise HlsUnsupportedError("malformed", f"unexpected URI line: {line!r}")

    if has_stream_inf and has_segment_tag:
        raise HlsUnsupportedError("malformed", "playlist mixes master and media tags")
    if has_stream_inf:
        if not variants:
            raise HlsUnsupportedError("malformed", "master playlist has no variants")
        return MasterPlaylist(variants=variants, media_groups=media_groups)
    if segments:
        return MediaPlaylist(
            segments=segments,
            has_endlist=has_endlist,
            map_uri=map_uri,
            map_byterange=map_byterange,
            media_sequence=media_sequence,
            has_program_date_time=has_program_date_time,
        )
    raise HlsUnsupportedError("malformed", "playlist has neither variants nor segments")


def select_variant(master: MasterPlaylist, max_height: int) -> Variant:
    """Highest declared RESOLUTION height <= max_height; else the lowest
    available video variant; else the highest-BANDWIDTH variant; else the
    first variant in playlist order (spec/PLAN-M3U8.md section 5)."""
    with_height = [v for v in master.variants if v.height is not None]
    under_cap = [v for v in with_height if v.height <= max_height]
    if under_cap:
        return max(under_cap, key=lambda v: (v.height, v.bandwidth or 0))
    if with_height:
        return min(with_height, key=lambda v: (v.height, v.bandwidth or 0))
    with_bandwidth = [v for v in master.variants if v.bandwidth is not None]
    if with_bandwidth:
        return max(with_bandwidth, key=lambda v: v.bandwidth)
    return master.variants[0]


def select_audio_rendition(
    master: MasterPlaylist, audio_group: str | None
) -> dict[str, str] | None:
    """Preferred EXT-X-MEDIA TYPE=AUDIO rendition for a variant's AUDIO
    group: DEFAULT=YES, else AUTOSELECT=YES, else first in playlist order."""
    if not audio_group:
        return None
    candidates = [
        attrs
        for attrs in master.media_groups.get(audio_group, [])
        if attrs.get("TYPE", "").upper() == "AUDIO" and attrs.get("URI")
    ]
    if not candidates:
        return None
    for attrs in candidates:
        if attrs.get("DEFAULT", "").upper() == "YES":
            return attrs
    for attrs in candidates:
        if attrs.get("AUTOSELECT", "").upper() == "YES":
            return attrs
    return candidates[0]


def _check_key_support(key: KeyInfo | None) -> None:
    """Milestone 1 supports clear streams only (METHOD=NONE). Any active
    key is classified and refused before a single segment/key byte is
    fetched (spec/PLAN-M3U8.md section 8)."""
    if key is None:
        return
    method = key.method.upper()
    if method in ("SAMPLE-AES", "SAMPLE-AES-CTR"):
        raise HlsUnsupportedError("sample_aes_unsupported", "SAMPLE-AES is not supported")
    keyformat = (key.keyformat or "identity").lower()
    if keyformat != "identity" or (key.uri or "").lower().startswith("skd://"):
        raise HlsUnsupportedError("drm_unsupported", "DRM-protected streams are not supported")
    raise HlsUnsupportedError(
        "encryption_unsupported", f"{method} encryption is not supported in this milestone"
    )


def _check_media_playlist_support(media: MediaPlaylist) -> None:
    if not media.has_endlist:
        raise HlsUnsupportedError(
            "live", "live playlists (no EXT-X-ENDLIST) are not supported in this milestone"
        )
    for segment in media.segments:
        _check_key_support(segment.key)


# ---------------------------------------------------------------------------
# Guarded fetch
# ---------------------------------------------------------------------------


def _guarded_get(url: str, *, stream: bool = False, headers: dict[str, str] | None = None):
    response = safe_request("GET", url, stream=stream, timeout=_REQUEST_TIMEOUT, headers=headers)
    response.raise_for_status()
    return response


def _decode_body(response) -> str:
    return response.content.decode("utf-8-sig", errors="replace")


def _resolve_media_playlist(
    url: str, max_height: int
) -> tuple[MediaPlaylist, str, MasterPlaylist | None, str | None]:
    """Follows a playlist URL - which may itself be a master - down to its
    selected media playlist, guard-fetching and resolving each hop against
    the previous response's final (post-redirect) URL. Returns the media
    playlist, its final URL, the outermost master (if any, for audio-group
    lookup), and the selected variant's AUDIO group id (if any)."""
    outer_master: MasterPlaylist | None = None
    audio_group: str | None = None
    visited: set[str] = set()
    current_url = url
    for _ in range(_MAX_NESTED_HOPS):
        if current_url in visited:
            raise HlsUnsupportedError("malformed", "playlist cycle detected")
        visited.add(current_url)
        response = _guarded_get(current_url)
        final_url = response.url
        doc = parse_playlist(_decode_body(response))
        if isinstance(doc, MediaPlaylist):
            return doc, final_url, outer_master, audio_group
        if outer_master is None:
            outer_master = doc
            variant = select_variant(doc, max_height)
            audio_group = variant.audio_group
        else:
            variant = select_variant(doc, max_height)
        current_url = urljoin(final_url, variant.uri)
    raise HlsUnsupportedError("malformed", "too many nested playlists")


def _resolve_window_playlist(
    url: str, max_height: int
) -> tuple[MediaPlaylist, str, Variant | None]:
    selected: Variant | None = None
    visited: set[str] = set()
    current_url = url
    for _ in range(_MAX_NESTED_HOPS):
        if current_url in visited:
            raise HlsUnsupportedError("malformed", "playlist cycle detected")
        visited.add(current_url)
        response = _guarded_get(current_url)
        final_url = response.url
        doc = parse_playlist(_decode_body(response))
        if isinstance(doc, MediaPlaylist):
            return doc, final_url, selected
        selected = select_variant(doc, max_height)
        current_url = urljoin(final_url, selected.uri)
    raise HlsUnsupportedError("malformed", "too many nested playlists")


def _sample_indices(count: int, limit: int) -> list[int]:
    sample_count = min(count, limit)
    if sample_count <= 1:
        return [0] if count else []
    return sorted(
        {round(index * (count - 1) / (sample_count - 1)) for index in range(sample_count)}
    )


def analyze_hls_window(
    url: str,
    *,
    max_height: int = _DEFAULT_MAX_HEIGHT,
    sample_requests: int = _DEFAULT_WINDOW_SAMPLE_REQUESTS,
) -> HlsWindowInfo:
    """Fetch one live snapshot and estimate its size without writing media."""
    media, media_url, variant = _resolve_window_playlist(url, max_height)
    if media.has_endlist:
        raise HlsUnsupportedError("malformed", "playlist is finite, not a live window")
    for segment in media.segments:
        _check_key_support(segment.key)
    if not media.segments:
        raise HlsUnsupportedError("window_too_short")

    lengths: list[int] = []
    for index in _sample_indices(len(media.segments), max(3, min(10, sample_requests))):
        segment_url = urljoin(media_url, media.segments[index].uri)
        response = safe_request("HEAD", segment_url, timeout=_REQUEST_TIMEOUT)
        try:
            if response.status_code >= 400:
                continue
            value = response.headers.get("Content-Length")
            if value is not None and int(value) > 0:
                lengths.append(int(value))
        except ValueError:
            continue
        finally:
            response.close()

    estimated_bytes = None
    if lengths:
        estimated_bytes = int(statistics.median(lengths) * len(media.segments))
    resolution = None
    if variant is not None and variant.width is not None and variant.height is not None:
        resolution = (variant.width, variant.height)
    return HlsWindowInfo(
        segment_count=len(media.segments),
        window_duration=sum(segment.duration for segment in media.segments),
        first_media_sequence=media.segments[0].sequence,
        last_media_sequence=media.segments[-1].sequence,
        selected_variant_uri=variant.uri if variant else None,
        selected_variant_resolution=resolution,
        selected_variant_bandwidth=variant.bandwidth if variant else None,
        has_program_date_time=media.has_program_date_time,
        estimated_bytes=estimated_bytes,
    )


class _ByteCounter:
    """Cumulative observed response-body bytes across every playlist, map,
    and segment fetch for one job - never Content-Length, playlist
    duration, or BANDWIDTH (spec/PLAN-M3U8.md section 7)."""

    def __init__(self, max_size: int | None) -> None:
        self.total = 0
        self.max_size = max_size
        self._lock = threading.Lock()

    def add(self, n: int) -> None:
        with self._lock:
            self.total += n
            if self.max_size is not None and self.total > self.max_size:
                raise DownloadTooLargeError(file_size=self.total, max_size=self.max_size)


def _fetch_to_file(
    url: str,
    dest: Path,
    counter: _ByteCounter,
    cancel_token: CancellationToken | None,
    *,
    byterange: tuple[int, int] | None = None,
) -> None:
    headers: dict[str, str] | None = None
    if byterange is not None:
        length, offset = byterange
        headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
    part_path = dest.with_name(dest.name + ".part")
    response = _guarded_get(url, stream=True, headers=headers)
    try:
        if cancel_token is not None and cancel_token.is_set():
            raise HlsDownloadError("cancelled")
        with open(part_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                if cancel_token is not None and cancel_token.is_set():
                    raise HlsDownloadError("cancelled")
                if not chunk:
                    continue
                counter.add(len(chunk))
                f.write(chunk)
    except Exception:
        part_path.unlink(missing_ok=True)
        raise
    finally:
        response.close()
    part_path.rename(dest)


def _segment_extension(uri: str) -> str:
    suffix = Path(urlparse(uri).path).suffix.lower()
    if suffix in (".ts", ".m4s", ".mp4", ".aac"):
        return suffix
    return ".ts"


def _render_media_playlist(
    media: MediaPlaylist, map_local_name: str | None, segment_local_names: list[str]
) -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    if map_local_name is not None:
        lines.append(f'#EXT-X-MAP:URI="{map_local_name}"')
    for segment, local_name in zip(media.segments, segment_local_names, strict=True):
        if segment.discontinuity:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f"#EXTINF:{segment.duration:.6f},")
        lines.append(local_name)
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _assert_local_only(playlist_text: str) -> None:
    """Defense in depth: a rewritten playlist that still contains a
    scheme-qualified reference is an implementation error, not something
    ffmpeg should ever be allowed to try (spec/PLAN-M3U8.md section 7)."""
    if "://" in playlist_text:
        raise HlsDownloadError("internal error: rewritten playlist still references a remote URL")


def _materialize_media_playlist(
    media: MediaPlaylist,
    base_url: str,
    track_dir: Path,
    counter: _ByteCounter,
    cancel_token: CancellationToken | None,
    created_files: list[Path],
) -> Path:
    """Guard-fetches the init map (if any) and every segment into track_dir,
    then writes a local media playlist referencing them by opaque relative
    names - never the original remote URI paths."""
    track_dir.mkdir(parents=True, exist_ok=True)

    map_local_name: str | None = None
    if media.map_uri is not None:
        map_abs_url = urljoin(base_url, media.map_uri)
        map_path = track_dir / "init.mp4"
        _fetch_to_file(map_abs_url, map_path, counter, cancel_token, byterange=media.map_byterange)
        created_files.append(map_path)
        map_local_name = map_path.name

    segment_local_names: list[str] = []
    for index, segment in enumerate(media.segments):
        if cancel_token is not None and cancel_token.is_set():
            raise HlsDownloadError("cancelled")
        seg_url = urljoin(base_url, segment.uri)
        local_name = f"seg{index:05d}{_segment_extension(segment.uri)}"
        seg_path = track_dir / local_name
        _fetch_to_file(seg_url, seg_path, counter, cancel_token, byterange=segment.byterange)
        created_files.append(seg_path)
        segment_local_names.append(local_name)

    playlist_text = _render_media_playlist(media, map_local_name, segment_local_names)
    _assert_local_only(playlist_text)
    playlist_path = track_dir / "playlist.m3u8"
    playlist_path.write_text(playlist_text, encoding="utf-8")
    created_files.append(playlist_path)
    return playlist_path


def _run_process(cmd: list[str], timeout: float, cancel_token: CancellationToken | None) -> None:
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if cancel_token is not None:
        cancel_token.on_cancel(process.terminate)
    try:
        _, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise HlsDownloadError("ffmpeg remux timed out") from exc
    if cancel_token is not None and cancel_token.is_set():
        raise HlsDownloadError("cancelled")
    if process.returncode:
        message = stderr.decode("utf-8", errors="replace") if stderr else ""
        logger.warning("ffmpeg HLS command failed: %s", message[-2000:])
        raise HlsDownloadError("ffmpeg remux failed")


def _run_ffmpeg_remux(
    video_playlist: Path,
    audio_playlist: Path | None,
    output_path: Path,
    timeout: float,
    cancel_token: CancellationToken | None = None,
) -> None:
    """Local-only remux: `-protocol_whitelist file,pipe` means ffmpeg cannot
    reach the network even if a rewrite bug left a remote URL behind."""
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "hls",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(video_playlist),
    ]
    if audio_playlist is not None:
        cmd += ["-f", "hls", "-protocol_whitelist", "file,pipe", "-i", str(audio_playlist)]
    cmd += ["-map", "0:v:0"]
    cmd += ["-map", "1:a:0"] if audio_playlist is not None else ["-map", "0:a:0?"]
    cmd += ["-c", "copy", "-sn", "-dn", "-movflags", "+faststart", str(output_path)]
    _run_process(cmd, timeout, cancel_token)


def _remove_dir_if_exists(path: Path) -> None:
    if path.is_dir():
        for child in path.iterdir():
            child.unlink(missing_ok=True)
        path.rmdir()


@dataclass(frozen=True)
class _DownloadedSegment:
    segment: Segment
    path: Path | None


def _select_window(segments: list[Segment], window_seconds: float | None) -> list[Segment]:
    if window_seconds is None:
        return list(segments)
    if window_seconds <= 0:
        raise HlsUnsupportedError("window_too_short")
    selected: list[Segment] = []
    duration = 0.0
    for segment in reversed(segments):
        selected.append(segment)
        duration += segment.duration
        if duration >= window_seconds - 1e-9:
            break
    selected.reverse()
    if not selected:
        raise HlsUnsupportedError("window_too_short")
    return selected


def _fetch_window_segment(
    segment: Segment,
    base_url: str,
    dest: Path,
    counter: _ByteCounter,
    cancel_token: CancellationToken | None,
) -> _DownloadedSegment:
    if cancel_token is not None and cancel_token.is_set():
        raise HlsDownloadError("cancelled")
    headers = None
    if segment.byterange is not None:
        length, offset = segment.byterange
        headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
    part = dest.with_name(dest.name + ".part")
    response = safe_request(
        "GET",
        urljoin(base_url, segment.uri),
        stream=True,
        timeout=_REQUEST_TIMEOUT,
        headers=headers,
    )
    if response.status_code in (404, 410):
        response.close()
        part.unlink(missing_ok=True)
        return _DownloadedSegment(segment, None)
    try:
        response.raise_for_status()
        with open(part, "wb") as stream:
            for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                if cancel_token is not None and cancel_token.is_set():
                    raise HlsDownloadError("cancelled")
                if chunk:
                    counter.add(len(chunk))
                    stream.write(chunk)
        if part.stat().st_size == 0:
            raise HlsDownloadError("empty HLS segment")
        part.rename(dest)
        return _DownloadedSegment(segment, dest)
    except Exception:
        part.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)
        raise
    finally:
        response.close()


def _download_segment_batch(
    segments: list[Segment],
    base_url: str,
    work_dir: Path,
    counter: _ByteCounter,
    cancel_token: CancellationToken | None,
    workers: int,
    existing: dict[int, _DownloadedSegment],
) -> None:
    pending = [segment for segment in segments if segment.sequence not in existing]
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hls-segment") as pool:
        futures = {}
        for segment in pending:
            path = work_dir / f"seg-{segment.sequence:020d}{_segment_extension(segment.uri)}"
            future = pool.submit(
                _fetch_window_segment,
                segment,
                base_url,
                path,
                counter,
                cancel_token,
            )
            futures[future] = segment.sequence
        for future in as_completed(futures):
            downloaded = future.result()
            existing[downloaded.segment.sequence] = downloaded


def _continuous_runs(downloaded: list[_DownloadedSegment]) -> list[list[_DownloadedSegment]]:
    runs: list[list[_DownloadedSegment]] = []
    current: list[_DownloadedSegment] = []
    previous_sequence: int | None = None
    for item in downloaded:
        if item.path is None:
            if current:
                runs.append(current)
                current = []
            previous_sequence = item.segment.sequence
            continue
        if current and (
            item.segment.discontinuity or item.segment.sequence != previous_sequence + 1
        ):
            runs.append(current)
            current = []
        current.append(item)
        previous_sequence = item.segment.sequence
    if current:
        runs.append(current)
    return runs


def _write_run_playlist(
    run: list[_DownloadedSegment], path: Path, map_local_name: str | None = None
) -> None:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    if map_local_name is not None:
        lines.append(f'#EXT-X-MAP:URI="{map_local_name}"')
    for item in run:
        lines.extend((f"#EXTINF:{item.segment.duration:.6f},", item.path.name))
    lines.append("#EXT-X-ENDLIST")
    text = "\n".join(lines) + "\n"
    _assert_local_only(text)
    path.write_text(text, encoding="utf-8")


def _probe_duration(path: Path) -> float:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(completed.stdout.strip())
    except (subprocess.SubprocessError, ValueError) as exc:
        raise HlsDownloadError("could not verify HLS output duration") from exc


def _concat_runs(
    run_outputs: list[Path],
    output_path: Path,
    timeout: float,
    cancel_token: CancellationToken | None,
) -> None:
    concat_path = run_outputs[0].parent / "concat.txt"
    concat_path.write_text(
        "".join(
            f"file '{path.name.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
            for path in run_outputs
        ),
        encoding="utf-8",
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "1",
        "-i",
        str(concat_path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    try:
        _run_process(cmd, timeout, cancel_token)
    finally:
        concat_path.unlink(missing_ok=True)


def download_hls_window(
    url: str,
    dest_dir: Path,
    max_size: int | None,
    cancel_token: CancellationToken | None,
    window_seconds: float | None = None,
    progress=None,
    *,
    max_height: int = _DEFAULT_MAX_HEIGHT,
    segment_workers: int = _DEFAULT_SEGMENT_WORKERS,
    max_gap_ratio: float = _DEFAULT_MAX_GAP_RATIO,
    ffmpeg_timeout: float = _DEFAULT_FFMPEG_TIMEOUT,
    _snapshot: tuple[MediaPlaylist, str] | None = None,
) -> HlsWindowDownloadResult:
    """Download one published live window and convert it to a finite MP4."""
    del progress  # Reserved for M2b progress reporting.
    dest_dir.mkdir(parents=True, exist_ok=True)
    work_dir = dest_dir / "_hls_window"
    output_path = dest_dir / "hls-download.mp4"
    counter = _ByteCounter(max_size)
    downloaded: dict[int, _DownloadedSegment] = {}
    try:
        if _snapshot is None:
            media, media_url, _ = _resolve_window_playlist(url, max_height)
        else:
            media, media_url = _snapshot
        if media.has_endlist:
            raise HlsUnsupportedError("malformed", "playlist is not live")
        for segment in media.segments:
            _check_key_support(segment.key)
        selected = _select_window(media.segments, window_seconds)
        if not selected:
            raise HlsUnsupportedError("window_too_short")
        selected_duration = sum(segment.duration for segment in selected)
        work_dir.mkdir(parents=True, exist_ok=True)
        map_local_name = None
        if media.map_uri is not None:
            map_path = work_dir / "init.mp4"
            _fetch_to_file(
                urljoin(media_url, media.map_uri),
                map_path,
                counter,
                cancel_token,
                byterange=media.map_byterange,
            )
            map_local_name = map_path.name
        _download_segment_batch(
            selected, media_url, work_dir, counter, cancel_token, segment_workers, downloaded
        )

        refreshed, refreshed_url, _ = _resolve_window_playlist(media_url, max_height)
        later = [
            segment for segment in refreshed.segments if segment.sequence > selected[-1].sequence
        ]
        if later:
            refreshed_selected = _select_window(refreshed.segments, window_seconds)
            if window_seconds is None:
                refreshed_selected = refreshed_selected[-len(selected) :]
            allowed = {segment.sequence for segment in refreshed_selected}
            new_tail = [segment for segment in later if segment.sequence in allowed]
            _download_segment_batch(
                new_tail,
                refreshed_url,
                work_dir,
                counter,
                cancel_token,
                segment_workers,
                downloaded,
            )
            selected = refreshed_selected

        selected_duration = sum(segment.duration for segment in selected)
        final_items = [
            downloaded.get(segment.sequence, _DownloadedSegment(segment, None))
            for segment in selected
        ]
        gaps = [item for item in final_items if item.path is None]
        missing_duration = sum(item.segment.duration for item in gaps)
        gap_ratio = len(gaps) / len(final_items) if final_items else 1.0
        if gap_ratio > max_gap_ratio:
            raise HlsUnsupportedError("window_expired")
        runs = _continuous_runs(final_items)
        if not runs:
            raise HlsUnsupportedError("window_too_short")

        run_outputs: list[Path] = []
        for index, run in enumerate(runs):
            playlist_path = work_dir / f"run-{index:03d}.m3u8"
            run_output = work_dir / f"run-{index:03d}.mp4"
            _write_run_playlist(run, playlist_path, map_local_name)
            _run_ffmpeg_remux(playlist_path, None, run_output, ffmpeg_timeout, cancel_token)
            run_outputs.append(run_output)
        if len(run_outputs) == 1:
            shutil.move(run_outputs[0], output_path)
        else:
            _concat_runs(run_outputs, output_path, ffmpeg_timeout, cancel_token)

        delivered_duration = sum(
            item.segment.duration for item in final_items if item.path is not None
        )
        actual_duration = _probe_duration(output_path)
        if (
            delivered_duration <= 0
            or abs(actual_duration - delivered_duration) / delivered_duration > 0.05
        ):
            raise HlsDownloadError("concatenated HLS duration drift exceeds 5 percent")
        metadata = HlsWindowMetadata(
            selected_duration=selected_duration,
            delivered_duration=delivered_duration,
            gap_count=len(gaps),
            missing_duration=missing_duration,
        )
    except Exception:
        output_path.unlink(missing_ok=True)
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    shutil.rmtree(work_dir, ignore_errors=True)
    return HlsWindowDownloadResult(
        file_paths=[str(output_path)], title=output_path.name, window=metadata
    )


def download_hls(
    url: str,
    dest_dir: Path,
    *,
    max_size: int | None = None,
    max_height: int = _DEFAULT_MAX_HEIGHT,
    cancel_token: CancellationToken | None = None,
    ffmpeg_timeout: float = _DEFAULT_FFMPEG_TIMEOUT,
    segment_workers: int = _DEFAULT_SEGMENT_WORKERS,
    max_gap_ratio: float = _DEFAULT_MAX_GAP_RATIO,
) -> DownloadResult:
    """Downloads a finite (EXT-X-ENDLIST) clear-stream HLS playlist into
    dest_dir and remuxes it locally into one MP4. Every playlist, variant,
    audio rendition, init-map, and segment fetch goes through
    ssrf_guard.safe_request; ffmpeg only ever sees local file paths.

    Raises HlsUnsupportedError for a live, encrypted/DRM, or malformed
    playlist, and DownloadTooLargeError if observed bytes exceed max_size.
    Leaves no partial files behind on any failure or cancellation."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    counter = _ByteCounter(max_size)
    created_files: list[Path] = []
    video_dir = dest_dir / "_hls_video"
    audio_dir = dest_dir / "_hls_audio"
    output_path = dest_dir / "hls-download.mp4"
    try:
        media, media_url, outer_master, audio_group = _resolve_media_playlist(url, max_height)
        if not media.has_endlist:
            return download_hls_window(
                url,
                dest_dir,
                max_size,
                cancel_token,
                max_height=max_height,
                segment_workers=segment_workers,
                max_gap_ratio=max_gap_ratio,
                ffmpeg_timeout=ffmpeg_timeout,
                _snapshot=(media, media_url),
            )
        _check_media_playlist_support(media)
        video_playlist = _materialize_media_playlist(
            media, media_url, video_dir, counter, cancel_token, created_files
        )

        audio_playlist: Path | None = None
        if outer_master is not None and audio_group:
            rendition = select_audio_rendition(outer_master, audio_group)
            if rendition is not None and rendition.get("URI"):
                audio_abs_url = urljoin(media_url, rendition["URI"])
                audio_media, audio_final_url, _, _ = _resolve_media_playlist(
                    audio_abs_url, max_height
                )
                _check_media_playlist_support(audio_media)
                audio_playlist = _materialize_media_playlist(
                    audio_media, audio_final_url, audio_dir, counter, cancel_token, created_files
                )

        if cancel_token is not None and cancel_token.is_set():
            raise HlsDownloadError("cancelled")

        _run_ffmpeg_remux(video_playlist, audio_playlist, output_path, ffmpeg_timeout)
    except Exception:
        output_path.unlink(missing_ok=True)
        for path in created_files:
            path.unlink(missing_ok=True)
        _remove_dir_if_exists(video_dir)
        _remove_dir_if_exists(audio_dir)
        raise
    else:
        for path in created_files:
            path.unlink(missing_ok=True)
        _remove_dir_if_exists(video_dir)
        _remove_dir_if_exists(audio_dir)

    return DownloadResult(file_paths=[str(output_path)], title=output_path.name)
