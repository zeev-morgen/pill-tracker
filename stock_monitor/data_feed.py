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
        """Return the first non-None value from fast_info attributes."""
        for attr in attrs:
            val = getattr(fast_info, attr, None)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
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

            price = self._fi_get(fi, "lastPrice", "regularMarketPrice")
            if price is None:
                logger.warning("No price available for %s", symbol)
                return None

            prev_close = self._fi_get(
                fi, "previousClose", "regularMarketPreviousClose"
            )
            volume = self._fi_get(fi, "lastVolume", "dayVolume", "regularMarketVolume")
            day_high = self._fi_get(fi, "dayHigh", "regularMarketDayHigh")
            day_low  = self._fi_get(fi, "dayLow",  "regularMarketDayLow")

            change_pct = (
                (price - prev_close) / prev_close * 100.0
                if prev_close and prev_close != 0
                else 0.0
            )

            return {
                "symbol":     symbol,
                "price":      price,
                "prev_close": prev_close,
                "change_pct": change_pct,
                "volume":     int(volume) if volume is not None else 0,
                "day_high":   day_high,
                "day_low":    day_low,
                "session":    get_market_session(),
                "timestamp":  datetime.now(NYSE_TZ),
            }

        except Exception as exc:
            logger.error("Error fetching data for %s: %s", symbol, exc, exc_info=True)
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
        """Return the mean daily volume over the last *days* trading sessions."""
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
