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
    assert settings.user_workers == 2


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
    assert settings.ytmp3_api_key == "9b0ed5dab31616027ad7154140b0272d"
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



