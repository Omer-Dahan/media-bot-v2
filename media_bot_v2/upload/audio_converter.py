"""Audio conversion and metadata embedding helpers for audio-only downloads.

Converts audio streams to high-quality MP3 (192kbps) with ID3v2 tags (title,
artist, album) and optional embedded cover art (attached picture).

If any metadata field or cover art is missing, it is omitted without error.
Intermediate files are cleaned up safely.
"""

from __future__ import annotations

import logging
import os
import subprocess
import uuid
from pathlib import Path

from media_bot_v2.upload.media_probe import KIND_VIDEO, make_thumb, probe

logger = logging.getLogger(__name__)

DEFAULT_AUDIO_BITRATE = "192k"
DEFAULT_CONVERT_TIMEOUT_SECONDS = 300.0


def build_mp3_convert_command(
    source: Path,
    target: Path,
    *,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    cover_path: Path | None = None,
    bitrate: str = DEFAULT_AUDIO_BITRATE,
) -> list[str]:
    """Build the ffmpeg command line to convert `source` to an MP3 at `target`
    with ID3v2 tags and optional embedded cover art.

    Metadata fields (title, artist, album) are included only when provided and
    non-empty; missing fields are omitted rather than invented. Cover art is
    embedded as an attached picture (disposition attached_pic) using mjpeg
    encoding so that arbitrary image formats (JPEG, PNG, WebP) are valid ID3 APIC frames.
    """
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source)]
    if cover_path is not None and cover_path.exists():
        cmd.extend(["-i", str(cover_path)])
        cmd.extend(["-map", "0:a:0", "-map", "1:v:0"])
        cmd.extend(["-c:a", "libmp3lame", "-b:a", bitrate])
        cmd.extend(["-c:v", "mjpeg", "-disposition:v:0", "attached_pic"])
    else:
        cmd.extend(["-map", "0:a:0"])
        cmd.extend(["-c:a", "libmp3lame", "-b:a", bitrate])

    if title and title.strip():
        cmd.extend(["-metadata", f"title={title.strip()}"])
    if artist and artist.strip():
        cmd.extend(["-metadata", f"artist={artist.strip()}"])
    if album and album.strip():
        cmd.extend(["-metadata", f"album={album.strip()}"])

    cmd.extend(["-id3v2_version", "3"])
    cmd.append(str(target))
    return cmd


def extract_cover_from_media(source: Path) -> Path | None:
    """Extract embedded cover art (attached_pic) from an audio file if present."""
    target = source.parent / f"{uuid.uuid4().hex}-extracted-cover.jpg"
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-an",
                "-c:v",
                "mjpeg",
                "-frames:v",
                "1",
                str(target),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        if target.exists() and target.stat().st_size > 100:
            return target
        target.unlink(missing_ok=True)
    except (subprocess.SubprocessError, OSError):
        target.unlink(missing_ok=True)
    return None


def resolve_cover_image(
    source: Path,
    *,
    duration: int = 0,
    explicit_thumb: Path | None = None,
) -> Path | None:
    """Find or extract a usable cover image for `source`.

    Order:
    1. Explicit thumb path if provided and exists.
    2. Image files written in `source.parent` (e.g. yt-dlp thumbnail).
    3. Video frame thumbnail if `source` contains a video stream.
    4. Embedded attached picture if `source` is an audio file with cover art.
    5. None if no cover image could be resolved.
    """
    if explicit_thumb is not None and explicit_thumb.exists() and explicit_thumb.stat().st_size > 0:
        return explicit_thumb

    parent = source.parent
    if parent.exists():
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
            for img in parent.glob(ext):
                if img.is_file() and not img.name.endswith("-thumb.jpg") and img.stat().st_size > 100:
                    return img

    info = probe(source)
    if info.kind == KIND_VIDEO:
        thumb = make_thumb(source, duration or info.duration)
        if thumb and thumb.exists():
            return thumb

    cover = extract_cover_from_media(source)
    if cover and cover.exists():
        return cover

    return None


def convert_to_mp3(
    source: Path,
    *,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    cover_path: Path | None = None,
    bitrate: str = DEFAULT_AUDIO_BITRATE,
    timeout: float = DEFAULT_CONVERT_TIMEOUT_SECONDS,
) -> Path:
    """Convert `source` to an MP3 with ID3v2 metadata and optional cover art.

    On success, replaces `source` with the resulting `.mp3` (or returns the new
    path if renamed) and cleans up intermediate files.
    On failure, logs a warning, cleans up any temporary output, and returns
    `source` untouched.
    """
    target = source.parent / f"{source.stem}.{uuid.uuid4().hex[:8]}.converting.mp3"
    cmd = build_mp3_convert_command(
        source,
        target,
        title=title,
        artist=artist,
        album=album,
        cover_path=cover_path,
        bitrate=bitrate,
    )
    logger.info("Converting %s to MP3 (title=%r, artist=%r, cover=%s)", source.name, title, artist, bool(cover_path))
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        logger.warning("MP3 conversion failed for %s, using original file", source.name, exc_info=True)
        target.unlink(missing_ok=True)
        return source

    final = source.with_suffix(".mp3")
    try:
        os.replace(target, final)
        if final != source:
            source.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not move %s into place", target, exc_info=True)
        target.unlink(missing_ok=True)
        return source
    return final
