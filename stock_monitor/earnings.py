"""Earnings date monitor — alerts N days before a tracked symbol reports results."""

import logging
from datetime import date, datetime
from typing import List, Optional, Set

import pytz
import yfinance as yf

logger = logging.getLogger(__name__)
NYSE_TZ = pytz.timezone("America/New_York")


class EarningsMonitor:
    DEFAULT_ALERT_DAYS = [7, 3, 1]

    def __init__(
        self,
        symbols: List[str],
        dispatcher,                           # NotificationDispatcher
        alert_days: Optional[List[int]] = None,
    ) -> None:
        self._symbols    = [s.upper() for s in symbols]
        self._dispatcher = dispatcher
        self._alert_days = sorted(alert_days or self.DEFAULT_ALERT_DAYS, reverse=True)
        # (symbol, days_before, earnings_date_str) — prevents duplicate alerts
        self._alerted: Set[tuple] = set()

    # ── Public ────────────────────────────────────────────────────────────────

    async def daily_check(self) -> None:
        """Scheduled once per weekday morning. Fires alerts where due."""
        logger.info("EarningsMonitor: daily check (%d symbols)", len(self._symbols))
        today = datetime.now(NYSE_TZ).date()
        for symbol in self._symbols:
            try:
                self._check_symbol(symbol, today)
            except Exception as exc:
                logger.error("Earnings check failed for %s: %s", symbol, exc)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _check_symbol(self, symbol: str, today: date) -> None:
        earnings_date = self._fetch_next_earnings(symbol)
        if earnings_date is None:
            logger.debug("No upcoming earnings date found for %s", symbol)
            return

        days = (earnings_date - today).days
        if days < 0 or days not in self._alert_days:
            return

        key = (symbol, days, str(earnings_date))
        if key in self._alerted:
            return
        self._alerted.add(key)

        date_str = earnings_date.strftime("%d/%m/%Y")
        title    = f"📅 התראת דיווח תוצאות: {symbol}"
        body     = (
            f"*{symbol}* עתיד לדווח תוצאות בעוד *{days} ימים* ({date_str}).\n"
            "⚠️ כדאי להתכונן מראש."
        )
        self._dispatcher.dispatch_raw(title, body)
        logger.info("Earnings alert dispatched: %s in %d days (%s)", symbol, days, date_str)

    def _fetch_next_earnings(self, symbol: str) -> Optional[date]:
        """Return the next future earnings date for *symbol*, or None."""
        try:
            ticker = yf.Ticker(symbol)
            today  = datetime.now(NYSE_TZ).date()

            # ── Try .calendar first (lightweight) ─────────────────────────
            cal = ticker.calendar
            if cal is not None and not (hasattr(cal, "empty") and cal.empty):
                if isinstance(cal, dict):
                    ed = cal.get("Earnings Date")
                    if ed:
                        if isinstance(ed, (list, tuple)):
                            ed = ed[0]
                        ed_date = ed.date() if hasattr(ed, "date") else ed
                        if isinstance(ed_date, date) and ed_date >= today:
                            return ed_date
                else:
                    # DataFrame — index contains field names
                    if "Earnings Date" in cal.index:
                        raw     = cal.loc["Earnings Date"]
                        val     = raw.iloc[0] if hasattr(raw, "iloc") else raw
                        ed_date = val.date() if hasattr(val, "date") else val
                        if isinstance(ed_date, date) and ed_date >= today:
                            return ed_date

            # ── Fall back to earnings_dates DataFrame ──────────────────────
            ed_df = ticker.earnings_dates
            if ed_df is not None and not ed_df.empty:
                future = [idx for idx in ed_df.index if idx.date() >= today]
                if future:
                    ts = min(future)   # min() is sort-order-safe regardless of DataFrame order
                    return ts.date() if hasattr(ts, "date") else ts

        except Exception as exc:
            logger.debug("Could not fetch earnings date for %s: %s", symbol, exc)

        return None
