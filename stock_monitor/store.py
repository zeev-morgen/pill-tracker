"""
Storage for alert history and portfolio holdings.

Both stores write to PostgreSQL when DATABASE_URL is configured (see db.py)
and fall back to in-memory structures otherwise, so the monitor still runs
locally with no database installed. The public API is identical in both modes.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime
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

#: How long a successful database read is reused before going back to the
#: database. This process is the only writer — every write updates the
#: in-memory copy and the row together — so a read on a timer can only ever
#: return what memory already holds.
#:
#: The number is set by compute billing, not by staleness. Managed Postgres
#: charges for the time the instance is awake and suspends it after about five
#: minutes idle. The polling loop asked for the watchlist every sixty seconds,
#: so it never once went idle: 720 compute-hours a month against a free tier's
#: ~190, which is precisely how this deployment lost its database. At an hour
#: the same loop wakes it roughly 24 times a day instead of 1,440 — a few
#: compute-hours a month, with the instance asleep the rest of the time.
#:
#: The cost of the long window: an edit made directly against the database,
#: outside this app, can take up to an hour to appear. Nothing else writes to
#: it, so that is a price worth paying.
READ_CACHE_TTL = 3600


def _positive(value, label: str) -> float:
    """A number greater than zero, or a Hebrew error naming the field."""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise HoldingError(f"{label} חייב להיות מספר") from exc
    if not math.isfinite(number) or number <= 0:
        raise HoldingError(f"{label} חייב להיות גדול מאפס")
    return number


def _cache_is_fresh(loaded_at: float) -> bool:
    """Whether a cached read is still good. Zero means "never loaded"."""
    return loaded_at > 0 and (time.monotonic() - loaded_at) < READ_CACHE_TTL


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
    #: When the position was opened. Drives holding duration in the journal.
    purchase_date: Optional[date] = None

    @staticmethod
    def create(
        ticker: str, quantity, entry_price, sector=None, asset_type=None,
        purchase_date=None,
    ) -> "Holding":
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

        purchase_date = _parse_date(purchase_date)
        if purchase_date is not None and purchase_date > date.today():
            raise HoldingError("מועד הרכישה לא יכול להיות בעתיד")

        return Holding(
            ticker=ticker,
            quantity=quantity,
            entry_price=entry_price,
            sector=sector,
            asset_type=asset_type,
            purchase_date=purchase_date,
        )

    def add_shares(self, quantity, price, purchase_date=None) -> "Holding":
        """Average an additional purchase into this position.

        Saving a ticker that already exists *replaces* it, so topping up by
        hand meant computing the weighted average yourself and typing the new
        total — and typing the added quantity instead of the total silently
        deleted the shares already held. This does the arithmetic instead.

        The original purchase date is kept: holding duration is measured from
        when the position was opened, not from the latest top-up. A date is
        only taken from the caller when none was ever recorded.
        """
        try:
            quantity = float(quantity)
            price = float(price)
        except (TypeError, ValueError) as exc:
            raise HoldingError("כמות ומחיר חייבים להיות מספרים") from exc
        if quantity <= 0:
            raise HoldingError("כמות חייבת להיות גדולה מאפס")
        if price <= 0:
            raise HoldingError("מחיר חייב להיות גדול מאפס")

        total = self.quantity + quantity
        average = (self.quantity * self.entry_price + quantity * price) / total

        kept_date = self.purchase_date
        if kept_date is None and purchase_date:
            kept_date = _parse_date(purchase_date)
            if kept_date is not None and kept_date > date.today():
                raise HoldingError("מועד הרכישה לא יכול להיות בעתיד")

        return Holding(
            ticker=self.ticker,
            # Rounded only to shed binary-float noise; six places is finer than
            # any real quantity or share price.
            quantity=round(total, 6),
            entry_price=round(average, 6),
            sector=self.sector,
            asset_type=self.asset_type,
            purchase_date=kept_date,
        )

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "sector": self.sector,
            "asset_type": self.asset_type,
            "purchase_date": self.purchase_date.isoformat() if self.purchase_date else None,
        }


def _parse_date(value) -> Optional[date]:
    """Accept a date, an ISO string, or blank (meaning 'not recorded')."""
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise HoldingError("מועד רכישה חייב להיות בפורמט YYYY-MM-DD") from exc


def _row_to_holding(row) -> Holding:
    return Holding(
        ticker=row.ticker,
        quantity=row.quantity,
        entry_price=row.entry_price,
        sector=row.sector,
        asset_type=row.asset_type,
        purchase_date=row.purchase_date,
    )


class PortfolioStore:
    """Manually entered positions (ticker, quantity, entry price).

    ``upsert`` doubles as the edit operation: saving an existing ticker
    replaces its quantity and entry price.
    """

    def __init__(self) -> None:
        self._holdings: Dict[str, Holding] = {}
        self._lock = threading.Lock()
        #: When the database was last read successfully. See READ_CACHE_TTL.
        self._loaded_at = 0.0

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
                                purchase_date=holding.purchase_date,
                            )
                        )
                    else:
                        row.quantity = holding.quantity
                        row.entry_price = holding.entry_price
                        row.sector = holding.sector
                        row.asset_type = holding.asset_type
                        row.purchase_date = holding.purchase_date
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
        if db.is_enabled() and not _cache_is_fresh(self._loaded_at):
            try:
                with db.session_scope() as session:
                    rows = session.scalars(
                        select(db.HoldingRow).order_by(db.HoldingRow.ticker)
                    ).all()
                holdings = [_row_to_holding(r) for r in rows]
                with self._lock:
                    self._holdings = {h.ticker: h for h in holdings}
                # Stamped only after the read succeeded. Stamping on failure
                # would cache an empty result and keep serving it.
                self._loaded_at = time.monotonic()
                return holdings
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


# ── Trade journal: closed positions ───────────────────────────────────────────

RATINGS = ("green", "orange", "red")


@dataclass
class ClosedPosition:
    """A recorded sale. ``fraction_sold`` < 1 means a partial exit."""

    ticker: str
    quantity: float
    entry_price: float
    exit_price: float
    sold_date: date
    pnl_pct: float
    pnl_value: float
    fraction_sold: float = 1.0
    #: Currency of entry_price, exit_price and pnl_value.
    currency: str = ""
    #: pnl_value in dollars, at the rate on the day of the sale. None when the
    #: rate was unavailable then — it cannot be recovered afterwards, so the
    #: journal reports the gap rather than converting at today's rate and
    #: presenting a number that was never realised.
    pnl_value_usd: Optional[float] = None
    purchase_date: Optional[date] = None
    holding_days: Optional[int] = None
    atr_pct_at_close: Optional[float] = None
    sector: Optional[str] = None
    rating: Optional[str] = None
    ai_analysis: Optional[str] = None
    personal_note: Optional[str] = None
    id: Optional[int] = None

    @staticmethod
    def from_sale(
        holding: Holding,
        quantity_sold: float,
        exit_price: float,
        sold_date=None,
        atr_pct: Optional[float] = None,
        sector: Optional[str] = None,
        currency: str = "",
    ) -> "ClosedPosition":
        """Build a journal entry from a holding and the sale details."""
        try:
            quantity_sold = float(quantity_sold)
            exit_price = float(exit_price)
        except (TypeError, ValueError) as exc:
            raise HoldingError("כמות ומחיר מכירה חייבים להיות מספרים") from exc
        if quantity_sold <= 0:
            raise HoldingError("כמות למכירה חייבת להיות גדולה מאפס")
        if quantity_sold > holding.quantity + 1e-9:
            raise HoldingError(
                f"לא ניתן למכור {quantity_sold:g} — בפוזיציה יש {holding.quantity:g}"
            )
        if exit_price <= 0:
            raise HoldingError("מחיר מכירה חייב להיות גדול מאפס")

        sold = _parse_date(sold_date) or date.today()
        if sold > date.today():
            raise HoldingError("מועד המכירה לא יכול להיות בעתיד")
        if holding.purchase_date and sold < holding.purchase_date:
            raise HoldingError("מועד המכירה מוקדם ממועד הרכישה")

        holding_days = (
            (sold - holding.purchase_date).days if holding.purchase_date else None
        )
        pnl_value = (exit_price - holding.entry_price) * quantity_sold
        # Converted here, at the sale, because this is the rate that was
        # actually realised. Reading the journal a year later and converting at
        # that day's rate would report a dollar profit the trade never made.
        from . import fx

        pnl_value_usd = fx.to_usd(pnl_value, currency)
        return ClosedPosition(
            ticker=holding.ticker,
            quantity=quantity_sold,
            entry_price=holding.entry_price,
            exit_price=exit_price,
            purchase_date=holding.purchase_date,
            sold_date=sold,
            holding_days=holding_days,
            pnl_pct=(exit_price - holding.entry_price) / holding.entry_price * 100.0,
            pnl_value=pnl_value,
            currency=currency,
            pnl_value_usd=pnl_value_usd,
            fraction_sold=quantity_sold / holding.quantity,
            atr_pct_at_close=atr_pct,
            sector=sector or holding.sector,
        )

    #: Figures a correction may change. Everything else on the entry is either
    #: derived from these or is not a fact about the trade.
    REVISABLE = ("quantity", "entry_price", "exit_price", "purchase_date", "sold_date")

    def revised(self, **changes) -> "ClosedPosition":
        """A copy with corrected figures and every derived value recomputed.

        A recorded sale can be wrong in ways only the person who made it knows
        about — the shares sold came from a second account with a different
        cost basis, so the averaged entry price the app held was not the basis
        of *those* shares. Correcting the entry price by hand and leaving
        pnl_pct beside it would produce an entry that contradicts itself, so
        everything downstream of the change is recomputed rather than kept.
        """
        unknown = set(changes) - set(self.REVISABLE)
        if unknown:
            raise ValueError(f"unsupported fields: {sorted(unknown)}")

        quantity = _positive(changes.get("quantity", self.quantity), "כמות")
        entry_price = _positive(changes.get("entry_price", self.entry_price), "מחיר כניסה")
        exit_price = _positive(changes.get("exit_price", self.exit_price), "מחיר יציאה")

        sold = (
            _parse_date(changes["sold_date"]) if "sold_date" in changes else self.sold_date
        ) or self.sold_date
        purchased = (
            _parse_date(changes["purchase_date"]) if "purchase_date" in changes
            else self.purchase_date
        )
        if sold > date.today():
            raise HoldingError("מועד המכירה לא יכול להיות בעתיד")
        if purchased and sold < purchased:
            raise HoldingError("מועד המכירה מוקדם ממועד הרכישה")

        pnl_value = (exit_price - entry_price) * quantity
        return ClosedPosition(
            id=self.id,
            ticker=self.ticker,
            quantity=quantity,
            entry_price=entry_price,
            exit_price=exit_price,
            purchase_date=purchased,
            sold_date=sold,
            holding_days=(sold - purchased).days if purchased else None,
            pnl_pct=(exit_price - entry_price) / entry_price * 100.0,
            pnl_value=pnl_value,
            currency=self.currency,
            pnl_value_usd=self._revised_usd(pnl_value),
            fraction_sold=self._revised_fraction(quantity),
            atr_pct_at_close=self.atr_pct_at_close,
            sector=self.sector,
            # The verdict was reached about the old figures. Kept only while
            # they still hold; see _drops_analysis.
            rating=None if self._drops_analysis(changes) else self.rating,
            ai_analysis=None if self._drops_analysis(changes) else self.ai_analysis,
            # The user's own retrospective is theirs, and survives regardless.
            personal_note=self.personal_note,
        )

    def _drops_analysis(self, changes: dict) -> bool:
        """Whether a change invalidates the AI verdict attached to this entry.

        Money changed means the trade being graded changed, and a green light
        sitting beside numbers it never saw is worse than no light at all. A
        date correction leaves the grade standing — holding time is part of the
        picture, but it is not what the verdict is about.
        """
        return any(
            key in changes and changes[key] != getattr(self, key)
            for key in ("quantity", "entry_price", "exit_price")
        )

    def _revised_usd(self, pnl_value: float) -> Optional[float]:
        """The corrected P&L in dollars, at the rate the sale actually got.

        Scaled from the stored pair rather than re-converted, because the rate
        that applied is the one on the day of the sale and it is not recorded
        anywhere else. Re-converting at today's rate would quietly restate a
        realised figure — the exact mistake pnl_value_usd exists to avoid.
        """
        if not self.currency:
            return pnl_value
        if self.pnl_value_usd is None or not self.pnl_value:
            return None
        return self.pnl_value_usd * (pnl_value / self.pnl_value)

    def _revised_fraction(self, quantity: float) -> float:
        """Fraction of the original position, kept consistent with the new size.

        The position's original total is recoverable from the pair already
        stored — quantity divided by fraction — so a corrected quantity can be
        expressed against the same total instead of being left labelled with a
        percentage that no longer matches it.
        """
        if not self.quantity or not self.fraction_sold:
            return self.fraction_sold
        return min(quantity * self.fraction_sold / self.quantity, 1.0)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "quantity": self.quantity,
            "entry_price": round(self.entry_price, 4),
            "exit_price": round(self.exit_price, 4),
            "purchase_date": self.purchase_date.isoformat() if self.purchase_date else None,
            "sold_date": self.sold_date.isoformat(),
            "holding_days": self.holding_days,
            "pnl_pct": round(self.pnl_pct, 2),
            "pnl_value": round(self.pnl_value, 2),
            # Currency of the three figures above; the dollar P&L is separate
            # so a Tel Aviv trade can show both what it made in agorot and what
            # that was worth on the day.
            "currency": self.currency or "",
            "pnl_value_usd": (
                round(self.pnl_value_usd, 2) if self.pnl_value_usd is not None
                else (round(self.pnl_value, 2) if not self.currency else None)
            ),
            "fraction_sold": round(self.fraction_sold, 4),
            "is_partial": self.fraction_sold < 0.999,
            "atr_pct_at_close": (
                round(self.atr_pct_at_close, 2) if self.atr_pct_at_close is not None else None
            ),
            "sector": self.sector,
            "rating": self.rating,
            "ai_analysis": self.ai_analysis,
            "personal_note": self.personal_note,
        }


def _row_to_closed(row) -> ClosedPosition:
    return ClosedPosition(
        id=row.id,
        ticker=row.ticker,
        quantity=row.quantity,
        entry_price=row.entry_price,
        exit_price=row.exit_price,
        purchase_date=row.purchase_date,
        sold_date=row.sold_date,
        holding_days=row.holding_days,
        pnl_pct=row.pnl_pct,
        pnl_value=row.pnl_value,
        # Rows written before these columns existed read back as NULL, which
        # is exactly right: no currency means dollars, and no stored USD P&L
        # means as_dict falls back to pnl_value for those.
        currency=getattr(row, "currency", None) or "",
        pnl_value_usd=getattr(row, "pnl_value_usd", None),
        fraction_sold=row.fraction_sold,
        atr_pct_at_close=row.atr_pct_at_close,
        sector=row.sector,
        rating=row.rating,
        ai_analysis=row.ai_analysis,
        personal_note=row.personal_note,
    )


class ClosedPositionStore:
    """Journal of sold positions, newest first."""

    def __init__(self) -> None:
        self._rows: List[ClosedPosition] = []
        self._next_id = 1
        self._lock = threading.Lock()

    def add(self, closed: ClosedPosition) -> ClosedPosition:
        if db.is_enabled():
            try:
                with db.session_scope() as session:
                    row = db.ClosedPositionRow(
                        ticker=closed.ticker,
                        quantity=closed.quantity,
                        entry_price=closed.entry_price,
                        exit_price=closed.exit_price,
                        purchase_date=closed.purchase_date,
                        sold_date=closed.sold_date,
                        holding_days=closed.holding_days,
                        pnl_pct=closed.pnl_pct,
                        pnl_value=closed.pnl_value,
                        currency=closed.currency or None,
                        pnl_value_usd=closed.pnl_value_usd,
                        fraction_sold=closed.fraction_sold,
                        atr_pct_at_close=closed.atr_pct_at_close,
                        sector=closed.sector,
                        rating=closed.rating,
                        ai_analysis=closed.ai_analysis,
                        personal_note=closed.personal_note,
                    )
                    session.add(row)
                    session.flush()
                    closed.id = row.id
                return closed
            except (SQLAlchemyError, RuntimeError) as exc:
                logger.error("Failed to persist closed position %s: %s", closed.ticker, exc)
        with self._lock:
            closed.id = self._next_id
            self._next_id += 1
            self._rows.insert(0, closed)
        return closed

    def all(self, limit: int = 200) -> List[ClosedPosition]:
        if db.is_enabled():
            try:
                with db.session_scope() as session:
                    rows = session.scalars(
                        select(db.ClosedPositionRow)
                        .order_by(desc(db.ClosedPositionRow.sold_date),
                                  desc(db.ClosedPositionRow.id))
                        .limit(limit)
                    ).all()
                return [_row_to_closed(r) for r in rows]
            except (SQLAlchemyError, RuntimeError) as exc:
                logger.error("Failed to read closed positions: %s", exc)
        with self._lock:
            return list(self._rows)[:limit]

    def get(self, entry_id: int) -> Optional[ClosedPosition]:
        if db.is_enabled():
            try:
                with db.session_scope() as session:
                    row = session.get(db.ClosedPositionRow, entry_id)
                return _row_to_closed(row) if row else None
            except (SQLAlchemyError, RuntimeError) as exc:
                logger.error("Failed to read closed position %s: %s", entry_id, exc)
        with self._lock:
            return next((r for r in self._rows if r.id == entry_id), None)

    def revise(self, entry_id: int, **changes) -> Optional[ClosedPosition]:
        """Correct a recorded sale's figures, recomputing everything derived.

        Separate from update_fields because this does not set what it is given:
        a corrected entry price implies a different return, profit and dollar
        value, and writing one without the others would leave the entry
        disagreeing with itself.
        """
        existing = self.get(entry_id)
        if existing is None:
            return None
        revised = existing.revised(**changes)
        return self.update_fields(
            entry_id,
            **{
                field: getattr(revised, field)
                for field in (
                    "quantity", "entry_price", "exit_price", "purchase_date",
                    "sold_date", "holding_days", "pnl_pct", "pnl_value",
                    "pnl_value_usd", "fraction_sold", "rating", "ai_analysis",
                )
            },
        )

    def update_fields(self, entry_id: int, **fields) -> Optional[ClosedPosition]:
        """Patch fields on an existing entry, exactly as given.

        Callers correcting a trade should use revise() instead — it recomputes
        the values that follow from the ones being changed.
        """
        allowed = {
            "rating", "ai_analysis", "personal_note",
            "quantity", "entry_price", "exit_price", "purchase_date",
            "sold_date", "holding_days", "pnl_pct", "pnl_value",
            "pnl_value_usd", "fraction_sold",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported fields: {sorted(unknown)}")
        if "rating" in fields and fields["rating"] not in (*RATINGS, None):
            raise HoldingError(f"דירוג חייב להיות אחד מ-{RATINGS}")

        if db.is_enabled():
            try:
                with db.session_scope() as session:
                    row = session.get(db.ClosedPositionRow, entry_id)
                    if row is None:
                        return None
                    for key, value in fields.items():
                        setattr(row, key, value)
                    session.flush()
                    return _row_to_closed(row)
            except (SQLAlchemyError, RuntimeError) as exc:
                logger.error("Failed to update closed position %s: %s", entry_id, exc)
                return None
        with self._lock:
            entry = next((r for r in self._rows if r.id == entry_id), None)
            if entry is None:
                return None
            for key, value in fields.items():
                setattr(entry, key, value)
            return entry


# ── Watchlist ─────────────────────────────────────────────────────────────────

class WatchlistStore:
    """Monitored symbols, editable at runtime.

    Empty means "not configured here" — callers fall back to config.yaml, so an
    existing deployment keeps its watchlist until the user edits one in the UI.
    """

    def __init__(self) -> None:
        self._symbols: List[str] = []
        self._lock = threading.Lock()
        #: When the database was last read successfully. See READ_CACHE_TTL.
        self._loaded_at = 0.0

    def all(self) -> List[str]:
        if db.is_enabled() and not _cache_is_fresh(self._loaded_at):
            try:
                with db.session_scope() as session:
                    rows = session.scalars(
                        select(db.WatchlistRow).order_by(db.WatchlistRow.symbol)
                    ).all()
                symbols = [r.symbol for r in rows]
                with self._lock:
                    self._symbols = list(symbols)
                self._loaded_at = time.monotonic()
                return symbols
            except (SQLAlchemyError, RuntimeError) as exc:
                logger.error("Failed to read watchlist: %s", exc)
        with self._lock:
            return list(self._symbols)

    @staticmethod
    def normalize(symbol: str) -> str:
        """Validate the shape of a symbol without storing it.

        Split out from ``add`` so a caller can check the symbol actually exists
        before committing it — validating the characters and saving in one step
        left no room to ask the data source anything.
        """
        symbol = str(symbol).strip().upper()
        if not symbol or len(symbol) > 16:
            raise HoldingError("יש להזין טיקר תקין")
        if not all(c.isalnum() or c in ".-^" for c in symbol):
            raise HoldingError("טיקר יכול להכיל אותיות, ספרות ותווי '.', '-', '^' בלבד")
        return symbol

    def add(self, symbol: str) -> str:
        symbol = self.normalize(symbol)

        with self._lock:
            if symbol not in self._symbols:
                self._symbols.append(symbol)
                self._symbols.sort()
        if db.is_enabled():
            try:
                with db.session_scope() as session:
                    if session.get(db.WatchlistRow, symbol) is None:
                        session.add(db.WatchlistRow(symbol=symbol))
            except (SQLAlchemyError, RuntimeError) as exc:
                logger.error("Failed to persist watchlist symbol %s: %s", symbol, exc)
        return symbol

    def remove(self, symbol: str) -> bool:
        symbol = str(symbol).strip().upper()
        with self._lock:
            removed = symbol in self._symbols
            if removed:
                self._symbols.remove(symbol)
        if db.is_enabled():
            try:
                with db.session_scope() as session:
                    result = session.execute(
                        delete(db.WatchlistRow).where(db.WatchlistRow.symbol == symbol)
                    )
                    removed = removed or result.rowcount > 0
            except (SQLAlchemyError, RuntimeError) as exc:
                logger.error("Failed to remove watchlist symbol %s: %s", symbol, exc)
        return removed

    def seed(self, symbols: List[str]) -> None:
        """Populate from config.yaml on first run, without overwriting edits.

        "Empty" only means "first run" when the store can actually be read. If
        a database is configured but unreachable, every read comes back empty —
        and seeding on that would refill the watchlist with config.yaml's
        symbols, which is how a user who curated their list months ago finds it
        replaced by tickers they removed. Better to monitor nothing for a few
        minutes than to show a list that is confidently wrong.
        """
        state = db.status()
        if state["url_configured"] and not state["enabled"]:
            logger.warning(
                "Watchlist not seeded — a database is configured but unreachable, "
                "so an empty watchlist means 'cannot read' rather than 'first run'."
            )
            return
        if self.all():
            return
        for symbol in symbols:
            try:
                self.add(symbol)
            except HoldingError:
                logger.warning("Skipping invalid watchlist symbol from config: %s", symbol)


closed_position_store = ClosedPositionStore()
watchlist_store = WatchlistStore()
