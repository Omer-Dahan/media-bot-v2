"""Unit tests for M12.7's `YtdlpLoggerAdapter` (media_bot_v2/engines/ytdlp_support.py):
routes yt-dlp's own diagnostics - the `ERROR:`/`WARNING:` lines it would
otherwise print straight to stderr under `quiet=True` with no `logger=`
passed - into `logging.getLogger("media_bot_v2.ytdlp")`, where they reach the
per-request log buffer (`logging_setup.RequestLogHandler`) like every other
log line written during a request. Progress spam (`[download] ...`) must
never be promoted above DEBUG, whichever method it arrives through, and
every yt-dlp option dict that drives a download or metadata lookup must
actually carry an adapter instance - see the class docstring for why.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest

from media_bot_v2 import logging_setup
from media_bot_v2.engines.instagram import InstagramEngine
from media_bot_v2.engines.tiktok import TikTokEngine
from media_bot_v2.engines.youtube import YouTubeEngine
from media_bot_v2.engines.ytdlp_support import DownloadGuard, YtdlpLoggerAdapter

LOGGER_NAME = "media_bot_v2.ytdlp"


# ==============================================================================
# 1. Levels
# ==============================================================================


@pytest.mark.parametrize("method", ["error", "warning"])
def test_error_and_warning_land_at_warning(caplog, method):
    adapter = YtdlpLoggerAdapter()
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        getattr(adapter, method)("ERROR: unable to download video data: HTTP Error 403: Forbidden")
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.name == LOGGER_NAME
    assert record.levelno == logging.WARNING


@pytest.mark.parametrize("method", ["debug", "info"])
def test_debug_and_info_land_at_debug(caplog, method):
    adapter = YtdlpLoggerAdapter()
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        getattr(adapter, method)("[youtube] Extracting URL: https://example.com/watch")
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.name == LOGGER_NAME
    assert record.levelno == logging.DEBUG


# ==============================================================================
# 2. Progress filter: yt-dlp's `to_screen` forwards through `warning`/`error`
# too, not just `debug` - a future yt-dlp routing change must not promote
# progress spam to WARNING.
# ==============================================================================


@pytest.mark.parametrize(
    "line",
    [
        "[download]  42.1% of 10.00MiB at 1.00MiB/s ETA 00:09",
        "[download] Destination: /tmp/x.mp4",
    ],
)
@pytest.mark.parametrize("method", ["warning", "error"])
def test_progress_lines_never_reach_warning(caplog, method, line):
    adapter = YtdlpLoggerAdapter()
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        getattr(adapter, method)(line)
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.DEBUG


# ==============================================================================
# 3. Buffer capture: the whole point of the adapter is that this output
# reaches the per-request buffer, not stderr.
# ==============================================================================


@contextmanager
def _ytdlp_logger_at_debug_with_request_handler():
    logger = logging.getLogger(LOGGER_NAME)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    handler = logging_setup.RequestLogHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
        logger.setLevel(previous_level)


def test_adapter_warning_reaches_request_log_buffer():
    adapter = YtdlpLoggerAdapter()
    with _ytdlp_logger_at_debug_with_request_handler():
        with logging_setup.request_log_scope() as buffer:
            adapter.warning("Falling back to generic n function search")
        text = buffer.text()
    assert "Falling back to generic n function search" in text


def test_ytdlp_format_error_line_reaches_buffer_via_adapter():
    adapter = YtdlpLoggerAdapter()
    with _ytdlp_logger_at_debug_with_request_handler():
        with logging_setup.request_log_scope() as buffer:
            adapter.error("ERROR: [youtube] abc123: Video unavailable. This video is private")
        text = buffer.text()
    assert "Video unavailable" in text


# ==============================================================================
# 4. Wiring: every yt-dlp option dict that drives a download or metadata
# lookup must carry the adapter - checked per engine so one engine cannot
# silently lose it without failing its own test.
# ==============================================================================


def _assert_logger_wired(opts: dict) -> None:
    assert isinstance(opts.get("logger"), YtdlpLoggerAdapter)


def test_youtube_download_opts_carry_logger_adapter(tmp_path):
    engine = YouTubeEngine(quality="720", max_download_size=1000)
    opts = engine._build_ydl_opts(tmp_path, loop=None, cancel_token=None)
    _assert_logger_wired(opts)


def test_youtube_info_opts_carry_logger_adapter():
    engine = YouTubeEngine(quality="720", max_download_size=1000)
    opts = engine.info_opts("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    _assert_logger_wired(opts)


def test_instagram_download_opts_carry_logger_adapter(tmp_path):
    engine = InstagramEngine(max_download_size=1000)
    opts = engine._build_ydl_opts(tmp_path, loop=None, cancel_token=None)
    _assert_logger_wired(opts)


def test_tiktok_local_download_opts_carry_logger_adapter(tmp_path):
    engine = TikTokEngine(registry=None, health_tracker=None, max_download_size=1000)
    guard = DownloadGuard(max_size=1000, cancel_token=None)
    opts = engine._build_local_ydl_opts(tmp_path, guard, loop=None)
    _assert_logger_wired(opts)
