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
