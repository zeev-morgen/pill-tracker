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
import re
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

#: Why the last init_db call fell back to memory, or None when it did not.
#: Read by status(); set only here.
_last_error: Optional[str] = None


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


#: A connection URL with credentials in it, anywhere inside a longer string.
_URL_WITH_CREDENTIALS = re.compile(r"\b[a-z+]+://[^\s/@]*@?[^\s]*", re.IGNORECASE)


def _redact(message: str) -> str:
    """Remove the connection string from an error before it is displayed.

    Driver errors quote the DSN back — "could not connect to
    postgresql://user:pass@host" — and this text ends up in a browser and in
    logs that get pasted into chats.

    Scope, deliberately: URL-shaped text goes, and so does the configured
    DATABASE_URL wherever it appears verbatim. A hostname or username
    mentioned in prose does not — "password authentication failed for user
    neondb_owner" survives intact, because that sentence is the diagnosis and
    it contains no secret. The password is the thing that must never appear,
    and it only ever appears inside the URL.
    """
    configured = os.environ.get("DATABASE_URL", "").strip()
    if configured:
        message = message.replace(configured, "<DATABASE_URL>")
    return _URL_WITH_CREDENTIALS.sub("<url>", message)


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
    global _engine, _SessionFactory, _last_error

    url = url or os.environ.get("DATABASE_URL", "")
    if not url:
        logger.info("DATABASE_URL not set — using in-memory storage (data is not persisted)")
        _last_error = "DATABASE_URL is not set"
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
        _last_error = None
        return True
    except Exception as exc:
        # Deliberately broad: a malformed URL surfaces as UnicodeEncodeError or
        # ValueError rather than SQLAlchemyError, and an unreachable host as
        # OSError. None of these should take the monitor down — losing
        # persistence must not also cost us price polling and Telegram alerts.
        # Redacted here too, not only in status(): logs get pasted into chats
        # and issue trackers, and a driver error quotes the DSN back verbatim.
        logger.error(
            "PostgreSQL init failed (%s) — falling back to in-memory storage. "
            "Saved rows are untouched; this process just cannot read them. "
            "Check that DATABASE_URL holds a current connection string.",
            _redact(f"{exc.__class__.__name__}: {exc}")[:300],
        )
        _last_error = _redact(f"{exc.__class__.__name__}: {exc}")[:300]
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


def status() -> dict:
    """Whether persistence is live, and if not, why.

    Falling back to memory is silent by design — losing the database must not
    take the monitor down with it. The cost of that is a dashboard which then
    shows an empty portfolio and a watchlist rebuilt from config.yaml, both of
    which look exactly like data loss and neither of which says what happened.
    The reason went only to the host's logs; this keeps it reachable.

    Never includes the connection string or any part of it — the reason a
    database is unreachable is frequently the credentials in it.
    """
    return {
        "enabled": is_enabled(),
        "url_configured": bool(os.environ.get("DATABASE_URL", "").strip()),
        "error": _last_error,
    }


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
