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

        During regular hours: uses fast_info (fast, no extra network call).
        During extended hours: supplements fast_info with history() calls to
        get the accurate after-hours price and the real 4PM regular close —
        because fast_info.last_price returns the regular close during AH sessions.
        """
        try:
            fi      = self._ticker(symbol).fast_info
            session = get_market_session()

            # ── Base fields from fast_info ────────────────────────────────
            price = self._fi_get(fi, "last_price", "lastPrice", "regularMarketPrice")
            prev_close  = self._fi_get(
                fi,
                "previous_close", "previousClose",
                "regular_market_previous_close", "regularMarketPreviousClose",
            )
            volume     = self._fi_get(fi, "last_volume", "lastVolume", "day_volume", "dayVolume")
            day_high   = self._fi_get(fi, "day_high",  "dayHigh")
            day_low    = self._fi_get(fi, "day_low",   "dayLow")
            open_price = self._fi_get(fi, "open",      "regularMarketOpen")

            # ── Extended-hours correction ─────────────────────────────────
            # fast_info.last_price == regular close during AH/pre sessions in
            # yfinance ≥ 1.0, so we pull the real prices from 5-min history.
            regular_close: Optional[float] = None
            if session in ("after", "pre"):
                ext = self._get_extended_prices(symbol)
                if ext:
                    price         = ext["current_price"]   # real AH/pre price
                    regular_close = ext["regular_close"]   # 4PM close
                    if ext.get("prev_close"):
                        prev_close = ext["prev_close"]     # yesterday's close

            if price is None:
                logger.warning("No price available for %s", symbol)
                return None

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
            since_close_pct = (
                (price - regular_close) / regular_close * 100.0
                if regular_close and regular_close != 0 and session != "regular"
                else None
            )

            return {
                "symbol":          symbol,
                "price":           price,
                "prev_close":      prev_close,
                "open_price":      open_price,
                "regular_close":   regular_close,
                "change_pct":      change_pct,
                "from_open_pct":   from_open_pct,
                "since_close_pct": since_close_pct,
                "volume":          int(volume) if volume is not None else 0,
                "day_high":        day_high,
                "day_low":         day_low,
                "session":         session,
                "timestamp":       datetime.now(NYSE_TZ),
            }

        except Exception as exc:
            logger.error("Error fetching data for %s: %s", symbol, exc, exc_info=True)
            return None

    def _get_extended_prices(self, symbol: str) -> Optional[Dict]:
        """Return accurate prices during pre/after-hours from intraday history.

        Returns dict with keys: current_price, regular_close, prev_close (optional).
        """
        try:
            # 5-min bars including extended hours → last bar = current AH price
            hist_pre = self._ticker(symbol).history(
                period="2d", interval="5m", prepost=True
            )
            # Daily bars (no extended hours) → iloc[-1] = today's close, [-2] = yesterday
            hist_daily = self._ticker(symbol).history(
                period="5d", interval="1d", prepost=False
            )

            if hist_pre.empty:
                return None

            current_price = float(hist_pre["Close"].iloc[-1])

            regular_close: Optional[float] = None
            prev_close:    Optional[float] = None

            if not hist_daily.empty:
                # Today's regular session close is always the last bar in daily history
                regular_close = float(hist_daily["Close"].iloc[-1])
                if len(hist_daily) >= 2:
                    prev_close = float(hist_daily["Close"].iloc[-2])

            return {
                "current_price": current_price,
                "regular_close": regular_close,
                "prev_close":    prev_close,
            }

        except Exception as exc:
            logger.warning("Extended-hours history failed for %s: %s", symbol, exc)
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
