"""yfinance data feed with pre-market / after-hours support."""

import logging
from datetime import datetime
from typing import Dict, Optional, Tuple

import pandas as pd
import pytz
import yfinance as yf

logger = logging.getLogger(__name__)

NYSE_TZ = pytz.timezone("America/New_York")

# Market session boundaries (hour, minute) in ET
_PRE_OPEN   = (4,  0)
_REG_OPEN   = (9, 30)
_REG_CLOSE  = (16, 0)
_POST_CLOSE = (20, 0)


# ── Session helpers ───────────────────────────────────────────────────────────

def get_market_session() -> str:
    """Return the current NYSE/NASDAQ session: 'pre' | 'regular' | 'after' | 'closed'."""
    now = datetime.now(NYSE_TZ)
    if now.weekday() >= 5:          # Saturday / Sunday
        return "closed"
    t = (now.hour, now.minute)
    if _PRE_OPEN <= t < _REG_OPEN:
        return "pre"
    if _REG_OPEN <= t < _REG_CLOSE:
        return "regular"
    if _REG_CLOSE <= t < _POST_CLOSE:
        return "after"
    return "closed"


def is_market_open(include_extended: bool = True) -> bool:
    session = get_market_session()
    if session == "regular":
        return True
    return include_extended and session in ("pre", "after")


# ── Data feed ─────────────────────────────────────────────────────────────────

class StockDataFeed:
    """Thin wrapper around yfinance.Ticker with caching and error handling."""

    def __init__(self) -> None:
        self._tickers: Dict[str, yf.Ticker] = {}

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _ticker(self, symbol: str) -> yf.Ticker:
        if symbol not in self._tickers:
            self._tickers[symbol] = yf.Ticker(symbol)
        return self._tickers[symbol]

    @staticmethod
    def _fi_get(fast_info, *attrs) -> Optional[float]:
        """Return the first non-None float from fast_info, skipping on any error.

        yfinance property getters can raise (not just AttributeError) when the
        underlying network call fails, so each access is individually guarded.
        """
        for attr in attrs:
            try:
                val = getattr(fast_info, attr)
                if val is not None:
                    return float(val)
            except Exception:
                pass
        return None

    # ── Public API ────────────────────────────────────────────────────────────

    def get_current_data(self, symbol: str) -> Optional[Dict]:
        """
        Fetch the most current quote for *symbol*.

        Returns a dict with keys: symbol, price, prev_close, change_pct,
        volume, day_high, day_low, session, timestamp.
        Returns None on failure.
        """
        try:
            fi = self._ticker(symbol).fast_info

            # yfinance ≥ 1.0 uses snake_case; fall back to older camelCase names
            price = self._fi_get(
                fi, "last_price", "lastPrice", "regularMarketPrice"
            )
            if price is None:
                logger.warning("No price available for %s", symbol)
                return None

            prev_close = self._fi_get(
                fi,
                "previous_close", "previousClose",
                "regular_market_previous_close", "regularMarketPreviousClose",
            )
            volume = self._fi_get(
                fi, "last_volume", "lastVolume", "day_volume", "dayVolume"
            )
            day_high = self._fi_get(fi, "day_high", "dayHigh")
            day_low  = self._fi_get(fi, "day_low",  "dayLow")
            open_price = self._fi_get(fi, "open", "regularMarketOpen")

            change_pct = (
                (price - prev_close) / prev_close * 100.0
                if prev_close and prev_close != 0
                else 0.0
            )
            from_open_pct = (
                (price - open_price) / open_price * 100.0
                if open_price and open_price != 0
                else None
            )

            return {
                "symbol":        symbol,
                "price":         price,
                "prev_close":    prev_close,
                "open_price":    open_price,
                "change_pct":    change_pct,
                "from_open_pct": from_open_pct,
                "since_close_pct": None,   # filled in by monitor_cycle after-hours
                "regular_close": None,     # filled in by monitor_cycle
                "volume":        int(volume) if volume is not None else 0,
                "day_high":      day_high,
                "day_low":       day_low,
                "session":       get_market_session(),
                "timestamp":     datetime.now(NYSE_TZ),
            }

        except Exception as exc:
            logger.error("Error fetching data for %s: %s", symbol, exc, exc_info=True)
            return None

    def get_regular_close_price(self, symbol: str) -> Optional[float]:
        """Return today's regular-session closing price (last bar at/before 4 PM ET).

        Used to compute the after-hours since-close change when the app starts
        after the market has already closed.
        """
        try:
            hist = self._ticker(symbol).history(period="2d", interval="5m", prepost=False)
            if hist.empty:
                return None
            return float(hist["Close"].iloc[-1])
        except Exception as exc:
            logger.error("Error fetching regular close for %s: %s", symbol, exc)
            return None

    def get_intraday_history(
        self,
        symbol: str,
        period: str = "1d",
        interval: str = "1m",
        prepost: bool = True,
    ) -> Optional[pd.DataFrame]:
        """Intraday OHLCV bars, optionally including pre/after-hours candles."""
        try:
            df = self._ticker(symbol).history(
                period=period, interval=interval, prepost=prepost
            )
            return df if not df.empty else None
        except Exception as exc:
            logger.error("Error fetching history for %s: %s", symbol, exc, exc_info=True)
            return None

    def get_average_daily_volume(self, symbol: str, days: int = 10) -> Optional[float]:
        """Return the mean daily volume over the last *days* trading sessions.

        Tries fast_info.ten_day_average_volume first (instant, no extra request),
        then falls back to pulling daily bars.
        """
        try:
            fi = self._ticker(symbol).fast_info
            fast_val = self._fi_get(fi, "ten_day_average_volume", "threeMonthAverageVolume")
            if fast_val and fast_val > 0:
                return fast_val
        except Exception:
            pass

        try:
            hist = self._ticker(symbol).history(
                period=f"{days + 7}d", interval="1d"
            )
            if hist.empty or len(hist) < 2:
                return None
            return float(hist["Volume"].tail(days).mean())
        except Exception as exc:
            logger.error(
                "Error fetching avg volume for %s: %s", symbol, exc, exc_info=True
            )
            return None
