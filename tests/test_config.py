"""Config loading from environment variables, matching the old bot's names."""

import pytest
from pydantic import ValidationError

from media_bot_v2.config import Settings


def test_loads_required_telegram_fields(monkeypatch):
    monkeypatch.setenv("APP_ID", "555")
    monkeypatch.setenv("APP_HASH", "abc123")
    monkeypatch.setenv("BOT_TOKEN", "555:token")
    settings = Settings(_env_file=None)
    assert settings.app_id == 555
    assert settings.app_hash == "abc123"
    assert settings.bot_token == "555:token"


def test_owner_ids_parses_comma_separated_list(monkeypatch):
    monkeypatch.setenv("APP_ID", "1")
    monkeypatch.setenv("APP_HASH", "h")
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("OWNER", "111, 222,333")
    settings = Settings(_env_file=None)
    assert settings.owner_ids == [111, 222, 333]


def test_owner_ids_empty_when_unset(monkeypatch):
    monkeypatch.setenv("APP_ID", "1")
    monkeypatch.setenv("APP_HASH", "h")
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.delenv("OWNER", raising=False)
    settings = Settings(_env_file=None)
    assert settings.owner_ids == []


def test_session_name_defaults_to_v2_not_the_old_bots_main(monkeypatch):
    monkeypatch.setenv("APP_ID", "1")
    monkeypatch.setenv("APP_HASH", "h")
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.delenv("SESSION_NAME", raising=False)
    settings = Settings(_env_file=None)
    assert settings.session_name == "v2"
    assert settings.session_name != "main"


def test_session_name_main_is_rejected(monkeypatch):
    """Covers finding 8: copying an old .env with SESSION_NAME=main would
    otherwise make this bot open the old bot's live production session."""
    monkeypatch.setenv("APP_ID", "1")
    monkeypatch.setenv("APP_HASH", "h")
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("SESSION_NAME", "main")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_db_dsn_defaults_to_local_sqlite(monkeypatch):
    monkeypatch.setenv("APP_ID", "1")
    monkeypatch.setenv("APP_HASH", "h")
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.delenv("DB_DSN", raising=False)
    settings = Settings(_env_file=None)
    assert settings.db_dsn == "sqlite:///database.sqlite3"


def test_youtube_and_concurrency_defaults(monkeypatch):
    monkeypatch.setenv("APP_ID", "1")
    monkeypatch.setenv("APP_HASH", "h")
    monkeypatch.setenv("BOT_TOKEN", "t")
    settings = Settings(_env_file=None)
    assert settings.force_ipv4 is False
    assert settings.potoken is None
    assert settings.potoken_provider_url is None
    assert settings.youtube_cookies_file is None
    assert settings.youtube_player_client is None
    assert settings.youtube_js_runtimes is None
    assert settings.youtube_remote_components is None
    assert settings.workers == 100
    assert settings.user_workers == 5  # M11.11: 2 -> 5, see config.py's comment
    assert settings.thread_pool_size == 48
    assert settings.upload_concurrency_limit == 20


def test_youtube_and_concurrency_custom_values(monkeypatch):
    monkeypatch.setenv("APP_ID", "1")
    monkeypatch.setenv("APP_HASH", "h")
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("FORCE_IPV4", "true")
    monkeypatch.setenv("POTOKEN", "po_token_value")
    monkeypatch.setenv("POTOKEN_PROVIDER_URL", "http://127.0.0.1:4416")
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", "/path/to/cookies.txt")
    monkeypatch.setenv("YOUTUBE_PLAYER_CLIENT", "android,web")
    monkeypatch.setenv("YOUTUBE_JS_RUNTIMES", "node")
    monkeypatch.setenv("YOUTUBE_REMOTE_COMPONENTS", "ejs:github")
    monkeypatch.setenv("WORKERS", "50")
    monkeypatch.setenv("USER_WORKERS", "4")
    monkeypatch.setenv("THREAD_POOL_SIZE", "24")
    monkeypatch.setenv("UPLOAD_CONCURRENCY_LIMIT", "10")
    settings = Settings(_env_file=None)
    assert settings.force_ipv4 is True
    assert settings.potoken == "po_token_value"
    assert settings.potoken_provider_url == "http://127.0.0.1:4416"
    assert settings.youtube_cookies_file == "/path/to/cookies.txt"
    assert settings.youtube_player_client == "android,web"
    assert settings.youtube_js_runtimes == "node"
    assert settings.youtube_remote_components == "ejs:github"
    assert settings.workers == 50
    assert settings.user_workers == 4
    assert settings.thread_pool_size == 24
    assert settings.upload_concurrency_limit == 10


def test_youtube_settings_alias_choices(monkeypatch):
    monkeypatch.setenv("APP_ID", "1")
    monkeypatch.setenv("APP_HASH", "h")
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("PLAYER_CLIENT", "mweb")
    monkeypatch.setenv("JS_RUNTIMES", "deno,node")
    monkeypatch.setenv("REMOTE_COMPONENTS", "ejs:npm")
    settings = Settings(_env_file=None)
    assert settings.youtube_player_client == "mweb"
    assert settings.youtube_js_runtimes == "deno,node"
    assert settings.youtube_remote_components == "ejs:npm"


