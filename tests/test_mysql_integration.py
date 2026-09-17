"""Optional integration test against a real MySQL server.

Skipped by default. To run it, set MYSQL_TEST_DSN to a throwaway MySQL
database (never the production DSN) and run:

    MYSQL_TEST_DSN="mysql+pymysql://user:pass@host/db" uv run pytest tests/test_mysql_integration.py -v

This creates all tables in that database, exercises basic CRUD through the
models, and drops the tables again. It never touches the production DB_DSN.
"""

import os

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from media_bot_v2.db.models import Base, User

MYSQL_TEST_DSN = os.environ.get("MYSQL_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not MYSQL_TEST_DSN,
    reason="MYSQL_TEST_DSN not set; see this file's docstring to run against real MySQL",
)


@pytest.fixture
def mysql_engine():
    engine = create_engine(MYSQL_TEST_DSN)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_create_all_matches_expected_tables_on_real_mysql(mysql_engine):
    tables = set(inspect(mysql_engine).get_table_names())
    assert {"users", "settings", "payments", "video_cache"} <= tables


def test_user_roundtrip_on_real_mysql(mysql_engine):
    factory = sessionmaker(bind=mysql_engine)
    with factory() as session:
        session.add(User(user_id=42, free=3, paid=0, is_blocked=0))
        session.commit()

    with factory() as session:
        user = session.query(User).filter(User.user_id == 42).one()
        assert user.free == 3
