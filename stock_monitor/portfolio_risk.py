"""
Portfolio risk analytics: ATR volatility exposure, sector concentration and
allocation breakdowns.

The maths lives in pure functions that take plain lists of dicts, so the whole
module is unit-testable without network access. ``PortfolioRiskAnalyzer`` is the
thin layer that fetches live data and feeds those functions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional

import pandas as pd
import yfinance as yf

from .store import PortfolioStore

logger = logging.getLogger(__name__)

# yfinance does not expose index membership, so it comes from this local,
# user-extensible mapping. Tickers not listed here fall into "Other".
INDEX_MEMBERSHIP: Dict[str, List[str]] = {
    "AAPL": ["S&P 500", "NASDAQ-100", "Dow Jones"],
    "MSFT": ["S&P 500", "NASDAQ-100", "Dow Jones"],
    "NVDA": ["S&P 500", "NASDAQ-100", "Dow Jones"],
    "GOOGL": ["S&P 500", "NASDAQ-100"],
    "AMZN": ["S&P 500", "NASDAQ-100"],
    "META": ["S&P 500", "NASDAQ-100"],
    "TSLA": ["S&P 500", "NASDAQ-100"],
    "ORCL": ["S&P 500", "NASDAQ-100"],
    "MU": ["S&P 500", "NASDAQ-100"],
    "CF": ["S&P 500"],
    "SEDG": ["S&P 400"],
    "ABCL": ["Russell 2000"],
    "FULC": ["Russell 2000"],
    "SNDK": ["S&P 500"],
}


@dataclass(frozen=True)
class RiskThresholds:
    atr_period: int = 14
    # ATR as % of price above which a stock counts as "high volatility"
    high_atr_pct: float = 4.0
    # % of total portfolio value in high-ATR stocks that triggers an alert
    volatility_exposure_pct: float = 30.0
    # % of total portfolio value in a single sector that triggers an alert
    sector_concentration_pct: float = 40.0


@dataclass
class Fundamentals:
    ticker: str
    sector: str = "Unknown"
    name: str = ""
    indexes: List[str] = field(default_factory=lambda: ["Other"])


# ── Pure analytics ────────────────────────────────────────────────────────────

def compute_atr(history: pd.DataFrame, period: int = 14) -> Optional[float]:
    """Wilder's Average True Range over OHLC history.

    Returns None when there is not enough data for one full period.
    """
    if history is None or len(history) < period + 1:
        return None
    if not {"High", "Low", "Close"}.issubset(history.columns):
        return None
    high, low, close = history["High"], history["Low"], history["Close"]
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = true_range.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    value = atr.iloc[-1]
    return None if pd.isna(value) else float(value)


def build_volatility_report(positions: List[dict], thresholds: RiskThresholds) -> dict:
    """positions: [{ticker, market_value, atr_pct}] — atr_pct may be None."""
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


def build_sector_report(positions: List[dict], thresholds: RiskThresholds) -> dict:
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


def build_allocation(positions: List[dict]) -> dict:
    """Percentage weights by sector and by index membership.

    A stock belonging to several indexes contributes to each of them; weights
    are normalized per bucket so every pie still sums to 100%.
    """
    by_sector: Dict[str, float] = {}
    by_index: Dict[str, float] = {}
    for p in positions:
        by_sector[p["sector"]] = by_sector.get(p["sector"], 0.0) + p["market_value"]
        for index_name in p.get("indexes") or ["Other"]:
            by_index[index_name] = by_index.get(index_name, 0.0) + p["market_value"]

    def to_weights(bucket: Dict[str, float]) -> List[dict]:
        bucket_total = sum(bucket.values())
        if bucket_total <= 0:
            return []
        return [
            {"label": label, "weight_pct": round(value / bucket_total * 100.0, 2)}
            for label, value in sorted(bucket.items(), key=lambda kv: kv[1], reverse=True)
        ]

    return {
        "total_value": round(sum(p["market_value"] for p in positions), 2),
        "by_sector": to_weights(by_sector),
        "by_index": to_weights(by_index),
    }


# ── Live data layer ───────────────────────────────────────────────────────────

@lru_cache(maxsize=256)
def fetch_fundamentals(ticker: str) -> Fundamentals:
    """Sector/name via yfinance; index membership via the local mapping."""
    info: dict = {}
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as exc:
        logger.warning("fundamentals fetch failed for %s: %s", ticker, exc)
    return Fundamentals(
        ticker=ticker,
        sector=info.get("sector") or "Unknown",
        name=info.get("shortName") or ticker,
        indexes=INDEX_MEMBERSHIP.get(ticker, ["Other"]),
    )


class PortfolioRiskAnalyzer:
    """Builds portfolio-level risk and allocation reports from live prices."""

    def __init__(
        self,
        store: PortfolioStore,
        thresholds: RiskThresholds = RiskThresholds(),
    ) -> None:
        self._store = store
        self.thresholds = thresholds

    def collect_positions(self) -> List[dict]:
        """One entry per holding, enriched with price, ATR, sector and P/L.

        Holdings whose price cannot be fetched are skipped rather than failing
        the whole report.
        """
        positions: List[dict] = []
        for holding in self._store.all():
            try:
                history = yf.Ticker(holding.ticker).history(period="6mo", auto_adjust=True)
            except Exception as exc:
                logger.warning("history fetch failed for %s: %s", holding.ticker, exc)
                continue
            if history is None or history.empty:
                logger.warning("no price history for %s — skipping", holding.ticker)
                continue

            price = float(history["Close"].iloc[-1])
            atr = compute_atr(history, self.thresholds.atr_period)
            fundamentals = fetch_fundamentals(holding.ticker)
            market_value = price * holding.quantity
            positions.append(
                {
                    "ticker": holding.ticker,
                    "quantity": holding.quantity,
                    "entry_price": holding.entry_price,
                    "current_price": price,
                    "market_value": market_value,
                    "pnl_pct": (price - holding.entry_price) / holding.entry_price * 100.0,
                    "pnl_value": (price - holding.entry_price) * holding.quantity,
                    "atr": atr,
                    "atr_pct": (atr / price * 100.0) if atr and price > 0 else None,
                    "sector": fundamentals.sector,
                    "indexes": fundamentals.indexes,
                }
            )
        return positions

    def full_report(self) -> dict:
        """Positions plus both alert reports and the allocation breakdown."""
        positions = self.collect_positions()
        return {
            "positions": [
                {
                    "ticker": p["ticker"],
                    "quantity": p["quantity"],
                    "entry_price": round(p["entry_price"], 2),
                    "current_price": round(p["current_price"], 2),
                    "market_value": round(p["market_value"], 2),
                    "pnl_pct": round(p["pnl_pct"], 2),
                    "pnl_value": round(p["pnl_value"], 2),
                    "atr_pct": round(p["atr_pct"], 2) if p["atr_pct"] is not None else None,
                    "sector": p["sector"],
                }
                for p in positions
            ],
            "total_value": round(sum(p["market_value"] for p in positions), 2),
            "total_pnl_value": round(sum(p["pnl_value"] for p in positions), 2),
            "volatility": build_volatility_report(positions, self.thresholds),
            "sector": build_sector_report(positions, self.thresholds),
            "allocation": build_allocation(positions),
        }
