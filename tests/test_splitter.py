"""Splitter: ffmpeg stream-copy segmenting for video, raw byte chunks for
everything else. Uses a real (tiny) ffmpeg-generated video, no network."""

import os
import subprocess
from pathlib import Path

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


def test_split_raw_deletes_source_file_after_successful_split(tmp_path):
    f = tmp_path / "archive.zip"
    f.write_bytes(os.urandom(3000))

    splitter.split_file(f, limit=1000)

    assert not f.exists()  # covers finding 6: source freed once parts exist


def test_split_file_keeps_source_when_under_limit(tmp_path):
    f = tmp_path / "small.bin"
    f.write_bytes(b"x" * 100)

    splitter.split_file(f, limit=1000)

    assert f.exists()  # no split happened - nothing to free the source for


@pytest.fixture
def tiny_video(tmp_path):
    video_path = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            # 6s (not 2s): long enough that per-segment container overhead
            # (moov/ftyp headers) is small relative to each part's video
            # data, so segment_time isn't dominated by the 1-second floor
            # in _segment_video and a generous limit doesn't need rescuing.
            "-i",
            "testsrc=duration=6:size=64x64:rate=10",
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


def test_split_video_produces_multiple_playable_parts_all_within_limit(tiny_video):
    full_size = tiny_video.stat().st_size
    assert full_size > 0
    limit = max(full_size // 2, 1024)

    parts = splitter.split_file(tiny_video, limit=limit)

    assert len(parts) > 1
    assert not tiny_video.exists()  # source freed after a successful split (finding 6)
    for part in parts:
        assert part.exists()
        assert 0 < part.stat().st_size <= limit  # covers finding 3: every part fits the limit
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", str(part)],
            check=True,
            capture_output=True,
            text=True,
        )
        assert probe.returncode == 0


def test_split_video_resegments_a_part_that_still_exceeds_the_limit(tmp_path, monkeypatch):
    """Covers finding 3: if ffmpeg's first pass still leaves an oversized
    part (bitrate spike, coarse keyframe spacing), the splitter must correct
    itself rather than hand an over-limit part to the uploader."""
    limit = 1000
    call_count = 0

    def fake_segment_video(file_path, *, limit, margin):
        nonlocal call_count
        call_count += 1
        stem = file_path.stem
        parent = file_path.parent
        if call_count == 1:
            # First pass: part000 is oversized, part001 is fine.
            p0 = parent / f"{stem}.part000.mp4"
            p0.write_bytes(b"x" * (limit + 500))
            p1 = parent / f"{stem}.part001.mp4"
            p1.write_bytes(b"y" * 100)
            return [p0, p1]
        # Any resegmentation attempt now produces something that fits.
        fixed = parent / f"{stem}.part000-fixed.mp4"
        fixed.write_bytes(b"z" * 400)
        return [fixed]

    monkeypatch.setattr(splitter, "_segment_video", fake_segment_video)

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"source-bytes")

    parts = splitter._split_video(video, limit=limit)

    assert all(part.stat().st_size <= limit for part in parts)
    assert call_count == 2  # one resegment attempt was enough to fix it


def test_split_video_falls_back_to_raw_bytes_when_resegmenting_never_fits(tmp_path, monkeypatch):
    """Covers finding 3's other allowed fix: when even repeated ffmpeg
    resegmentation can't shrink a part under the limit, fall back to a raw
    byte split for just that part instead of shipping an oversized part."""
    limit = 1000

    def always_oversized_segment_video(file_path, *, limit, margin):
        stem = file_path.stem
        oversized = file_path.parent / f"{stem}.part000.mp4"
        oversized.write_bytes(b"x" * (limit + 500))
        return [oversized]

    monkeypatch.setattr(splitter, "_segment_video", always_oversized_segment_video)

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"source-bytes")

    parts = splitter._split_video(video, limit=limit)

    assert len(parts) >= 2  # the oversized part was chunked into raw pieces
    assert all(part.stat().st_size <= limit for part in parts)
    reassembled_size = sum(part.stat().st_size for part in parts)
    assert reassembled_size == limit + 500


def test_split_video_cleans_up_partial_parts_on_ffmpeg_failure(tmp_path, monkeypatch):
    """Covers finding 6: if ffmpeg crashes mid-split, any segments it
    already wrote must not be left behind as disk garbage."""
    monkeypatch.setattr(splitter, "_probe_duration_seconds", lambda path: 10.0)

    def fake_run(cmd, **kwargs):
        # Simulate ffmpeg writing one segment before crashing.
        segment_template = cmd[-1]
        partial = Path(segment_template.replace("%03d", "000"))
        partial.write_bytes(b"partial-segment")
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(splitter.subprocess, "run", fake_run)

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x" * 5000)

    with pytest.raises(subprocess.CalledProcessError):
        splitter._segment_video(video, limit=1000, margin=0.7)

    assert not any(tmp_path.glob("clip.part*"))
