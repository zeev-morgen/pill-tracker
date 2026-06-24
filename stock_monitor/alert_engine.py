"""
Alert engine: evaluates custom trigger rules against live market data.

Supported alert types
─────────────────────
price_change_pct  — fires when price moves ± N % within a rolling time window
volume_spike      — fires when today's volume outpaces the 10-day average.
                    With time_adjusted (default), the benchmark is scaled to how
                    much of the trading session has elapsed, so a stock that has
                    already traded a full day's volume 3 hours in will spike —
                    instead of waiting for the raw full-day total to be exceeded.
price_threshold   — fires when price crosses above/below a fixed level
"""

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pytz

from .config import AlertConfig, AppConfig, StockConfig
from .data_feed import StockDataFeed, session_elapsed_fraction

logger = logging.getLogger(__name__)
NYSE_TZ = pytz.timezone("America/New_York")

# Floor for the session-elapsed fraction used in time-adjusted volume spikes.
# Avoids a near-zero denominator in the first minutes after the open blowing the
# pace ratio up to a meaningless number (≈ first 20 min of the 390-min session).
MIN_SESSION_FRACTION = 0.05


# ── Domain objects ────────────────────────────────────────────────────────────

@dataclass
class PricePoint:
    timestamp: datetime
    price: float
    volume: int


@dataclass
class AlertEvent:
    symbol: str
    alert_type: str
    message: str
    price: float
    timestamp: datetime
    severity: str = "INFO"      # INFO | WARNING | CRITICAL


# ── Engine ────────────────────────────────────────────────────────────────────

