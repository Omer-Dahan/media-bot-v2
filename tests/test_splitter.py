"""Splitter: ffmpeg stream-copy segmenting for video, raw byte chunks for
everything else. Uses a real (tiny) ffmpeg-generated video, no network."""

import os
import subprocess

import pytest

from media_bot_v2.upload import splitter


def test_needs_split_false_under_limit(tmp_path):
    f = tmp_path / "small.bin"
    f.write_bytes(b"x" * 100)
    assert splitter.needs_split(f, limit=1000) is False


def test_needs_split_true_over_limit(tmp_path):
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 2000)
    assert splitter.needs_split(f, limit=1000) is True


def test_split_file_returns_original_when_under_limit(tmp_path):
    f = tmp_path / "small.bin"
    f.write_bytes(b"x" * 100)
    assert splitter.split_file(f, limit=1000) == [f]


def test_split_raw_binary_into_ordered_chunks_that_reassemble(tmp_path):
    original = os.urandom(5000)
    f = tmp_path / "archive.zip"
    f.write_bytes(original)

    parts = splitter.split_file(f, limit=2000)

    assert len(parts) == 3
    reassembled = b"".join(p.read_bytes() for p in parts)
    assert reassembled == original


@pytest.fixture
def tiny_video(tmp_path):
    video_path = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=64x64:rate=10",
            "-pix_fmt",
            "yuv420p",
            # Frequent keyframes (every 5 frames = 0.5s) so the segment
            # muxer, which can only cut on keyframes with stream copy, has
            # somewhere to cut in this short clip.
            "-g",
            "5",
            "-keyint_min",
            "5",
            str(video_path),
        ],
        check=True,
        capture_output=True,
    )
    return video_path


def test_split_video_produces_multiple_playable_parts(tiny_video):
    full_size = tiny_video.stat().st_size
    assert full_size > 0

    parts = splitter.split_file(tiny_video, limit=max(full_size // 4, 1024))

    assert len(parts) > 1
    for part in parts:
        assert part.exists()
        assert part.stat().st_size > 0
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", str(part)],
            check=True,
            capture_output=True,
            text=True,
        )
        assert probe.returncode == 0
