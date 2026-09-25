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

    Returns [file_path] unchanged if no split is needed. If a split does
    happen, the source file is removed once its parts exist on disk - the
    old bot did the same (src/engine/base.py) so a 2GB source is never held
    on disk for the full duration of a multi-part upload on top of its parts.
    """
    if not needs_split(file_path, limit=limit):
        return [file_path]
    if _is_video(file_path):
        parts = _split_video(file_path, limit=limit)
    else:
        parts = _split_raw(file_path, limit=limit)
    file_path.unlink(missing_ok=True)
    return parts


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


_VIDEO_SPLIT_MARGIN = 0.7  # target 70% of the byte limit per part, not 100%
_MAX_RESEGMENT_ATTEMPTS = 2  # tighter-margin ffmpeg retries before falling back to raw bytes


def _split_video(file_path: Path, *, limit: int) -> list[Path]:
    """Split via ffmpeg's segment muxer, targeting a duration per part, then
    verify every part actually landed under `limit` and self-correct if not.

    This build's segment muxer has no byte-size option (older ffmpeg
    versions had `-segment_size`; this one doesn't), so the byte limit is
    converted to a target duration from the file's average bitrate
    (duration / total_size * limit). A stream-copy segment can only cut on
    keyframes, and bitrate is rarely perfectly constant, so any single part
    can still land over `limit` despite the safety margin - that part is
    re-segmented with a tighter margin, and if it still doesn't fit after a
    few attempts, it is chunked by raw bytes instead (still correct, just
    not independently playable, matching `_split_raw`'s guarantee).
    """
    parts = _segment_video(file_path, limit=limit, margin=_VIDEO_SPLIT_MARGIN)
    return _ensure_parts_within_limit(
        parts, limit=limit, margin=_VIDEO_SPLIT_MARGIN, attempts_left=_MAX_RESEGMENT_ATTEMPTS
    )


def _segment_video(file_path: Path, *, limit: int, margin: float) -> list[Path]:
    stem = file_path.stem
    ext = file_path.suffix or ".mp4"
    segment_template = str(file_path.parent / f"{stem}.part%03d{ext}")

    duration = _probe_duration_seconds(file_path)
    total_size = file_path.stat().st_size
    segment_time = max(duration * limit / total_size * margin, 1.0)

    # Every part is its own MP4: put its moov up front so each one streams.
    faststart = (
        ["-segment_format_options", "movflags=+faststart"]
        if ext.lower() in {".mp4", ".m4v", ".mov"}
        else []
    )
    try:
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
                *faststart,
                segment_template,
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        # ffmpeg can crash after writing one or more segments - leaving those
        # behind would both waste disk and confuse the next glob() call.
        for leftover in file_path.parent.glob(f"{stem}.part*{ext}"):
            leftover.unlink(missing_ok=True)
        raise

    parts = sorted(file_path.parent.glob(f"{stem}.part*{ext}"))
    if not parts:
        raise RuntimeError(f"ffmpeg produced no parts for {file_path}")
    return parts


def _ensure_parts_within_limit(
    parts: list[Path], *, limit: int, margin: float, attempts_left: int
) -> list[Path]:
    fixed: list[Path] = []
    for part in parts:
        size = part.stat().st_size
        if size <= limit:
            fixed.append(part)
            continue
        if attempts_left > 0:
            tighter_margin = margin / 2
            resegmented = _segment_video(part, limit=limit, margin=tighter_margin)
            part.unlink()
            fixed.extend(
                _ensure_parts_within_limit(
                    resegmented, limit=limit, margin=tighter_margin, attempts_left=attempts_left - 1
                )
            )
        else:
            fixed.extend(_split_raw(part, limit=limit))
            part.unlink()
    return fixed


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