class AlertEngine:
    def __init__(self, config: AppConfig, data_feed: StockDataFeed) -> None:
        self.config    = config
        self.data_feed = data_feed

        # Rolling 1-min price/volume history per symbol (up to 3 h of 1-min bars)
        self._history: Dict[str, deque] = {}

        # Cooldown registry: (symbol, alert_key) → last_triggered UTC
        self._cooldowns: Dict[Tuple[str, str], datetime] = {}

        # 10-day average volume cache: symbol → (avg_volume, cache_timestamp)
        self._avg_vol_cache: Dict[str, Tuple[float, datetime]] = {}

        # Threshold crossing state: (symbol, direction, level) → was_triggered_last_poll
        # None = first observation (no alert fired yet), True/False = prior state
        self._threshold_states: Dict[Tuple[str, str, float], Optional[bool]] = {}

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _history_for(self, symbol: str) -> deque:
        if symbol not in self._history:
            self._history[symbol] = deque(maxlen=300)
        return self._history[symbol]

    def _on_cooldown(self, symbol: str, key: str, minutes: int) -> bool:
        ck = (symbol, key)
        if ck not in self._cooldowns:
            return False
        elapsed = (datetime.now(NYSE_TZ) - self._cooldowns[ck]).total_seconds() / 60
        return elapsed < minutes

    def _set_cooldown(self, symbol: str, key: str) -> None:
        self._cooldowns[(symbol, key)] = datetime.now(NYSE_TZ)

    def _avg_volume(self, symbol: str) -> Optional[float]:
        now = datetime.now(NYSE_TZ)
        if symbol in self._avg_vol_cache:
            avg, ts = self._avg_vol_cache[symbol]
            if (now - ts).total_seconds() < 4 * 3600:   # 4-hour cache
                return avg
        avg = self.data_feed.get_average_daily_volume(symbol)
        if avg:
            self._avg_vol_cache[symbol] = (avg, now)
        return avg

    # ── Public API ────────────────────────────────────────────────────────────

    def evaluate(
        self, symbol: str, stock_cfg: StockConfig, data: Dict
    ) -> List[AlertEvent]:
        """Record the latest data point and return any triggered AlertEvents."""
        self._history_for(symbol).append(
            PricePoint(
                timestamp=data["timestamp"],
                price=data["price"],
                volume=data["volume"],
            )
        )

        events: List[AlertEvent] = []
        for alert in stock_cfg.alerts:
            event = self._check(symbol, alert, data)
            if event:
                events.append(event)
        return events

    # ── Dispatchers ───────────────────────────────────────────────────────────

    def _check(
        self, symbol: str, alert: AlertConfig, data: Dict
    ) -> Optional[AlertEvent]:
        if alert.type == "price_change_pct":
            return self._check_price_change_pct(symbol, alert, data["price"])
        if alert.type == "volume_spike":
            return self._check_volume_spike(symbol, alert, data["volume"])
        if alert.type == "price_threshold":
            return self._check_price_threshold(symbol, alert, data["price"])
        logger.warning("Unknown alert type: %s", alert.type)
        return None

    # ── Individual checks ─────────────────────────────────────────────────────

    def _check_price_change_pct(
        self, symbol: str, alert: AlertConfig, price: float
    ) -> Optional[AlertEvent]:
        history = self._history_for(symbol)
        if len(history) < 2:
            return None

        cutoff    = datetime.now(NYSE_TZ) - timedelta(minutes=alert.window_minutes)
        window    = [p for p in history if p.timestamp >= cutoff]
        if len(window) < 2:
            return None

        base = window[0].price
        if base == 0:
            return None

        change_pct = (price - base) / base * 100.0
        if abs(change_pct) < alert.threshold_pct:
            return None

        key = f"price_change_pct_{alert.window_minutes}m_{alert.threshold_pct}"
        if self._on_cooldown(symbol, key, alert.cooldown_minutes):
            return None
        self._set_cooldown(symbol, key)

        direction = "UP" if change_pct > 0 else "DOWN"
        severity  = "WARNING" if abs(change_pct) >= alert.threshold_pct * 1.5 else "INFO"
        return AlertEvent(
            symbol=symbol,
            alert_type="price_change_pct",
            message=(
                f"${symbol} moved {direction} {abs(change_pct):.2f}% "
                f"in {alert.window_minutes} min "
                f"(${base:.2f} → ${price:.2f})"
            ),
            price=price,
            timestamp=datetime.now(NYSE_TZ),
            severity=severity,
        )

    def _check_volume_spike(
        self, symbol: str, alert: AlertConfig, volume: int
    ) -> Optional[AlertEvent]:
        if volume <= 0:
            return None

        avg = self._avg_volume(symbol)
        if not avg or avg <= 0:
            return None

        # Choose what to compare today's cumulative volume against.
        if alert.time_adjusted:
            frac = session_elapsed_fraction()
            if 0.0 < frac < 1.0:
                # Mid-session: scale the 10-day average to the elapsed portion of
                # the trading day, so a spike can fire intraday — e.g. a full
                # day's volume already traded in the first 3 hours. Clamp the
                # fraction so the noisy first minutes don't inflate the ratio.
                frac = max(frac, MIN_SESSION_FRACTION)
                expected = avg * frac
                benchmark = (
                    f"the pace expected by {frac * 100:.0f}% into the day "
                    f"({int(expected):,} of the {int(avg):,} 10-day avg)"
                )
            else:
                # Pre-market or after the close: no meaningful intraday pace, so
                # fall back to the plain full-day comparison.
                expected = avg
                benchmark = f"the 10-day avg ({int(avg):,})"
        else:
            expected = avg
            benchmark = f"the 10-day avg ({int(avg):,})"

        if expected <= 0:
            return None

        ratio = volume / expected
        if ratio < alert.multiplier:
            return None

        key = f"volume_spike_{alert.multiplier}x"
        if self._on_cooldown(symbol, key, alert.cooldown_minutes):
            return None
        self._set_cooldown(symbol, key)

        return AlertEvent(
            symbol=symbol,
            alert_type="volume_spike",
            message=(
                f"${symbol} volume spike: {volume:,} shares "
                f"= {ratio:.1f}× {benchmark}"
            ),
            price=0.0,
            timestamp=datetime.now(NYSE_TZ),
            severity="WARNING",
        )

    def _check_price_threshold(
        self, symbol: str, alert: AlertConfig, price: float
    ) -> Optional[AlertEvent]:
        if alert.above is not None:
            sk = (symbol, "above", alert.above)
            prev = self._threshold_states.get(sk)        # None on first poll
            now_above = price >= alert.above
            self._threshold_states[sk] = now_above

            # Fire only on the upward edge; skip the very first observation so
            # we don't alert just because the price is already above at startup.
            if now_above and prev is not None and not prev:
                key = f"price_above_{alert.above}"
                if not self._on_cooldown(symbol, key, alert.cooldown_minutes):
                    self._set_cooldown(symbol, key)
                    return AlertEvent(
                        symbol=symbol,
                        alert_type="price_threshold",
                        message=(
                            f"${symbol} crossed ABOVE ${alert.above:.2f} "
                            f"(now ${price:.2f})"
                        ),
                        price=price,
                        timestamp=datetime.now(NYSE_TZ),
                        severity="INFO",
                    )

        if alert.below is not None:
            sk = (symbol, "below", alert.below)
            prev = self._threshold_states.get(sk)
            now_below = price <= alert.below
            self._threshold_states[sk] = now_below

            if now_below and prev is not None and not prev:
                key = f"price_below_{alert.below}"
                if not self._on_cooldown(symbol, key, alert.cooldown_minutes):
                    self._set_cooldown(symbol, key)
                    return AlertEvent(
                        symbol=symbol,
                        alert_type="price_threshold",
                        message=(
                            f"${symbol} crossed BELOW ${alert.below:.2f} "
                            f"(now ${price:.2f})"
                        ),
                        price=price,
                        timestamp=datetime.now(NYSE_TZ),
                        severity="WARNING",
                    )

        return None
