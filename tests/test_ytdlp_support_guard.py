"""Unit tests for M12.5: `DownloadGuard.check` must not reject a download on
an inflated `total_bytes_estimate` alone (production, 11.10: a real video was
refused as "4.7GB" from a split HLS/DASH format's estimate, extrapolated from
the first fragments, before a single byte of real evidence backed it up).

An exact `total_bytes` or real `downloaded_bytes` past the cap must still
reject immediately - that part has to keep working, or a real oversize
download is only caught after it has already consumed the bandwidth it was
supposed to be stopped from consuming.
"""

from __future__ import annotations

import pytest

from media_bot_v2.engines.ytdlp_support import DownloadGuard, DownloadTooLargeSignal

MAX_SIZE = 4 * 1024 * 1024 * 1024  # 4GiB, matches config.py's default


def test_inflated_estimate_with_tiny_real_progress_does_not_raise():
    """`total_bytes_estimate` alone, with total_bytes unknown and only a
    trickle of real bytes received, must not trip the guard."""
    guard = DownloadGuard(max_size=MAX_SIZE, cancel_token=None)
    guard.check(
        {
            "status": "downloading",
            "total_bytes": None,
            "total_bytes_estimate": 4.7 * 1024 * 1024 * 1024,  # the production value
            "downloaded_bytes": 1 * 1024 * 1024,  # 1MB - far under the grace floor
        }
    )
    assert guard.oversize == []


def test_inflated_estimate_after_real_progress_past_the_threshold_raises():
    """The same oversize estimate, once real progress clears the grace
    threshold, is trusted and may raise."""
    guard = DownloadGuard(max_size=MAX_SIZE, cancel_token=None)
    with pytest.raises(DownloadTooLargeSignal):
        guard.check(
            {
                "status": "downloading",
                "total_bytes": None,
                "total_bytes_estimate": 4.7 * 1024 * 1024 * 1024,
                "downloaded_bytes": 60 * 1024 * 1024,  # past the 50MB grace floor
            }
        )
    assert len(guard.oversize) == 1


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


def test_downloaded_bytes_over_the_limit_raises():
    """Real traffic actually passing the cap must still stop the download
    early, regardless of what total_bytes/estimate said beforehand."""
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
