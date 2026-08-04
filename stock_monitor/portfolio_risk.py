"""
Portfolio risk analytics: ATR volatility exposure, sector concentration and
allocation breakdowns.

The maths lives in pure functions that take plain lists of dicts, so the whole
module is unit-testable without network access. ``PortfolioRiskAnalyzer`` is the
thin layer that fetches live data and feeds those functions.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import Dict, List, Optional

import pandas as pd
import yfinance as yf

from . import reference_data
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
    #: 'etf' for index funds / ETFs, 'stock' otherwise.
    asset_type: str = "stock"


UNKNOWN_SECTOR = "Unknown"


def _finite(value):
    """None for anything JSON cannot carry.

    ``json.dumps(allow_nan=False)`` — which is what FastAPI's JSONResponse
    uses — raises on NaN and infinity, so one bad number from yfinance would
    otherwise fail the entire endpoint rather than blank a single field.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if math.isfinite(value) else None
    return value


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
    """Percentage weights by sector, by index membership, and by asset type.

    A stock belonging to several indexes contributes to each of them; those
    weights are normalized per bucket so the pie still sums to 100%.

    ``by_asset_type`` answers a different question and is therefore computed
    against the portfolio total, not normalized per bucket: how much is held
    through index funds / ETFs versus picked as individual stocks.
    """
    by_sector: Dict[str, float] = {}
    by_index: Dict[str, float] = {}
    etf_value = 0.0
    total = sum(p["market_value"] for p in positions)

    for p in positions:
        by_sector[p["sector"]] = by_sector.get(p["sector"], 0.0) + p["market_value"]
        for index_name in p.get("indexes") or ["Other"]:
            by_index[index_name] = by_index.get(index_name, 0.0) + p["market_value"]
        if p.get("asset_type") == "etf":
            etf_value += p["market_value"]

    def to_weights(bucket: Dict[str, float]) -> List[dict]:
        bucket_total = sum(bucket.values())
        if bucket_total <= 0:
            return []
        return [
            {"label": label, "weight_pct": round(value / bucket_total * 100.0, 2)}
            for label, value in sorted(bucket.items(), key=lambda kv: kv[1], reverse=True)
        ]

    stock_value = total - etf_value
    return {
        "total_value": round(total, 2),
        "by_sector": to_weights(by_sector),
        "by_index": to_weights(by_index),
        "by_asset_type": {
            "etf_value": round(etf_value, 2),
            "stock_value": round(stock_value, 2),
            "etf_pct": round(etf_value / total * 100.0, 2) if total > 0 else 0.0,
            "stock_pct": round(stock_value / total * 100.0, 2) if total > 0 else 0.0,
        },
    }


# ── Live data layer ───────────────────────────────────────────────────────────

@lru_cache(maxsize=256)
def fetch_fundamentals(ticker: str) -> Fundamentals:
    """Sector, name and asset type for a ticker.

    Sector resolution order — yfinance first because it is authoritative and
    current, then the curated table, then Unknown. yfinance's ``.info``
    frequently comes back empty (throttling, upstream changes), so without the
    fallback most holdings ended up unclassified and the sector breakdown was
    one meaningless slice. A manual override on the holding still beats both;
    see ``collect_positions``.
    """
    info: dict = {}
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as exc:
        logger.warning("fundamentals fetch failed for %s: %s", ticker, exc)

    curated_sector = reference_data.lookup_sector(ticker)
    sector = info.get("sector") or curated_sector or UNKNOWN_SECTOR
    if not info.get("sector") and curated_sector:
        logger.debug("Sector for %s resolved from the curated table", ticker)

    quote_type = str(info.get("quoteType") or "").upper()
    if quote_type:
        asset_type = "etf" if quote_type in {"ETF", "MUTUALFUND", "INDEX"} else "stock"
    else:
        asset_type = "etf" if reference_data.is_known_etf(ticker) else "stock"

    return Fundamentals(
        ticker=ticker,
        sector=sector,
        name=info.get("shortName") or ticker,
        indexes=INDEX_MEMBERSHIP.get(ticker, ["Other"]),
        asset_type=asset_type,
    )


