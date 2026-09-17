"""MySQL dialect compatibility, without a MySQL server.

Compiles the models' DDL against sqlalchemy.dialects.mysql and checks the
generated CREATE TABLE statements name the same tables/columns as the old
bot's production schema (see spec/INVENTORY.md section 1). This catches
dialect-specific issues (column types, reserved words, enum rendering) that
the SQLite-backed tests in test_models_match_old_schema.py cannot, without
needing a real MySQL server.

For a real MySQL server, see tests/test_mysql_integration.py (README has
instructions to run it).
"""

import re

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from media_bot_v2.db.models import Base, Payment, ProviderHealth, Setting, User, VideoCache

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


def _compiled_ddl(model) -> str:
    return str(CreateTable(model.__table__).compile(dialect=mysql.dialect()))


def test_all_tables_compile_against_mysql_dialect():
    for model in (User, Setting, Payment, VideoCache):
        ddl = _compiled_ddl(model)
        assert f"CREATE TABLE {model.__tablename__}" in ddl


def test_mysql_ddl_columns_match_old_schema():
    for model in (User, Setting, Payment, VideoCache):
        ddl = _compiled_ddl(model)
        for column in EXPECTED_COLUMNS[model.__tablename__]:
            assert re.search(rf"\b{column}\b", ddl), f"{model.__tablename__}.{column} missing from MySQL DDL"


def test_settings_quality_and_format_render_as_old_bots_varchar_lengths():
    ddl = _compiled_ddl(Setting)
    assert "quality VARCHAR(6)" in ddl
    assert "format VARCHAR(8)" in ddl


def test_payments_status_renders_as_old_bots_varchar_length():
    ddl = _compiled_ddl(Payment)
    assert "status VARCHAR(9)" in ddl


def test_users_config_renders_as_json_on_mysql():
    ddl = _compiled_ddl(User)
    assert "config JSON" in ddl


def test_users_id_columns_render_as_expected_mysql_types():
    """Covers finding 7: column presence alone doesn't catch a type
    regression - user_id must stay BIGINT (a plain INTEGER truncates modern
    64-bit Telegram user IDs), and the bandwidth counters must stay BIGINT
    since they accumulate bytes transferred, not row counts."""
    ddl = _compiled_ddl(User)
    assert re.search(r"\buser_id BIGINT NOT NULL\b", ddl)
    assert re.search(r"\bbandwidth_used BIGINT\b", ddl)
    assert re.search(r"\btotal_bandwidth BIGINT\b", ddl)
    assert re.search(r"\bid INTEGER NOT NULL AUTO_INCREMENT\b", ddl)


def test_users_varchar_lengths_match_old_schema():
    ddl = _compiled_ddl(User)
    assert "first_name VARCHAR(100)" in ddl
    assert "username VARCHAR(100)" in ddl


def test_payments_columns_render_as_expected_mysql_types():
    ddl = _compiled_ddl(Payment)
    assert re.search(r"\buser_id INTEGER NOT NULL\b", ddl)  # FK to users.id (Integer PK)
    assert "transaction_id VARCHAR(100)" in ddl
    assert "method VARCHAR(50)" in ddl


def test_video_cache_columns_render_as_expected_mysql_types():
    ddl = _compiled_ddl(VideoCache)
    assert "cache_key VARCHAR(64)" in ddl


def test_provider_health_compiles_against_mysql_dialect():
    """ProviderHealth is a v2-only table added in M3, not present in the legacy schema.

    Verify it compiles cleanly to valid MySQL DDL without affecting legacy tables.
    """
    ddl = _compiled_ddl(ProviderHealth)
    assert "CREATE TABLE provider_health" in ddl
    assert "provider VARCHAR(50) NOT NULL" in ddl
    assert "platform VARCHAR(50) NOT NULL" in ddl
    assert "total_attempts INTEGER NOT NULL" in ddl
    assert "total_response_time FLOAT NOT NULL" in ddl
    assert "disabled_until DATETIME" in ddl


def test_metadata_create_all_compiles_for_every_table():
    """Full metadata (all shared tables + v2-only tables + FKs) must compile as one DDL batch."""
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))
        assert table.name in ddl

