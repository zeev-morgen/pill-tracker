"""
Storage for alert history and portfolio holdings.

Both stores write to PostgreSQL when DATABASE_URL is configured (see db.py)
and fall back to in-memory structures otherwise, so the monitor still runs
locally with no database installed. The public API is identical in both modes.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import delete, desc, select
from sqlalchemy.exc import SQLAlchemyError

from . import db

logger = logging.getLogger(__name__)

MAX_ALERTS = 100
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


# ── Alerts ────────────────────────────────────────────────────────────────────

@dataclass
class AlertRecord:
    timestamp: str
    symbol: str
    alert_type: str
    message: str
    severity: str


class AlertStore:
    """Recent alerts, newest first. Postgres-backed when available."""

    def __init__(self) -> None:
        self._alerts: deque = deque(maxlen=MAX_ALERTS)
        self._lock = threading.Lock()

    def add(self, symbol: str, alert_type: str, message: str, severity: str) -> None:
        record = AlertRecord(
            timestamp=datetime.now().strftime(_TS_FORMAT),
            symbol=symbol,
            alert_type=alert_type,
            message=message,
            severity=severity,
        )
        # Always keep the in-memory copy: it serves reads if the DB blips.
        with self._lock:
            self._alerts.appendleft(record)

        if not db.is_enabled():
            return
        try:
            with db.session_scope() as session:
                session.add(
                    db.AlertRow(
                        symbol=symbol,
                        alert_type=alert_type,
                        message=message,
                        severity=severity,
                    )
                )
        except (SQLAlchemyError, RuntimeError) as exc:
            logger.error("Failed to persist alert for %s: %s", symbol, exc)

    def recent(self, n: int = 50) -> List[AlertRecord]:
        if db.is_enabled():
            try:
                with db.session_scope() as session:
                    rows = session.scalars(
                        select(db.AlertRow).order_by(desc(db.AlertRow.created_at)).limit(n)
                    ).all()
                return [
                    AlertRecord(
                        timestamp=row.created_at.strftime(_TS_FORMAT),
                        symbol=row.symbol,
                        alert_type=row.alert_type,
                        message=row.message,
                        severity=row.severity,
                    )
                    for row in rows
                ]
            except (SQLAlchemyError, RuntimeError) as exc:
                logger.error("Failed to read alerts from database: %s", exc)
        with self._lock:
            return list(self._alerts)[:n]


# ── Portfolio holdings ────────────────────────────────────────────────────────

class HoldingError(ValueError):
    """Raised when a holding fails validation."""


#: Portfolio is split by these for the index-vs-single-stock breakdown.
ASSET_TYPES = ("stock", "etf")


@dataclass
class Holding:
    ticker: str
    quantity: float
    entry_price: float
    #: Manual sector override. None means "use whatever yfinance reports".
    sector: Optional[str] = None
    #: 'stock' or 'etf'. None means "detect automatically".
    asset_type: Optional[str] = None

    @staticmethod
    def create(ticker: str, quantity, entry_price, sector=None, asset_type=None) -> "Holding":
        """Validate and normalize user input. Raises HoldingError on bad input."""
        ticker = str(ticker).strip().upper()
        if not ticker or len(ticker) > 16:
            raise HoldingError("יש להזין טיקר תקין")
        if not all(c.isalnum() or c in ".-^" for c in ticker):
            raise HoldingError("טיקר יכול להכיל אותיות, ספרות ותווי '.', '-', '^' בלבד")
        try:
            quantity = float(quantity)
            entry_price = float(entry_price)
        except (TypeError, ValueError) as exc:
            raise HoldingError("כמות ומחיר כניסה חייבים להיות מספרים") from exc
        if quantity <= 0:
            raise HoldingError("כמות חייבת להיות גדולה מאפס")
        if entry_price <= 0:
            raise HoldingError("מחיר כניסה חייב להיות גדול מאפס")

        # Blank input means "no override", not an empty-string sector.
        sector = (str(sector).strip() or None) if sector is not None else None
        if sector is not None and len(sector) > 64:
            raise HoldingError("שם סקטור ארוך מדי (עד 64 תווים)")

        if asset_type is not None:
            asset_type = str(asset_type).strip().lower() or None
        if asset_type is not None and asset_type not in ASSET_TYPES:
            raise HoldingError("סוג נכס חייב להיות 'stock' או 'etf'")

        return Holding(
            ticker=ticker,
            quantity=quantity,
            entry_price=entry_price,
            sector=sector,
            asset_type=asset_type,
        )

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "sector": self.sector,
            "asset_type": self.asset_type,
        }


def _row_to_holding(row) -> Holding:
    return Holding(
        ticker=row.ticker,
        quantity=row.quantity,
        entry_price=row.entry_price,
        sector=row.sector,
        asset_type=row.asset_type,
    )


class PortfolioStore:
    """Manually entered positions (ticker, quantity, entry price).

    ``upsert`` doubles as the edit operation: saving an existing ticker
    replaces its quantity and entry price.
    """

    def __init__(self) -> None:
        self._holdings: Dict[str, Holding] = {}
        self._lock = threading.Lock()

    def upsert(self, holding: Holding) -> Holding:
        with self._lock:
            self._holdings[holding.ticker] = holding

        if db.is_enabled():
            try:
                with db.session_scope() as session:
                    row = session.get(db.HoldingRow, holding.ticker)
                    if row is None:
                        session.add(
                            db.HoldingRow(
                                ticker=holding.ticker,
                                quantity=holding.quantity,
                                entry_price=holding.entry_price,
                                sector=holding.sector,
                                asset_type=holding.asset_type,
                            )
                        )
                    else:
                        row.quantity = holding.quantity
                        row.entry_price = holding.entry_price
                        row.sector = holding.sector
                        row.asset_type = holding.asset_type
                        row.updated_at = datetime.now()
            except (SQLAlchemyError, RuntimeError) as exc:
                logger.error("Failed to persist holding %s: %s", holding.ticker, exc)
        return holding

    def get(self, ticker: str) -> Optional[Holding]:
        ticker = str(ticker).strip().upper()
        if db.is_enabled():
            try:
                with db.session_scope() as session:
                    row = session.get(db.HoldingRow, ticker)
                return _row_to_holding(row) if row else None
            except (SQLAlchemyError, RuntimeError) as exc:
                logger.error("Failed to read holding %s: %s", ticker, exc)
        with self._lock:
            return self._holdings.get(ticker)

    def all(self) -> List[Holding]:
        if db.is_enabled():
            try:
                with db.session_scope() as session:
                    rows = session.scalars(
                        select(db.HoldingRow).order_by(db.HoldingRow.ticker)
                    ).all()
                return [_row_to_holding(r) for r in rows]
            except (SQLAlchemyError, RuntimeError) as exc:
                logger.error("Failed to read holdings: %s", exc)
        with self._lock:
            return list(self._holdings.values())

    def delete(self, ticker: str) -> bool:
        ticker = str(ticker).strip().upper()
        with self._lock:
            removed = self._holdings.pop(ticker, None) is not None

        if db.is_enabled():
            try:
                with db.session_scope() as session:
                    result = session.execute(
                        delete(db.HoldingRow).where(db.HoldingRow.ticker == ticker)
                    )
                    removed = removed or result.rowcount > 0
            except (SQLAlchemyError, RuntimeError) as exc:
                logger.error("Failed to delete holding %s: %s", ticker, exc)
        return removed


# ── Global singletons shared across all modules ───────────────────────────────

alert_store = AlertStore()
portfolio_store = PortfolioStore()
