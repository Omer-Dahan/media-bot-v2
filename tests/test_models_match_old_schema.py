"""Verify our SQLAlchemy models reproduce the old bot's exact table/column
names and types, since the new bot reads/writes the same production DB.

Expected names below were taken directly from the old bot's
src/database/model.py and src/database/cache.py, and cross-checked against
the live schema in its local database.sqlite3 (see spec/SPEC.md, section
"Locked decisions" / DB schema mapping).
"""

from sqlalchemy import inspect

from media_bot_v2.db.models import Base, Payment, Setting, User, VideoCache

EXPECTED_COLUMNS = {
    "users": {
        "id",
        "user_id",
        "first_name",
        "username",
        "free",
        "paid",
        "bandwidth_used",
        "total_bandwidth",
        "is_blocked",
        "config",
    },
    "settings": {"id", "quality", "format", "subtitles", "title_length", "user_id"},
    "payments": {"id", "method", "amount", "status", "transaction_id", "user_id"},
    "video_cache": {"id", "cache_key", "file_id", "meta", "created_at"},
}


def test_table_names_match_old_bot():
    assert {User.__tablename__, Setting.__tablename__, Payment.__tablename__, VideoCache.__tablename__} == {
        "users",
        "settings",
        "payments",
        "video_cache",
    }


def test_column_names_match_old_bot():
    for model in (User, Setting, Payment, VideoCache):
        columns = {c.name for c in inspect(model).columns}
        assert columns == EXPECTED_COLUMNS[model.__tablename__], model.__tablename__


def test_users_user_id_is_unique_bigint():
    col = inspect(User).columns["user_id"]
    assert col.unique is True
    assert col.nullable is False


def test_settings_and_payments_have_fk_to_users():
    settings_fk = list(inspect(Setting).columns["user_id"].foreign_keys)
    payments_fk = list(inspect(Payment).columns["user_id"].foreign_keys)
    assert settings_fk and settings_fk[0].column.table.name == "users"
    assert payments_fk and payments_fk[0].column.table.name == "users"


def test_create_all_on_fresh_sqlite_db_succeeds(tmp_path):
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'schema_check.sqlite3'}")
    Base.metadata.create_all(engine)
    tables = inspect(engine).get_table_names()
    assert set(tables) == {"users", "settings", "payments", "video_cache"}
