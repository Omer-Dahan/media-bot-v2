"""Large-file splitting for uploads over Telegram's 2GB limit.

Placeholder for M1: real implementation ports the old bot's ffmpeg-based
split logic (engine/base.py: _split_video_if_needed / _upload_split_video),
splitting into ~1.9GB parts and re-labeling captions per part.
"""

from __future__ import annotations

from pathlib import Path

TG_NORMAL_MAX_SIZE = 2000 * 1024 * 1024


def needs_split(file_path: Path, *, limit: int = TG_NORMAL_MAX_SIZE) -> bool:
    return file_path.stat().st_size > limit
