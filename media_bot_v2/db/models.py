"""SQLAlchemy 2.0 models mirroring the existing production database.

These table and column names are LOCKED: the bot keeps using the same bot
token and the same database as the old bot (media-downloader-bot), so user
records, settings, and payment history stay intact across the cutover. Do
not rename tables/columns and do not add a migration for this schema; it
already exists in production.

Verified against the old bot's src/database/model.py and src/database/cache.py,
and cross-checked against the live table definitions in its local
database.sqlite3 (SQLite dev copy; production uses MySQL via PyMySQL).

The Enum columns reproduce the exact VARCHAR lengths SQLAlchemy generated for
the old model (quality -> VARCHAR(6), format -> VARCHAR(8), payments.status ->
VARCHAR(9)), so a fresh `create_all()` on an empty database matches the
production DDL. JSON uses the dialect-neutral sqlalchemy.JSON (not
sqlalchemy.dialects.mysql.JSON like the old model) so the same model works
against both MySQL in prod and SQLite in tests; it renders as native JSON on
MySQL either way.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


class PaymentStatus:
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(100))
    username: Mapped[str | None] = mapped_column(String(100))
    free: Mapped[int | None] = mapped_column(Integer)
    paid: Mapped[int | None] = mapped_column(Integer)
    bandwidth_used: Mapped[int | None] = mapped_column(BigInteger)
    total_bandwidth: Mapped[int | None] = mapped_column(BigInteger)
    is_blocked: Mapped[int | None] = mapped_column(Integer)
    config: Mapped[dict | None] = mapped_column(JSON)

    settings: Mapped["Setting | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Setting(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    quality: Mapped[str] = mapped_column(
        Enum("high", "medium", "low", "audio", "custom", native_enum=False),
        nullable=False,
        default="high",
    )
    format: Mapped[str] = mapped_column(
        Enum("video", "audio", "document", native_enum=False),
        nullable=False,
        default="video",
    )
    subtitles: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title_length: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    user: Mapped["User"] = relationship(back_populates="settings")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    method: Mapped[str] = mapped_column(String(50), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(
            PaymentStatus.PENDING,
            PaymentStatus.COMPLETED,
            PaymentStatus.FAILED,
            PaymentStatus.REFUNDED,
            native_enum=False,
        ),
        nullable=False,
    )
    transaction_id: Mapped[str | None] = mapped_column(String(100))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    user: Mapped["User"] = relationship(back_populates="payments")


class VideoCache(Base):
    """Download cache: url+quality+format -> already-uploaded Telegram file_id(s).

    Matches the old bot's src/database/cache.py table, which stored this in
    the same SQLite file even though the module was named for a past Redis
    backend.
    """

    __tablename__ = "video_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cache_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    file_id: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=lambda: dt.datetime.now(dt.timezone.utc)
    )
