"""yfinance data feed with pre-market / after-hours support."""

import logging
import math
from datetime import datetime
from typing import Dict, Optional, Tuple

import pandas as pd
import pytz
import yfinance as yf

logger = logging.getLogger(__name__)


def _finite(value):
    """None for anything JSON cannot carry — NaN and infinity."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if math.isfinite(value) else None
    return value


def _usable(value) -> bool:
    """True for a number that can safely be divided by.

    NaN is truthy and compares unequal to zero, so `if value and value != 0`
    let it straight through.
    """
    return isinstance(value, (int, float)) and math.isfinite(value) and value != 0

NYSE_TZ = pytz.timezone("America/New_York")

# Market session boundaries (hour, minute) in ET
_PRE_OPEN   = (4,  0)
_REG_OPEN   = (9, 30)
_REG_CLOSE  = (16, 0)
_POST_CLOSE = (20, 0)

# Regular session length in minutes (9:30 → 16:00 ET = 6.5 h)
_REG_OPEN_MINUTES  = _REG_OPEN[0]  * 60 + _REG_OPEN[1]    # 570
_REG_CLOSE_MINUTES = _REG_CLOSE[0] * 60 + _REG_CLOSE[1]   # 960
_REG_SESSION_MINUTES = _REG_CLOSE_MINUTES - _REG_OPEN_MINUTES  # 390

# Floor for the session-elapsed fraction used in time-adjusted volume metrics.
# Avoids a near-zero denominator in the first minutes after the open inflating
# the pace ratio to a meaningless number (≈ first 20 min of the 390-min session).
MIN_SESSION_FRACTION = 0.05


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


def session_elapsed_fraction(now: Optional[datetime] = None) -> float:
    """Fraction (0.0–1.0) of the regular trading session elapsed so far.

    0.0  → at or before the 9:30 ET open (pre-market)
    1.0  → at or after the 16:00 ET close (after-hours / closed)
    0.46 → ~3 hours into the session

    Used to measure volume spikes relative to trading time: a stock that has
    already traded a full day's average volume only 3 hours in is spiking,
    even though the raw full-day comparison wouldn't flag it yet.
    """
    now = now or datetime.now(NYSE_TZ)
    minutes = now.hour * 60 + now.minute + now.second / 60.0
    if minutes <= _REG_OPEN_MINUTES:
        return 0.0
    if minutes >= _REG_CLOSE_MINUTES:
        return 1.0
    return (minutes - _REG_OPEN_MINUTES) / _REG_SESSION_MINUTES


def is_market_open(include_extended: bool = True) -> bool:
    session = get_market_session()
    if session == "regular":
        return True
    return include_extended and session in ("pre", "after")


# ── Data feed ─────────────────────────────────────────────────────────────────

class StockDataFeed:
    """Thin wrapper around yfinance.Ticker with per-call fresh instances."""

    def __init__(self) -> None:
        pass

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _ticker(self, symbol: str) -> yf.Ticker:
        # Always create a fresh Ticker so fast_info and history don't return
        # stale cached values from the previous polling cycle.
        return yf.Ticker(symbol)

    @staticmethod
    def _fi_get(fast_info, *attrs) -> Optional[float]:
        """Return the first usable float from fast_info, skipping on any error.

        yfinance property getters can raise (not just AttributeError) when the
        underlying network call fails, so each access is individually guarded.

        NaN counts as missing. yfinance returns float('nan') for a field it
        could not resolve, and NaN survives every ``is not None`` check, then
        propagates through the arithmetic into the API response — where
        ``json.dumps(allow_nan=False)`` rejects it and fails the whole request.
        """
        for attr in attrs:
            try:
                val = getattr(fast_info, attr)
                if val is None:
                    continue
                val = float(val)
                if math.isfinite(val):
                    return val
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
                if _usable(prev_close)
                else 0.0
            )
            from_open_pct = (
                (price - open_price) / open_price * 100.0
                if _usable(open_price)
                else None
            )
            since_close_pct = (
                (price - regular_close) / regular_close * 100.0
                if _usable(regular_close) and session != "regular"
                else None
            )

            # Every number is filtered on the way out. A single NaN reaching
            # the price cache made /api/status fail to serialize, taking down
            # the whole live-monitoring tab.
            return {
                "symbol":          symbol,
                "price":           _finite(price),
                "prev_close":      _finite(prev_close),
                "open_price":      _finite(open_price),
                "regular_close":   _finite(regular_close),
                "change_pct":      _finite(change_pct),
                "from_open_pct":   _finite(from_open_pct),
                "since_close_pct": _finite(since_close_pct),
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
        """Accurate prices during pre/after-hours, from one intraday request.

        Everything is derived from a single 5-minute series that already spans
        pre, regular and post bars, so the daily-bar request this used to make
        alongside it is gone. That request was both an extra round trip per
        symbol and a source of NaN: Yahoo publishes a row for the current day
        before filling in its close, and the previous guard let that NaN
        through into since_close_pct and on into the API response.

        Returns current_price, regular_close and prev_close.
        """
        try:
            history = self._ticker(symbol).history(
                period="5d", interval="5m", prepost=True
            )
            if history is None or history.empty or "Close" not in history.columns:
                return None

            # Bars with no close are gaps, not prices.
            history = history[history["Close"].notna()]
            if history.empty:
                return None

            current_price = float(history["Close"].iloc[-1])
            if not math.isfinite(current_price):
                return None

            regular = self._regular_session_closes(history)
            return {
                "current_price": current_price,
                "regular_close": regular[-1] if regular else None,
                "prev_close": regular[-2] if len(regular) >= 2 else None,
            }

        except Exception as exc:
            logger.warning("Extended-hours history failed for %s: %s", symbol, exc)
            return None

    @staticmethod
    def _regular_session_closes(history: "pd.DataFrame") -> list:
        """Closing price of each regular session in an intraday series.

        The last bar of a regular session is the one starting at 15:55, so the
        close is the final bar of each day at or before that. Reading it from
        the intraday series rather than from daily bars is what removes the
        second request — and the blank-daily-bar problem with it.
        """
        try:
            index = history.index
            if index.tz is None:
                index = index.tz_localize("UTC")
            local = index.tz_convert(NYSE_TZ)
        except Exception:
            return []

        minutes = local.hour * 60 + local.minute
        regular = history[(minutes >= 9 * 60 + 30) & (minutes < 16 * 60)]
        if regular.empty:
            return []
        # One close per session, oldest first.
        by_day = regular.groupby(local[
            (minutes >= 9 * 60 + 30) & (minutes < 16 * 60)
        ].date)["Close"].last()
        return [float(v) for v in by_day.tolist() if math.isfinite(float(v))]

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
