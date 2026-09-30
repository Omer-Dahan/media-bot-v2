"""Pytest configuration and test isolation.

Sets safe env vars before any project module is imported so tests never
touch the real .env, a real Telegram session, or the production database.
"""

import os

os.environ.setdefault("DB_DSN", "sqlite:///:memory:")
os.environ.setdefault("APP_ID", "12345")
os.environ.setdefault("APP_HASH", "mock_app_hash")
os.environ.setdefault("BOT_TOKEN", "123456:mock_bot_token")
os.environ.setdefault("OWNER", "123456789")


import pytest


@pytest.fixture
def bypass_ssrf_guard(monkeypatch):
    """Disables the SSRF guard's address check (media_bot_v2/engines/ssrf_guard.py)
    for tests that deliberately talk to a local test-only HTTP server
    (127.0.0.1) to exercise download mechanics unrelated to SSRF - loopback
    is exactly what the guard blocks by design. The guard itself is covered
    directly, without this fixture, by test_ssrf_guard.py and the dedicated
    SSRF tests in test_direct_engine.py / test_downloader.py."""
    from media_bot_v2.engines import ssrf_guard

    monkeypatch.setattr(ssrf_guard, "assert_safe_url", lambda url: None)


@pytest.fixture(autouse=True)
def _no_real_youtube_lookup(monkeypatch):
    """The quality menu looks up the real title/duration through yt-dlp.
    Tests must never hit the network, so by default the lookup finds nothing
    (the menu falls back to its placeholders); tests that care about the
    lookup patch it themselves."""

    async def _nothing(url, *, opts, timeout):
        return None, None

    monkeypatch.setattr("media_bot_v2.telegram.router.fetch_title_duration", _nothing)
