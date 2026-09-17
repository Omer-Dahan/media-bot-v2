"""Session/engine setup for the shared database (same DSN as the old bot)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from media_bot_v2.db.models import Base


def build_engine(db_dsn: str) -> Engine:
    kwargs = {}
    if not db_dsn.startswith("sqlite"):
        kwargs = {
            "pool_size": 50,
            "max_overflow": 100,
            "pool_timeout": 30,
            "pool_recycle": 1800,
        }
    return create_engine(db_dsn, **kwargs)


def build_session_factory(db_dsn: str) -> sessionmaker[Session]:
    engine = build_engine(db_dsn)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