def test_provider_settings_defaults(monkeypatch):
    monkeypatch.setenv("APP_ID", "1")
    monkeypatch.setenv("APP_HASH", "h")
    monkeypatch.setenv("BOT_TOKEN", "t")
    settings = Settings(_env_file=None)
    assert settings.tiktok_providers == "tikwm,tikdownloader,musicaldown,cobalt"
    assert settings.youtube_providers == "ytmp3,cobalt"
    assert settings.disabled_providers == ""
    assert settings.cobalt_url is None
    assert settings.ytmp3_api_key is None
    assert settings.provider_timeout == 15.0
    assert settings.provider_failure_threshold == 3
    assert settings.provider_cooldown_seconds == 300
    assert settings.parsed_tiktok_providers == ["tikwm", "tikdownloader", "musicaldown", "cobalt"]
    assert settings.parsed_youtube_providers == ["ytmp3", "cobalt"]
    assert settings.parsed_disabled_providers == set()


def test_provider_settings_custom_values(monkeypatch):
    monkeypatch.setenv("APP_ID", "1")
    monkeypatch.setenv("APP_HASH", "h")
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("TIKTOK_PROVIDERS", "musicaldown, tikwm")
    monkeypatch.setenv("YOUTUBE_PROVIDERS", "cobalt, ytmp3")
    monkeypatch.setenv("DISABLED_PROVIDERS", "cobalt, tikdownloader")
    monkeypatch.setenv("COBALT_URL", "https://cobalt.example.com")
    monkeypatch.setenv("YTMP3_API_KEY", "custom_key")
    monkeypatch.setenv("PROVIDER_TIMEOUT", "25.5")
    monkeypatch.setenv("PROVIDER_FAILURE_THRESHOLD", "5")
    monkeypatch.setenv("PROVIDER_COOLDOWN_SECONDS", "600")
    settings = Settings(_env_file=None)
    assert settings.parsed_tiktok_providers == ["musicaldown", "tikwm"]
    assert settings.parsed_youtube_providers == ["cobalt", "ytmp3"]
    assert settings.parsed_disabled_providers == {"cobalt", "tikdownloader"}
    assert settings.cobalt_url == "https://cobalt.example.com"
    assert settings.ytmp3_api_key == "custom_key"
    assert settings.provider_timeout == 25.5
    assert settings.provider_failure_threshold == 5
    assert settings.provider_cooldown_seconds == 600


def test_instagram_cookies_file_setting(monkeypatch):
    monkeypatch.setenv("APP_ID", "1")
    monkeypatch.setenv("APP_HASH", "h")
    monkeypatch.setenv("BOT_TOKEN", "t")
    settings = Settings(_env_file=None)
    assert settings.instagram_cookies_file is None

    monkeypatch.setenv("INSTAGRAM_COOKIES_FILE", "/path/to/ig_cookies.txt")
    settings_with_cookies = Settings(_env_file=None)
    assert settings_with_cookies.instagram_cookies_file == "/path/to/ig_cookies.txt"


def test_flood_sleep_threshold_defaults_to_zero_and_accepts_custom(monkeypatch):
    monkeypatch.setenv("APP_ID", "1")
    monkeypatch.setenv("APP_HASH", "h")
    monkeypatch.setenv("BOT_TOKEN", "t")
    settings = Settings(_env_file=None)
    assert settings.flood_sleep_threshold == 0

    monkeypatch.setenv("FLOOD_SLEEP_THRESHOLD", "60")
    settings_custom = Settings(_env_file=None)
    assert settings_custom.flood_sleep_threshold == 60





# =============================================================================
# M11.9: ARCHIVE_CHANNEL normalization.
#
# Telethon resolves any string of digits (optionally with a leading "-",
# e.g. the "-100..." IDs Telegram uses for channels/supergroups) as a
# *phone number* via GetContactsRequest - which bots cannot call, so a
# numeric channel ID passed through as a plain string always failed in
# production with "Cannot get entity by phone number as a bot". A numeric
# ARCHIVE_CHANNEL must become an `int` so Telethon resolves it as a peer ID
# instead; an "@username" stays a `str`; anything else is a clear
# configuration error raised at startup.
# =============================================================================


def _base_env(monkeypatch):
    monkeypatch.setenv("APP_ID", "1")
    monkeypatch.setenv("APP_HASH", "h")
    monkeypatch.setenv("BOT_TOKEN", "t")


def test_archive_channel_unset_is_none(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.delenv("ARCHIVE_CHANNEL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.archive_channel is None


def test_archive_channel_numeric_string_becomes_int(monkeypatch):
    """The exact value from the production incident: a channel ID must be
    converted to `int`, not left as the `str` Telethon misparses as a phone
    number."""
    _base_env(monkeypatch)
    monkeypatch.setenv("ARCHIVE_CHANNEL", "-1003534083142")
    settings = Settings(_env_file=None)
    assert settings.archive_channel == -1003534083142
    assert isinstance(settings.archive_channel, int)


def test_archive_channel_username_stays_string(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("ARCHIVE_CHANNEL", "@my_archive_channel")
    settings = Settings(_env_file=None)
    assert settings.archive_channel == "@my_archive_channel"
    assert isinstance(settings.archive_channel, str)


def test_archive_channel_invalid_format_raises_clear_error(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("ARCHIVE_CHANNEL", "not-a-valid-channel-id")
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    message = str(excinfo.value)
    assert "ARCHIVE_CHANNEL" in message
    assert "@" in message  # points the operator at the supported formats


def test_archive_channel_positive_numeric_id_also_becomes_int(monkeypatch):
    """Not every valid channel/user peer ID carries a leading '-'."""
    _base_env(monkeypatch)
    monkeypatch.setenv("ARCHIVE_CHANNEL", "123456789")
    settings = Settings(_env_file=None)
    assert settings.archive_channel == 123456789
    assert isinstance(settings.archive_channel, int)
