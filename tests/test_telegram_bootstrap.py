"""Telethon client construction is mocked - never connects to Telegram."""

from unittest.mock import patch

from media_bot_v2.config import Settings
from media_bot_v2.telegram.client import build_client


def test_build_client_constructs_telethon_client_without_connecting():
    settings = Settings(app_id=1, app_hash="h", bot_token="t", _env_file=None)
    with patch("media_bot_v2.telegram.client.TelegramClient") as mock_client_cls:
        build_client(settings, session_name="test-session")
        mock_client_cls.assert_called_once_with("test-session", 1, "h")


def test_main_calls_check_js_runtime():
    from media_bot_v2.bootstrap import main

    with (
        patch("media_bot_v2.bootstrap.load_settings") as mock_settings,
        patch("media_bot_v2.bootstrap.configure_logging"),
        patch("media_bot_v2.bootstrap.check_js_runtime") as mock_check_js,
        patch("media_bot_v2.bootstrap.build_session_factory"),
        patch("media_bot_v2.bootstrap.build_client") as mock_build_client,
        patch("media_bot_v2.bootstrap.register_handlers"),
    ):
        mock_settings.return_value = Settings(app_id=1, app_hash="h", bot_token="t", _env_file=None)
        mock_client = mock_build_client.return_value
        main()
        mock_check_js.assert_called_once()
        mock_client.start.assert_called_once_with(bot_token="t")
        mock_client.run_until_disconnected.assert_called_once()
