"""Market data access layer built on yfinance.

Every network call is isolated here so the rest of the system (alerts, AI,
API routes) can be tested with fake data injected in place of this module.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import Dict, Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# Index membership is not exposed by yfinance, so we keep an explicit,
# user-extensible mapping. Tickers not listed fall into "Other".
INDEX_MEMBERSHIP: Dict[str, list] = {
    "AAPL": ["S&P 500", "NASDAQ-100", "Dow Jones"],
    "MSFT": ["S&P 500", "NASDAQ-100", "Dow Jones"],
    "NVDA": ["S&P 500", "NASDAQ-100", "Dow Jones"],
    "GOOGL": ["S&P 500", "NASDAQ-100"],
    "AMZN": ["S&P 500", "NASDAQ-100"],
    "META": ["S&P 500", "NASDAQ-100"],
    "TSLA": ["S&P 500", "NASDAQ-100"],
    "JPM": ["S&P 500", "Dow Jones"],
    "V": ["S&P 500", "Dow Jones"],
    "XOM": ["S&P 500"],
    "CVX": ["S&P 500", "Dow Jones"],
}


class MarketDataError(Exception):
    """Raised when market data for a ticker cannot be fetched."""


@dataclass
class Fundamentals:
    ticker: str
    sector: str = "Unknown"
    industry: str = "Unknown"
    name: str = ""
    indexes: list = field(default_factory=lambda: ["Other"])


class MarketDataService:
    """Fetches quotes, history and fundamentals for tickers."""

    def fetch_history(self, ticker: str, period: str = "6mo") -> pd.DataFrame:
        """Return OHLC history. Raises MarketDataError when empty/unavailable."""
        try:
            df = yf.Ticker(ticker).history(period=period, auto_adjust=True)
        except Exception as exc:  # yfinance raises assorted exception types
            raise MarketDataError(f"history fetch failed for {ticker}: {exc}") from exc
        if df is None or df.empty:
            raise MarketDataError(f"no price history returned for {ticker}")
        required = {"High", "Low", "Close"}
        if not required.issubset(df.columns):
            raise MarketDataError(f"history for {ticker} is missing OHLC columns")
        return df

    def fetch_current_price(self, ticker: str) -> float:
        df = self.fetch_history(ticker, period="5d")
        return float(df["Close"].iloc[-1])

    def price_on(self, ticker: str, on: date) -> Optional[float]:
        """Closing price on (or the first trading day after) a given date."""
        try:
            df = self.fetch_history(ticker, period="max")
        except MarketDataError:
            return None
        idx = df.index.tz_localize(None) if df.index.tz is not None else df.index
        mask = idx.date >= on
        if not mask.any():
            return None
        return float(df.loc[mask, "Close"].iloc[0])

    @lru_cache(maxsize=256)
    def _fetch_fundamentals(self, ticker: str) -> Fundamentals:
        """Sector/industry via yfinance; index membership via local mapping."""
        info: Dict = {}
        try:
            info = yf.Ticker(ticker).info or {}
        except Exception as exc:
            logger.warning("fundamentals fetch failed for %s: %s", ticker, exc)
        return Fundamentals(
            ticker=ticker,
            sector=info.get("sector") or "Unknown",
            industry=info.get("industry") or "Unknown",
            name=info.get("shortName") or ticker,
            indexes=INDEX_MEMBERSHIP.get(ticker, ["Other"]),
        )

    # public alias — keeps the historical private name working for callers
    def fetch_fundamentals(self, ticker: str) -> Fundamentals:
        return self._fetch_fundamentals(ticker)
