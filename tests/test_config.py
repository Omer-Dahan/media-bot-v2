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
    assert settings.youtube_cookies_file is None
    assert settings.workers == 100
    assert settings.user_workers == 2


def test_youtube_and_concurrency_custom_values(monkeypatch):
    monkeypatch.setenv("APP_ID", "1")
    monkeypatch.setenv("APP_HASH", "h")
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("FORCE_IPV4", "true")
    monkeypatch.setenv("POTOKEN", "po_token_value")
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", "/path/to/cookies.txt")
    monkeypatch.setenv("WORKERS", "50")
    monkeypatch.setenv("USER_WORKERS", "4")
    settings = Settings(_env_file=None)
    assert settings.force_ipv4 is True
    assert settings.potoken == "po_token_value"
    assert settings.youtube_cookies_file == "/path/to/cookies.txt"
    assert settings.workers == 50
    assert settings.user_workers == 4
