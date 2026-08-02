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
from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import DateTime, Float, Integer, String, Text, create_engine
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
    updated_at: Mapped[datetime] = mapped_column(
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
