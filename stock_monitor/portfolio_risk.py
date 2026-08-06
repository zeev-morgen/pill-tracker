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
from datetime import date, datetime
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


def _market_today() -> date:
    """Today in exchange time — the server clock may be on another date."""
    from .data_feed import NYSE_TZ

    return datetime.now(NYSE_TZ).date()


def _bar_date(index_value) -> Optional[date]:
    """Session date of a price bar, as the exchange saw it.

    yfinance hands back a tz-aware timestamp for daily bars; converting to UTC
    first would roll a 00:00 New York bar back into the previous day.
    """
    try:
        return index_value.date()
    except AttributeError:
        return None


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

    def _intraday_quotes(self, tickers: List[str], session: str) -> Dict[str, dict]:
        """Current price and last regular close for every ticker, in one request.

        The 5-minute series already spans pre, regular and post bars, so a
        single batch answers what previously took a quote plus a history call
        per holding. That was 26 requests for thirteen positions on every
        refresh — above the rate limit that got the whole portfolio throttled.

        Returns {ticker: {price, regular_close}} for whatever it could resolve;
        callers fall back per ticker for the rest.
        """
        from .data_feed import StockDataFeed

        if not tickers:
            return {}
        frames = self._fetch_histories(
            tickers, period="5d", interval="5m", prepost=True
        )
        quotes: Dict[str, dict] = {}
        for ticker, frame in frames.items():
            try:
                priced = frame[frame["Close"].notna()]
                if priced.empty:
                    continue
                price = float(priced["Close"].iloc[-1])
                if not math.isfinite(price) or price <= 0:
                    continue
                closes = StockDataFeed._regular_session_closes(priced)
                quotes[ticker] = {
                    "price": price,
                    "regular_close": closes[-1] if closes else None,
                    "session": session,
                    # When the last print actually happened. Thinly traded
                    # names have no pre-market bar for hours after 04:00, so
                    # the newest bar can be yesterday's after-hours — a real
                    # price, but not a live one.
                    "as_of": _bar_date(priced.index[-1]),
                }
            except Exception as exc:
                logger.debug("intraday parse failed for %s: %s", ticker, exc)
        return quotes

    def _quote(self, ticker: str) -> dict:
        """Live quote for one ticker, or empty if it cannot be had.

        This is Yahoo's quote endpoint, which is a different service from the
        daily-bar chart endpoint and is frequently ahead of it: the chart API
        can publish a session's row hours before it fills in the close, while
        the quote already has the real number.

        Never raises — a quote is an enrichment, and losing it must not cost
        the whole portfolio report.
        """
        if self._feed is None:
            return {}
        try:
            return self._feed.get_current_data(ticker) or {}
        except Exception as exc:
            logger.debug("quote fetch failed for %s: %s", ticker, exc)
            return {}

    @staticmethod
    def _extended_fields(quote: dict, session: str) -> dict:
        """Pre/post-market columns, from a quote already fetched."""
        if session not in ("pre", "after") or not quote:
            return {"session": session} if session else {}
        change = quote.get("since_close_pct")
        if change is None:
            # The batched quote carries the regular close rather than a
            # precomputed move, so derive it here.
            close = _finite(quote.get("regular_close"))
            price = _finite(quote.get("price"))
            if close and price:
                change = (price - close) / close * 100.0
        return {
            "session": quote.get("session", session),
            "extended_price": _finite(quote.get("price")),
            "extended_change_pct": _finite(change),
        }

    def _fetch_histories(
        self, tickers: List[str], period: str = "6mo",
        interval: str = "1d", prepost: bool = False,
    ) -> Dict[str, "pd.DataFrame"]:
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
                period=period,
                interval=interval,
                prepost=prepost,
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
                frame = data[ticker]
                # Emptiness is tested on a cleaned copy, but the frame handed
                # back keeps its raw tail: the caller needs to see whether the
                # newest bar has no close, and dropna would sometimes remove
                # that evidence and sometimes not — a blank bar survives it
                # when Volume comes back as 0 rather than NaN.
                if not frame.dropna(how="all").empty:
                    histories[ticker] = frame
        elif len(tickers) == 1:
            if not data.dropna(how="all").empty:
                histories[tickers[0]] = data
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
        tickers = [h.ticker for h in holdings]
        histories = self._fetch_histories(tickers)
        # A daily close cannot be the current price while a session is running,
        # so the intraday batch is fetched then — and also when the daily bars
        # are behind, which is what the chart endpoint does most mornings.
        bars_behind = any(
            frame is not None and not frame.empty and "Close" in frame.columns
            and pd.isna(frame["Close"].iloc[-1])
            for frame in histories.values()
        )
        intraday = (
            self._intraday_quotes(tickers, session)
            if session in ("pre", "regular", "after") or bars_behind
            else {}
        )

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
                # Whether the newest row Yahoo returned had no close. That is
                # the signal that the chart endpoint is behind the market.
                bars_lagging = bool(pd.isna(history["Close"].iloc[-1]))
                history = history[history["Close"].notna()]
                if history.empty:
                    logger.warning("no priced bars for %s — skipping", holding.ticker)
                    self._skipped.append(holding.ticker)
                    self._skip_reasons.append("no price data")
                    continue

                price = float(history["Close"].iloc[-1])
                price_date = _bar_date(history.index[-1])
                price_source = "bar"
                # A non-finite price poisons every total it feeds, and NaN is
                # rejected outright by the JSON encoder — so the position is
                # dropped rather than allowed to fail the whole response.
                if not math.isfinite(price) or price <= 0:
                    logger.warning("unusable price (%s) for %s — skipping", price, holding.ticker)
                    self._skipped.append(holding.ticker)
                    self._skip_reasons.append("unusable price")
                    continue

                # Ask the quote endpoint when the bars cannot be the current
                # price: either they are missing the newest session, or a
                # session is under way and the last daily close is behind it.
                # When the bars are current and the market is shut, the close
                # *is* the price and the request is skipped — which is what
                # keeps the common case at one request for the whole portfolio.
                quote = intraday.get(holding.ticker) or (
                    self._quote(holding.ticker)
                    if bars_lagging or session in ("pre", "regular", "after")
                    else {}
                )
                quote_price = _finite(quote.get("price"))
                if quote_price and quote_price > 0:
                    price = float(quote_price)
                    price_source = "quote"
                    # A quote counts as live only if its bar is from today.
                    # Otherwise it is the last print from an earlier session and
                    # keeps that date, or the dashboard shows an unchanging
                    # number labelled "live" and it reads as a frozen portfolio.
                    as_of = quote.get("as_of")
                    price_date = None if as_of in (None, _market_today()) else as_of

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
                        "price_date": price_date,
                        "price_source": price_source,
                        "purchase_date": holding.purchase_date,
                        **self._extended_fields(quote, session),
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
        # The freshest bar anyone in the portfolio has is the best read on
        # "now" without needing an exchange calendar; anything behind it is
        # quoting an older session.
        bar_dates = [p["price_date"] for p in positions if p.get("price_date")]
        latest_bar = max(bar_dates) if bar_dates else None
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
                    # Which session the quoted close came from, so a stale
                    # price is visible instead of passing for today's.
                    "price_date": (
                        p["price_date"].isoformat() if p.get("price_date") else None
                    ),
                    "price_source": p.get("price_source", "bar"),
                    "price_is_stale": (
                        p.get("price_date") is not None
                        and latest_bar is not None
                        and p["price_date"] < latest_bar
                    ),
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
            "latest_bar_date": latest_bar.isoformat() if latest_bar else None,
            "volatility": build_volatility_report(positions, self.thresholds),
            "sector": build_sector_report(positions, self.thresholds),
            "allocation": build_allocation(positions),
        }
