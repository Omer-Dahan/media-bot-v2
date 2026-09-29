"""M11.2: TikTok's local yt-dlp fallback wires the shared progress formatter
into its download hook (finding #4 of the M11.1 audit) - the hook must still
enforce `guard.check`'s size ceiling exactly as before; it must not be
replaced by progress reporting, only accompanied by it."""

import asyncio

import pytest

from media_bot_v2.engines.tiktok import TikTokEngine
from media_bot_v2.engines.ytdlp_support import DownloadGuard, DownloadTooLargeSignal


class _FakeProgress:
    def __init__(self) -> None:
        self.updates: list[str] = []

    async def update(self, text: str, *, is_terminal: bool | None = None) -> None:
        self.updates.append(text)


async def test_tiktok_local_hook_forwards_progress_and_still_enforces_guard():
    loop = asyncio.get_running_loop()
    progress = _FakeProgress()
    engine = TikTokEngine(
        registry=None,
        health_tracker=None,
        max_download_size=100,
        progress=progress,
    )
    guard = DownloadGuard(max_size=100, cancel_token=None)
    hook = engine._make_progress_hook(loop, guard)

    hook({"status": "downloading", "downloaded_bytes": 45, "total_bytes": 100, "speed": 1024, "eta": 30})
    for _ in range(5):  # let run_coroutine_threadsafe's callback run on this same loop
        await asyncio.sleep(0)

    assert progress.updates, "hook did not forward progress to the reporter"
    text = progress.updates[-1]
    assert "45%" in text
    assert "█" in text
    assert "מהירות" in text

    # the same hook call must still raise once bytes exceed the size ceiling -
    # progress reporting must never come at the cost of the guard
    with pytest.raises(DownloadTooLargeSignal):
        hook({"status": "downloading", "downloaded_bytes": 200, "total_bytes": 200})


async def test_tiktok_local_hook_enforces_guard_even_without_a_progress_reporter():
    """The `noprogress`/no-reporter case (no MessageProgressReporter attached)
    must not accidentally disable the size guard either."""
    guard = DownloadGuard(max_size=100, cancel_token=None)
    engine = TikTokEngine(registry=None, health_tracker=None, max_download_size=100, progress=None)
    loop = asyncio.get_running_loop()
    hook = engine._make_progress_hook(loop, guard)

    with pytest.raises(DownloadTooLargeSignal):
        hook({"status": "downloading", "downloaded_bytes": 200, "total_bytes": 200})
