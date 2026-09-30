"""Telethon client construction is mocked - never connects to Telegram."""

from unittest.mock import patch

from media_bot_v2.config import Settings
from media_bot_v2.telegram.client import build_client


def test_build_client_constructs_telethon_client_without_connecting():
    settings = Settings(app_id=1, app_hash="h", bot_token="t", _env_file=None)
    with patch("media_bot_v2.telegram.client.TelegramClient") as mock_client_cls:
        build_client(settings, session_name="test-session")
        mock_client_cls.assert_called_once_with("test-session", 1, "h", flood_sleep_threshold=0)


def test_build_client_respects_custom_flood_sleep_threshold():
    settings = Settings(app_id=1, app_hash="h", bot_token="t", flood_sleep_threshold=60, _env_file=None)
    with patch("media_bot_v2.telegram.client.TelegramClient") as mock_client_cls:
        build_client(settings, session_name="test-session")
        mock_client_cls.assert_called_once_with("test-session", 1, "h", flood_sleep_threshold=60)



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


def test_main_initializes_and_shuts_down_the_dedicated_thread_pool():
    """M11.11 finding A: main() must create the dedicated executor (sized by
    THREAD_POOL_SIZE) before any pipeline work can run, and shut it down
    after the client disconnects - never leaking the pool across restarts
    or leaving pipeline work on Python's default executor."""
    from media_bot_v2 import executor as executor_module
    from media_bot_v2.bootstrap import main

    with (
        patch("media_bot_v2.bootstrap.load_settings") as mock_settings,
        patch("media_bot_v2.bootstrap.configure_logging"),
        patch("media_bot_v2.bootstrap.check_js_runtime"),
        patch("media_bot_v2.bootstrap.build_session_factory"),
        patch("media_bot_v2.bootstrap.build_client"),
        patch("media_bot_v2.bootstrap.register_handlers"),
        patch("media_bot_v2.bootstrap.shutdown_thread_pool") as mock_shutdown,
    ):
        mock_settings.return_value = Settings(
            app_id=1, app_hash="h", bot_token="t", thread_pool_size=7, _env_file=None
        )
        main()
        assert executor_module._executor is not None
        assert executor_module._executor._max_workers == 7
        mock_shutdown.assert_called_once()
    executor_module._executor.shutdown(wait=False)
    executor_module._executor = None


def test_main_initializes_the_upload_concurrency_semaphore():
    """M11.11 finding A.4: the account-health ceiling on concurrent uploads
    must be sized from UPLOAD_CONCURRENCY_LIMIT at startup."""
    from media_bot_v2.bootstrap import main
    from media_bot_v2.telegram import parallel_upload

    with (
        patch("media_bot_v2.bootstrap.load_settings") as mock_settings,
        patch("media_bot_v2.bootstrap.configure_logging"),
        patch("media_bot_v2.bootstrap.check_js_runtime"),
        patch("media_bot_v2.bootstrap.build_session_factory"),
        patch("media_bot_v2.bootstrap.build_client"),
        patch("media_bot_v2.bootstrap.register_handlers"),
        patch("media_bot_v2.bootstrap.shutdown_thread_pool"),
    ):
        mock_settings.return_value = Settings(
            app_id=1, app_hash="h", bot_token="t", upload_concurrency_limit=9, _env_file=None
        )
        main()
        assert parallel_upload._upload_semaphore is not None
        assert parallel_upload._upload_semaphore._value == 9
    parallel_upload._upload_semaphore = None


def test_log_archive_channel_warns_when_unset(caplog):
    from media_bot_v2.bootstrap import log_archive_channel

    with caplog.at_level("WARNING", logger="media_bot_v2.bootstrap"):
        log_archive_channel(None)
    assert any("ARCHIVE_CHANNEL is not configured" in r.message for r in caplog.records)


def test_log_archive_channel_logs_numeric_id(caplog):
    from media_bot_v2.bootstrap import log_archive_channel

    with caplog.at_level("INFO", logger="media_bot_v2.bootstrap"):
        log_archive_channel(-1003534083142)
    assert any(
        "-1003534083142" in r.message and "numeric ID" in r.message for r in caplog.records
    )


def test_log_archive_channel_logs_username(caplog):
    from media_bot_v2.bootstrap import log_archive_channel

    with caplog.at_level("INFO", logger="media_bot_v2.bootstrap"):
        log_archive_channel("@my_archive")
    assert any("@my_archive" in r.message and "username" in r.message for r in caplog.records)


def test_main_logs_archive_channel_at_startup():
    from unittest.mock import patch

    from media_bot_v2.bootstrap import main

    with (
        patch("media_bot_v2.bootstrap.load_settings") as mock_settings,
        patch("media_bot_v2.bootstrap.configure_logging"),
        patch("media_bot_v2.bootstrap.check_js_runtime"),
        patch("media_bot_v2.bootstrap.log_archive_channel") as mock_log_archive,
        patch("media_bot_v2.bootstrap.build_session_factory"),
        patch("media_bot_v2.bootstrap.build_client"),
        patch("media_bot_v2.bootstrap.register_handlers"),
    ):
        mock_settings.return_value = Settings(
            app_id=1, app_hash="h", bot_token="t", archive_channel=-1003534083142, _env_file=None
        )
        main()
        mock_log_archive.assert_called_once_with(-1003534083142)
