"""Config loading from environment variables, matching the old bot's names."""

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


def test_db_dsn_defaults_to_local_sqlite(monkeypatch):
    monkeypatch.setenv("APP_ID", "1")
    monkeypatch.setenv("APP_HASH", "h")
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.delenv("DB_DSN", raising=False)
    settings = Settings(_env_file=None)
    assert settings.db_dsn == "sqlite:///database.sqlite3"
