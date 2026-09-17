"""Large-file splitting for uploads over Telegram's 2GB limit.

Videos are split with ffmpeg's segment muxer using stream copy (no
re-encode), matching the old bot's ffmpeg-based >2GB splitter
(src/engine/base.py:700-890) so each part stays independently playable.
Non-video files (a direct link can point at anything - archives, PDFs,
disk images) are split as raw byte chunks instead: ffmpeg cannot segment an
arbitrary binary, and Telegram just needs ordered document parts, not
standalone playable files, for those.

This is a different mechanism from the old bot's ZIP splitter
(archive_manager.py), which only served torrent/JDownloader and is not
carried forward (spec/INVENTORY.md section 4).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import filetype

TG_NORMAL_MAX_SIZE = 2000 * 1024 * 1024


def needs_split(file_path: Path, *, limit: int = TG_NORMAL_MAX_SIZE) -> bool:
    return file_path.stat().st_size > limit


def split_file(file_path: Path, *, limit: int = TG_NORMAL_MAX_SIZE) -> list[Path]:
    """Split file_path into parts no larger than `limit` bytes each.

    Returns [file_path] unchanged if no split is needed.
    """
    if not needs_split(file_path, limit=limit):
        return [file_path]
    if _is_video(file_path):
        return _split_video(file_path, limit=limit)
    return _split_raw(file_path, limit=limit)


def _is_video(file_path: Path) -> bool:
    kind = filetype.guess(str(file_path))
    return kind is not None and kind.mime.startswith("video/")


def _probe_duration_seconds(file_path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(file_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def _split_video(file_path: Path, *, limit: int) -> list[Path]:
    """Split via ffmpeg's segment muxer, targeting a duration per part.

    This build's segment muxer has no byte-size option (older ffmpeg
    versions had `-segment_size`; this one doesn't), so the byte limit is
    converted to a target duration from the file's average bitrate
    (duration / total_size * limit), with a 5% safety margin for normal VBR
    variance. This assumes roughly constant bitrate across the file, true
    for the vast majority of single-source video; a file with a large
    bitrate spike partway through could still produce one oversized part.
    """
    stem = file_path.stem
    ext = file_path.suffix or ".mp4"
    segment_template = str(file_path.parent / f"{stem}.part%03d{ext}")

    duration = _probe_duration_seconds(file_path)
    total_size = file_path.stat().st_size
    segment_time = max(duration * limit / total_size * 0.95, 1.0)

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(file_path),
            "-c",
            "copy",
            "-map",
            "0",
            "-f",
            "segment",
            "-segment_time",
            str(segment_time),
            "-reset_timestamps",
            "1",
            segment_template,
        ],
        check=True,
        capture_output=True,
    )

    parts = sorted(file_path.parent.glob(f"{stem}.part*{ext}"))
    if not parts:
        raise RuntimeError(f"ffmpeg produced no parts for {file_path}")
    return parts


def _split_raw(file_path: Path, *, limit: int) -> list[Path]:
    parts: list[Path] = []
    with open(file_path, "rb") as src:
        index = 0
        while True:
            chunk = src.read(limit)
            if not chunk:
                break
            part_path = file_path.with_name(f"{file_path.name}.part{index:03d}")
            with open(part_path, "wb") as dst:
                dst.write(chunk)
            parts.append(part_path)
            index += 1
    return parts
