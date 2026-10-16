"""Unit tests for M12.5a: `DownloadGuard.check` rejects on a size estimate
alone only when it is GROSSLY over the cap (more than `_GROSS_ESTIMATE_FACTOR`
times it), once a small floor of real traffic backs it up. A merely-over-cap
estimate (production, 11.10: a real video was refused as "4.7GB" against a
4GiB cap from a split HLS/DASH format's estimate, extrapolated from the first
fragments, on a file that likely would have fit) must be let through: the
download proceeds, and `downloaded_bytes` crossing the cap still stops a
genuinely oversize file. Trade-off accepted on purpose: at most one cap's
worth of real traffic can be spent on a file whose estimate was wrong.

An exact `total_bytes` or real `downloaded_bytes` past the cap must still
reject immediately - that part has to keep working, or a real oversize
download is only caught after it has already consumed the bandwidth it was
supposed to be stopped from consuming.
"""

from __future__ import annotations

import pytest

from media_bot_v2.engines.ytdlp_support import DownloadGuard, DownloadTooLargeSignal
from media_bot_v2.telegram import texts

MAX_SIZE = 4 * 1024 * 1024 * 1024  # 4GiB, matches config.py's default


@pytest.mark.parametrize("received_bytes", [1 * 1024 * 1024, 50 * 1024 * 1024, 500 * 1024 * 1024])
def test_borderline_over_cap_estimate_never_rejects_at_any_real_progress(received_bytes):
    """A 4.7GB estimate against a 4GiB cap (the production value) is inside
    the gross band, not past it - it must not reject on its own, whatever
    real progress backs it up. This is the fix: such a file now downloads."""
    guard = DownloadGuard(max_size=MAX_SIZE, cancel_token=None)
    guard.check(
        {
            "status": "downloading",
            "total_bytes": None,
            "total_bytes_estimate": int(4.7 * 1024 * 1024 * 1024),
            "downloaded_bytes": received_bytes,
        }
    )
    assert guard.oversize == []


def test_grossly_inflated_estimate_rejects_after_minimal_real_progress():
    """A 10GB estimate against a 4GiB cap clears the gross band (> 2x) and
    rejects once a small floor of real traffic backs it up."""
    guard = DownloadGuard(max_size=MAX_SIZE, cancel_token=None)
    with pytest.raises(DownloadTooLargeSignal):
        guard.check(
            {
                "status": "downloading",
                "total_bytes": None,
                "total_bytes_estimate": 10 * 1024 * 1024 * 1024,
                "downloaded_bytes": 3 * 1024 * 1024,  # a few MB, past the ~1MiB floor
            }
        )
    assert len(guard.oversize) == 1


def test_grossly_inflated_estimate_with_no_real_progress_does_not_reject():
    """Even a grossly-over estimate must not reject before the minimal
    real-progress floor is cleared - a first-fragment spike alone is not
    enough evidence."""
    guard = DownloadGuard(max_size=MAX_SIZE, cancel_token=None)
    guard.check(
        {
            "status": "downloading",
            "total_bytes": None,
            "total_bytes_estimate": 10 * 1024 * 1024 * 1024,
            "downloaded_bytes": 0,
        }
    )
    assert guard.oversize == []


def test_exact_total_bytes_over_the_limit_raises_immediately():
    """A real Content-Length over the cap is trusted with zero real progress -
    this must keep working exactly as before."""
    guard = DownloadGuard(max_size=MAX_SIZE, cancel_token=None)
    with pytest.raises(DownloadTooLargeSignal):
        guard.check(
            {
                "status": "downloading",
                "total_bytes": 5 * 1024 * 1024 * 1024,
                "total_bytes_estimate": None,
                "downloaded_bytes": 0,
            }
        )
    assert len(guard.oversize) == 1


def test_downloaded_bytes_over_the_limit_raises_with_no_estimate_at_all():
    """Real traffic actually passing the cap must still stop the download,
    even with no estimate at all to back it up."""
    guard = DownloadGuard(max_size=MAX_SIZE, cancel_token=None)
    with pytest.raises(DownloadTooLargeSignal):
        guard.check(
            {
                "status": "downloading",
                "total_bytes": None,
                "total_bytes_estimate": None,
                "downloaded_bytes": MAX_SIZE + 1,
            }
        )
    assert len(guard.oversize) == 1


def test_exactly_at_the_cap_does_not_raise():
    guard = DownloadGuard(max_size=MAX_SIZE, cancel_token=None)
    guard.check(
        {
            "status": "downloading",
            "total_bytes": MAX_SIZE,
            "total_bytes_estimate": None,
            "downloaded_bytes": MAX_SIZE,
        }
    )
    assert guard.oversize == []


def test_one_byte_over_the_cap_raises():
    guard = DownloadGuard(max_size=MAX_SIZE, cancel_token=None)
    with pytest.raises(DownloadTooLargeSignal):
        guard.check(
            {
                "status": "downloading",
                "total_bytes": MAX_SIZE + 1,
                "total_bytes_estimate": None,
                "downloaded_bytes": 0,
            }
        )
    assert len(guard.oversize) == 1


def test_estimate_under_the_limit_never_raises_however_much_progress():
    guard = DownloadGuard(max_size=MAX_SIZE, cancel_token=None)
    guard.check(
        {
            "status": "downloading",
            "total_bytes": None,
            "total_bytes_estimate": MAX_SIZE - 1,
            "downloaded_bytes": MAX_SIZE - 1,
        }
    )
    assert guard.oversize == []


def test_non_downloading_status_is_ignored():
    guard = DownloadGuard(max_size=MAX_SIZE, cancel_token=None)
    guard.check({"status": "finished", "total_bytes": MAX_SIZE * 10})
    assert guard.oversize == []


def test_message_distinguishes_size_from_cap_at_the_boundary():
    """`format_download_too_large` must not print the identical rounded
    string for both numbers when they differ by one byte at the cap
    boundary (both would otherwise read "4.0GB")."""
    assert texts.human_size(MAX_SIZE + 1) == texts.human_size(MAX_SIZE)
    msg = texts.format_download_too_large(MAX_SIZE + 1, MAX_SIZE)
    assert f"{MAX_SIZE + 1:,} bytes" in msg
    assert f"{MAX_SIZE:,} bytes" in msg
