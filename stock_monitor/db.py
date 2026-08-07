"""
PostgreSQL persistence layer (SQLAlchemy 2.0).

Enabled by setting DATABASE_URL. When it is unset the whole module reports
``is_enabled() is False`` and the stores fall back to in-memory behaviour, so
running locally without a database keeps working exactly as before.

Accepted URL forms (Render / Supabase / Neon all hand out the first one)::

    postgres://user:pass@host/db
    postgresql://user:pass@host/db
    postgresql+psycopg://user:pass@host/db
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class AlertRow(Base):
    """Persistent alert history — survives restarts and redeploys."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    alert_type: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(16))


class HoldingRow(Base):
    """A manually entered portfolio position."""

    __tablename__ = "holdings"

    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    # User-supplied overrides. yfinance frequently returns no sector at all, so
    # the dashboard lets the user set one; NULL means "fall back to yfinance".
    sector: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    asset_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    #: When the position was opened — drives holding duration in the journal.
    purchase_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class ClosedPositionRow(Base):
    """A sold position — the trade journal entry.

    A partial sale writes a row for the sold portion and leaves the remainder
    in ``holdings``, so one ticker can appear here several times.
    """

    __tablename__ = "closed_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float] = mapped_column(Float, nullable=False)
    purchase_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    sold_date: Mapped[date] = mapped_column(Date, nullable=False)
    holding_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pnl_pct: Mapped[float] = mapped_column(Float, nullable=False)
    pnl_value: Mapped[float] = mapped_column(Float, nullable=False)
    #: Fraction of the original position this sale represents (1.0 = full exit).
    fraction_sold: Mapped[float] = mapped_column(Float, default=1.0)
    #: Currency of entry_price, exit_price and pnl_value — 'ILA' for a Tel Aviv
    #: position, empty or 'USD' otherwise.
    currency: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    #: pnl_value in dollars, converted at the rate on the day of the sale. That
    #: rate is the one actually realised and cannot be reconstructed later, so
    #: it is stored rather than recomputed when the journal is read.
    pnl_value_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    atr_pct_at_close: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sector: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    #: Claude's verdict: 'green' | 'orange' | 'red'.
    rating: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    ai_analysis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The user's own retrospective — never written by the system.
    personal_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class WatchlistRow(Base):
    """Symbols monitored for alerts, editable from the dashboard.

    When empty the app falls back to the symbols in config.yaml, so an existing
    deployment keeps its watchlist until the user edits one from the UI.
    """

    __tablename__ = "watchlist"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


# ── Engine bootstrap ──────────────────────────────────────────────────────────

_engine = None
_SessionFactory: Optional[sessionmaker] = None


def _normalize_url(url: str) -> str:
    """Force the psycopg3 driver, which is what requirements.txt installs."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def init_db(url: Optional[str] = None) -> bool:
    """Create the engine and tables. Returns True when Postgres is active.

    Safe to call more than once. A connection failure is logged and downgraded
    to in-memory mode rather than crashing the monitor at startup.
    """
    global _engine, _SessionFactory

    url = url or os.environ.get("DATABASE_URL", "")
    if not url:
        logger.info("DATABASE_URL not set — using in-memory storage (data is not persisted)")
        return False

    try:
        _engine = create_engine(
            _normalize_url(url),
            pool_pre_ping=True,   # silently reconnect after idle disconnects
            pool_recycle=300,
            echo=False,
        )
        Base.metadata.create_all(_engine)
        _add_missing_columns()
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)
        logger.info("PostgreSQL connected — alerts and holdings are persisted")
        return True
    except Exception as exc:
        # Deliberately broad: a malformed URL surfaces as UnicodeEncodeError or
        # ValueError rather than SQLAlchemyError, and an unreachable host as
        # OSError. None of these should take the monitor down — losing
        # persistence must not also cost us price polling and Telegram alerts.
        logger.error(
            "PostgreSQL init failed (%s: %s) — falling back to in-memory storage. "
            "Check that DATABASE_URL holds the real connection string.",
            exc.__class__.__name__,
            exc,
        )
        _engine = None
        _SessionFactory = None
        return False


def _add_missing_columns() -> None:
    """Add columns introduced after a database was first created.

    ``create_all`` only creates missing *tables*, so an existing deployment
    would keep an outdated ``holdings`` table and every query naming a new
    column would fail. This keeps the schema current without pulling in a
    migration framework, which is overkill at this scale. Each statement is
    additive and nullable, so it is safe to re-run and never touches data.
    """
    inspector = inspect(_engine)
    tables = set(inspector.get_table_names())
    additions = {
        "holdings": {
            "sector": "VARCHAR(64)",
            "asset_type": "VARCHAR(16)",
            "purchase_date": "DATE",
        },
        "closed_positions": {
            # Which currency entry_price and exit_price are in. Without it an
            # old Tel Aviv entry's agorot figures are indistinguishable from
            # dollars once they are in the journal.
            "currency": "VARCHAR(8)",
            # P&L converted at the rate on the day of the sale — the rate
            # actually realised, which cannot be recovered afterwards.
            "pnl_value_usd": "DOUBLE PRECISION",
        },
    }
    for table, columns in additions.items():
        if table not in tables:
            continue
        existing = {col["name"] for col in inspector.get_columns(table)}
        for column, ddl_type in columns.items():
            if column in existing:
                continue
            # One statement per transaction, and failures are contained: if the
            # database user cannot ALTER (or the column arrives another way),
            # that must not abort init_db and cost us persistence for every
            # other table.
            try:
                with _engine.begin() as connection:
                    connection.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
                    )
                logger.info("Schema updated: added %s.%s", table, column)
            except SQLAlchemyError as exc:
                logger.error(
                    "Could not add %s.%s (%s) — reads may fall back to memory. "
                    "Grant the database user ALTER on '%s' to fix this.",
                    table, column, exc.__class__.__name__, table,
                )


def is_enabled() -> bool:
    return _SessionFactory is not None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session. Raises RuntimeError when the DB is not enabled."""
    if _SessionFactory is None:
        raise RuntimeError("database is not initialized")
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
