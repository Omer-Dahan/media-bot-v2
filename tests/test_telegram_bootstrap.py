"""Telethon client construction is mocked - never connects to Telegram."""

from unittest.mock import patch

from media_bot_v2.config import Settings
from media_bot_v2.telegram.client import build_client


def test_build_client_constructs_telethon_client_without_connecting():
    settings = Settings(app_id=1, app_hash="h", bot_token="t", _env_file=None)
    with patch("media_bot_v2.telegram.client.TelegramClient") as mock_client_cls:
        build_client(settings, session_name="test-session")
        mock_client_cls.assert_called_once_with("test-session", 1, "h")
