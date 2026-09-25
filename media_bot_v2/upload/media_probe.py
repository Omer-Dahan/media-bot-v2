"""ffprobe/ffmpeg helpers that give Telegram what it needs to show a file as a
playable video: duration, width, height and a thumbnail.

Telethon fills these in itself only when `hachoir` is installed, and it is
not a dependency of this project, so without this module every video would
reach the user with no duration, no thumbnail and (often) no inline player.
Everything here is synchronous and blocking; callers run it in a thread.

A failed probe or thumbnail is never an error - the file is still delivered,
just with less metadata (`kind="other"` / no thumbnail).
"""

from __future__ import annotations

import json
import logging
import subprocess
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

import filetype

logger = logging.getLogger(__name__)

KIND_VIDEO = "video"
KIND_AUDIO = "audio"
KIND_PHOTO = "photo"
KIND_OTHER = "other"

_PROBE_TIMEOUT_SECONDS = 60
_THUMB_TIMEOUT_SECONDS = 60
_THUMB_MAX_SIDE = 320  # Telegram: a thumbnail must not exceed 320px on either side
_THUMB_MAX_BYTES = 200 * 1024  # ...nor 200KB
_THUMB_MIN_BYTES = 100  # anything smaller is a broken frame grab (same guard as the old bot)


@dataclass(frozen=True)
class MediaInfo:
    kind: str = KIND_OTHER
    duration: int = 0
    width: int = 0
    height: int = 0
    thumb_path: Path | None = None

    def with_thumb(self, thumb_path: Path | None) -> MediaInfo:
        return replace(self, thumb_path=thumb_path)


def probe(path: Path) -> MediaInfo:
    """Classify `path` and read its duration/resolution. Never raises."""
    guessed = filetype.guess(str(path))
    if guessed is not None and guessed.mime.startswith("image/"):
        return MediaInfo(kind=KIND_PHOTO)

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,width,height:stream_disposition=attached_pic",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        data = json.loads(result.stdout or "{}")
    except (subprocess.SubprocessError, OSError, ValueError):
        logger.warning("ffprobe failed for %s", path, exc_info=True)
        return MediaInfo()

    streams = data.get("streams") or []
    # An mp3's embedded cover art shows up as a "video" stream; it is not one.
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration = _to_int((data.get("format") or {}).get("duration"))

    if video is not None:
        return MediaInfo(
            kind=KIND_VIDEO,
            duration=duration,
            width=_to_int(video.get("width")),
            height=_to_int(video.get("height")),
        )
    if has_audio:
        return MediaInfo(kind=KIND_AUDIO, duration=duration)
    return MediaInfo()


def make_thumb(path: Path, duration: int) -> Path | None:
    """Grab the middle frame as a JPEG that satisfies Telegram's thumbnail
    limits (<=320px, <=200KB). Returns None if that could not be done."""
    thumb = path.parent / f"{uuid.uuid4().hex}-thumb.jpg"
    scale = f"scale='if(gt(iw,ih),{_THUMB_MAX_SIDE},-2)':'if(gt(iw,ih),-2,{_THUMB_MAX_SIDE})'"
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                str(max(duration, 0) / 2),
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                scale,
                "-q:v",
                "5",
                str(thumb),
            ],
            check=True,
            capture_output=True,
            timeout=_THUMB_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, OSError):
        logger.warning("Thumbnail extraction failed for %s", path, exc_info=True)
        thumb.unlink(missing_ok=True)
        return None

    try:
        size = thumb.stat().st_size
    except OSError:
        return None
    if size < _THUMB_MIN_BYTES or size > _THUMB_MAX_BYTES:
        logger.warning("Discarding thumbnail for %s: unusable size %s bytes", path, size)
        thumb.unlink(missing_ok=True)
        return None
    return thumb


def probe_with_thumb(path: Path) -> MediaInfo:
    """`probe` plus a thumbnail for videos."""
    info = probe(path)
    if info.kind == KIND_VIDEO:
        return info.with_thumb(make_thumb(path, info.duration))
    return info


def _to_int(value: object) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