class PortfolioRiskAnalyzer:
    """Builds portfolio-level risk and allocation reports from live prices."""

    def __init__(
        self,
        store: PortfolioStore,
        thresholds: RiskThresholds = RiskThresholds(),
        data_feed=None,
    ) -> None:
        self._store = store
        self.thresholds = thresholds
        # Optional: supplies extended-hours quotes. Injected by the app so the
        # running StockDataFeed is reused rather than a second one created.
        self._feed = data_feed

    def set_data_feed(self, feed) -> None:
        """Attach the feed after construction (the dashboard's analyzer is a
        module-level singleton built before the app owns a feed)."""
        self._feed = feed

    def _extended_hours(self, ticker: str, session: str) -> dict:
        """Pre/post-market price and move, when the feed can supply them.

        The caller passes the session, which is pure clock arithmetic, so
        outside pre/after hours this costs nothing. It used to query the feed
        per holding and then throw the answer away once it saw the session was
        'regular' or 'closed' — a wasted request per position, on every refresh.

        Returns empty rather than raising: extended-hours quotes are a display
        extra, and losing them must not cost the whole portfolio report.
        """
        if self._feed is None or session not in ("pre", "after"):
            return {"session": session} if session else {}
        try:
            data = self._feed.get_current_data(ticker) or {}
        except Exception as exc:
            logger.debug("extended-hours fetch failed for %s: %s", ticker, exc)
            return {"session": session}
        return {
            "session": data.get("session", session),
            "extended_price": _finite(data.get("price")),
            "extended_change_pct": _finite(data.get("since_close_pct")),
        }

    def _fetch_histories(self, tickers: List[str]) -> Dict[str, "pd.DataFrame"]:
        """Price history for the whole portfolio in a single request.

        Yahoo rate-limits per IP, and a shared cloud host burns that budget
        fast: one request per holding on every refresh was enough to get every
        ticker throttled at once. Tickers the batch does not cover fall back to
        an individual fetch, so a partial answer still yields most positions.
        """
        if not tickers:
            return {}
        try:
            data = yf.download(
                tickers,
                period="6mo",
                auto_adjust=True,
                progress=False,
                group_by="ticker",
                threads=False,
            )
        except Exception as exc:
            logger.warning(
                "batch history download failed (%s: %s) — falling back to per-ticker",
                exc.__class__.__name__, exc,
            )
            return {}

        histories: Dict[str, "pd.DataFrame"] = {}
        if data is None or data.empty:
            return histories
        if isinstance(data.columns, pd.MultiIndex):
            available = set(data.columns.get_level_values(0))
            for ticker in tickers:
                if ticker not in available:
                    continue
                frame = data[ticker].dropna(how="all")
                if not frame.empty:
                    histories[ticker] = frame
        elif len(tickers) == 1:
            frame = data.dropna(how="all")
            if not frame.empty:
                histories[tickers[0]] = frame
        return histories

    def collect_positions(self) -> List[dict]:
        """One entry per holding, enriched with price, ATR, sector and P/L.

        Holdings that cannot be priced are skipped rather than failing the whole
        report — see ``skipped_tickers`` for what was left out. The entire body
        is guarded, not just the fetch: an unexpected shape from yfinance (a
        frame without a Close column, a multi-index) used to raise past the
        handler and take down the endpoint.
        """
        from .data_feed import get_market_session

        holdings = self._store.all()
        positions: List[dict] = []
        self._skipped: List[str] = []
        self._skip_reasons: List[str] = []
        # One clock read for the whole report rather than one per holding.
        session = get_market_session()
        histories = self._fetch_histories([h.ticker for h in holdings])

        for holding in holdings:
            try:
                history = histories.get(holding.ticker)
                if history is None:
                    history = yf.Ticker(holding.ticker).history(
                        period="6mo", auto_adjust=True
                    )
                if history is None or history.empty or "Close" not in history.columns:
                    logger.warning("no usable price history for %s — skipping", holding.ticker)
                    self._skipped.append(holding.ticker)
                    self._skip_reasons.append("no price data")
                    continue

                # Trailing rows can carry a NaN close. A batch download indexes
                # every ticker against the union of all their trading days, so
                # one that has not printed yet today gets an empty bar — and
                # dropna(how="all") keeps it, because Volume is 0 rather than
                # NaN. Take the last close that exists instead of the last row,
                # which also keeps those blank bars out of the ATR window.
                history = history[history["Close"].notna()]
                if history.empty:
                    logger.warning("no priced bars for %s — skipping", holding.ticker)
                    self._skipped.append(holding.ticker)
                    self._skip_reasons.append("no price data")
                    continue

                price = float(history["Close"].iloc[-1])
                # A non-finite price poisons every total it feeds, and NaN is
                # rejected outright by the JSON encoder — so the position is
                # dropped rather than allowed to fail the whole response.
                if not math.isfinite(price) or price <= 0:
                    logger.warning("unusable price (%s) for %s — skipping", price, holding.ticker)
                    self._skipped.append(holding.ticker)
                    self._skip_reasons.append("unusable price")
                    continue

                atr = compute_atr(history, self.thresholds.atr_period)
                fundamentals = fetch_fundamentals(holding.ticker)
                market_value = price * holding.quantity
                # A user-set sector or asset type always wins: yfinance regularly
                # reports nothing, and the manual value is the whole point of the
                # override.
                sector = holding.sector or fundamentals.sector
                asset_type = holding.asset_type or fundamentals.asset_type
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
                        "atr_pct": (atr / price * 100.0) if atr else None,
                        "sector": sector,
                        "sector_is_manual": holding.sector is not None,
                        "asset_type": asset_type,
                        "asset_type_is_manual": holding.asset_type is not None,
                        "indexes": fundamentals.indexes,
                        "purchase_date": holding.purchase_date,
                        **self._extended_hours(holding.ticker, session),
                    }
                )
            except Exception as exc:
                logger.warning(
                    "skipping %s: %s: %s", holding.ticker, exc.__class__.__name__, exc
                )
                self._skipped.append(holding.ticker)
                self._skip_reasons.append(exc.__class__.__name__)
        return positions

    @property
    def skipped_tickers(self) -> List[str]:
        """Holdings dropped by the most recent ``collect_positions`` call."""
        return list(getattr(self, "_skipped", []))

    @property
    def skip_reason(self) -> Optional[str]:
        """The most common reason holdings were dropped, for the UI to show.

        When every position disappears the cause is systemic — throttling, a
        network block — and naming it is the difference between a mystery and
        something the user can act on.
        """
        reasons = getattr(self, "_skip_reasons", [])
        return max(set(reasons), key=reasons.count) if reasons else None

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
                    "sector_is_manual": p["sector_is_manual"],
                    "asset_type": p["asset_type"],
                    "asset_type_is_manual": p["asset_type_is_manual"],
                    "purchase_date": (
                        p["purchase_date"].isoformat() if p.get("purchase_date") else None
                    ),
                    "holding_days": (
                        (date.today() - p["purchase_date"]).days
                        if p.get("purchase_date") else None
                    ),
                    "session": p.get("session"),
                    "extended_price": (
                        round(p["extended_price"], 2)
                        if p.get("extended_price") is not None else None
                    ),
                    "extended_change_pct": (
                        round(p["extended_change_pct"], 2)
                        if p.get("extended_change_pct") is not None else None
                    ),
                }
                for p in positions
            ],
            "total_value": round(sum(p["market_value"] for p in positions), 2),
            "total_pnl_value": round(sum(p["pnl_value"] for p in positions), 2),
            # Held but unpriceable. Without this a position simply vanished from
            # the table with no hint that it was ever there.
            "skipped_tickers": self.skipped_tickers,
            "skip_reason": self.skip_reason,
            "volatility": build_volatility_report(positions, self.thresholds),
            "sector": build_sector_report(positions, self.thresholds),
            "allocation": build_allocation(positions),
        }
