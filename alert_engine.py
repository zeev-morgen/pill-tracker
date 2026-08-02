"""Risk management: ATR volatility exposure and sector diversification alerts.

The heavy lifting is done by pure functions (``compute_atr``,
``build_volatility_report``, ``build_sector_report``) that take plain data,
so the engine is trivial to unit-test without any network access.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import pandas as pd

from market_data import MarketDataError, MarketDataService
from store import PortfolioStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlertThresholds:
    atr_period: int = 14
    # ATR as % of price above which a stock counts as "high volatility"
    high_atr_pct: float = 4.0
    # % of total portfolio value in high-ATR stocks that triggers an alert
    volatility_exposure_pct: float = 30.0
    # % of total portfolio value in a single sector that triggers an alert
    sector_concentration_pct: float = 40.0


def compute_atr(history: pd.DataFrame, period: int = 14) -> Optional[float]:
    """Wilder's Average True Range over the given OHLC history.

    Returns None when there is not enough data for one full period.
    """
    if history is None or len(history) < period + 1:
        return None
    high, low, close = history["High"], history["Low"], history["Close"]
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = true_range.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    value = atr.iloc[-1]
    return None if pd.isna(value) else float(value)


def build_volatility_report(
    positions: List[dict], thresholds: AlertThresholds
) -> dict:
    """positions: [{ticker, market_value, atr_pct}] with atr_pct possibly None."""
    total_value = sum(p["market_value"] for p in positions)
    high_vol = [
        p
        for p in positions
        if p.get("atr_pct") is not None and p["atr_pct"] >= thresholds.high_atr_pct
    ]
    exposure_value = sum(p["market_value"] for p in high_vol)
    exposure_pct = (exposure_value / total_value * 100.0) if total_value > 0 else 0.0
    return {
        "total_value": round(total_value, 2),
        "high_volatility_positions": [
            {
                "ticker": p["ticker"],
                "atr_pct": round(p["atr_pct"], 2),
                "market_value": round(p["market_value"], 2),
            }
            for p in sorted(high_vol, key=lambda p: p["atr_pct"], reverse=True)
        ],
        "exposure_pct": round(exposure_pct, 2),
        "threshold_pct": thresholds.volatility_exposure_pct,
        "alert": exposure_pct >= thresholds.volatility_exposure_pct,
    }


def build_sector_report(positions: List[dict], thresholds: AlertThresholds) -> dict:
    """positions: [{ticker, market_value, sector}]."""
    total_value = sum(p["market_value"] for p in positions)
    by_sector: Dict[str, float] = {}
    for p in positions:
        by_sector[p["sector"]] = by_sector.get(p["sector"], 0.0) + p["market_value"]
    breakdown = [
        {
            "sector": sector,
            "market_value": round(value, 2),
            "weight_pct": round(value / total_value * 100.0, 2) if total_value else 0.0,
        }
        for sector, value in sorted(by_sector.items(), key=lambda kv: kv[1], reverse=True)
    ]
    concentrated = [
        s for s in breakdown if s["weight_pct"] >= thresholds.sector_concentration_pct
    ]
    return {
        "total_value": round(total_value, 2),
        "sectors": breakdown,
        "concentrated_sectors": concentrated,
        "threshold_pct": thresholds.sector_concentration_pct,
        "alert": bool(concentrated),
    }


class AlertEngine:
    """Builds portfolio-level risk reports from live market data."""

    def __init__(
        self,
        store: PortfolioStore,
        market_data: MarketDataService,
        thresholds: AlertThresholds = AlertThresholds(),
    ):
        self._store = store
        self._market = market_data
        self.thresholds = thresholds

    def _collect_positions(self) -> List[dict]:
        positions: List[dict] = []
        for holding in self._store.all():
            try:
                history = self._market.fetch_history(holding.ticker)
            except MarketDataError as exc:
                logger.warning("skipping %s in alerts: %s", holding.ticker, exc)
                continue
            price = float(history["Close"].iloc[-1])
            atr = compute_atr(history, self.thresholds.atr_period)
            fundamentals = self._market.fetch_fundamentals(holding.ticker)
            positions.append(
                {
                    "ticker": holding.ticker,
                    "market_value": price * holding.quantity,
                    "atr": atr,
                    "atr_pct": (atr / price * 100.0) if atr and price > 0 else None,
                    "sector": fundamentals.sector,
                }
            )
        return positions

    def volatility_exposure_report(self) -> dict:
        return build_volatility_report(self._collect_positions(), self.thresholds)

    def sector_diversification_report(self) -> dict:
        return build_sector_report(self._collect_positions(), self.thresholds)
