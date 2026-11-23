"""Valuation multiples, margins and growth — from whichever endpoint answers.

Why this exists
───────────────
Every analysis was reporting "אין נתון" for P/E, growth and margins. Three
separate causes, all of them in the fetching rather than in Yahoo:

1. **A wrong key.** The PEG ratio was read as ``info["pegRatio"]``. yfinance
   publishes it as ``trailingPegRatio`` — the figure moved to a different Yahoo
   endpoint and the old name is gone — so that one line was *never* going to
   have a value, in any market condition.

2. **One endpoint for everything.** All of it came from ``Ticker.info``, which
   is Yahoo's ``quoteSummary``: the endpoint Yahoo throttles first and hardest.
   This project already says so in three other modules' comments and routes
   around it there; the analysis did not. When quoteSummary answers
   ``YFRateLimitError`` — as it did on this deployment's host — all eight
   figures go missing at once, which is exactly the reported symptom: not one
   stale number, but the whole block empty, every time.

3. **No cache, and a retry that could not work.** The two attempts fired
   back-to-back with no delay, so against a rate limiter the second was as
   doomed as the first, and nothing was kept — so every analysis of every stock
   re-rolled the same dice. Multiples change once a quarter.

So the figures are now assembled field by field from three independent Yahoo
endpoints, newest answer wins, and the result records *which* endpoint supplied
each number:

* ``quote_summary`` — ``Ticker.info``. One request for everything, when it works.
* ``valuation`` — ``Ticker.valuation``, the fundamentals-timeseries API. Carries
  trailing and forward P/E, PEG and market cap, and is a different service from
  quoteSummary, so a throttle there does not reach it.
* ``income_stmt`` — the same timeseries API. Margins and growth computed from
  revenue, gross profit and net income, which is where those ratios come from
  anyway.

Nothing here invents a figure. A missing number stays missing, the caller is
told whether that is because the company has none or because no endpoint
answered, and ``fundamentals_status`` makes the difference answerable in one
request instead of a week of reading analyses.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yfinance as yf

from . import reference_data

logger = logging.getLogger(__name__)

#: Multiples change once a quarter, so a good answer is worth keeping.
CACHE_TTL = 6 * 3600
#: An answer with holes is kept only briefly: it usually means an endpoint was
#: throttled, and that clears in minutes. Caching it for six hours would turn a
#: passing throttle into a day of empty analyses.
CACHE_TTL_PARTIAL = 20 * 60
CACHE_LIMIT = 200

#: The figures the analysis prompt asks for. Order is the prompt's order.
FIELDS = (
    "pe_ratio", "forward_pe", "peg_ratio", "market_cap",
    "revenue_growth", "earnings_growth", "gross_margins", "profit_margins",
    "analyst_rating", "target_price", "fifty_two_high", "fifty_two_low",
    "sector", "short_name",
)

#: Fields that come from a *company's* accounts rather than its price, and so
#: decide whether an answer counts as complete enough to cache for hours.
_CORE = ("pe_ratio", "profit_margins", "revenue_growth", "market_cap")


@dataclass
class Figures:
    """The numbers, plus where each one came from."""

    values: Dict[str, Optional[float]] = field(default_factory=dict)
    #: field name → "quote_summary" | "valuation" | "income_stmt" | "fast_info"
    #: | "derived" | "reference_data"
    sources: Dict[str, str] = field(default_factory=dict)
    #: Which endpoints failed, and how. Kept even on success — a figure that
    #: only ever arrives from the third source is worth seeing.
    errors: List[str] = field(default_factory=list)
    #: "רבעוני" or "שנתי" when growth was computed here, so the prompt can say
    #: which it is instead of implying Yahoo's quarterly-YoY definition.
    growth_basis: str = ""

    def get(self, name: str):
        return self.values.get(name)

    @property
    def any_data(self) -> bool:
        """True when at least one figure arrived from somewhere."""
        return any(
            self.values.get(name) is not None
            for name in FIELDS if name not in ("sector", "short_name")
        )

    @property
    def complete(self) -> bool:
        return all(self.values.get(name) is not None for name in _CORE)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _finite(value) -> Optional[float]:
    """A usable float, or None. NaN is truthy and must not reach a prompt."""
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _row(frame, *names) -> Optional[object]:
    """A statement row by any of its spellings, case- and space-insensitively.

    yfinance renames these rows between versions ("Diluted EPS" against
    "DilutedEPS"), and a KeyError here would cost the whole block.
    """
    if frame is None or getattr(frame, "empty", True):
        return None
    wanted = {str(n).replace(" ", "").casefold() for n in names}
    for label in frame.index:
        key = str(label[0] if isinstance(label, tuple) else label)
        if key.replace(" ", "").casefold() in wanted:
            return frame.loc[label]
    return None


def _cell(series, position: int = 0) -> Optional[float]:
    """One value out of a statement row, by column position (newest first)."""
    if series is None:
        return None
    try:
        values = series.values if hasattr(series, "values") else series
        if len(values) <= position:
            return None
        return _finite(values[position])
    except Exception:                                  # noqa: BLE001
        return None


def _growth(series) -> Optional[float]:
    """Year-on-year change from a statement row, as a fraction.

    Quarterly statements are compared with the same quarter a year earlier —
    four columns back — because a quarter against the one before it measures
    seasonality, not growth.
    """
    for offset in (4, 1):
        latest, earlier = _cell(series, 0), _cell(series, offset)
        if latest is not None and earlier not in (None, 0):
            return (latest - earlier) / abs(earlier)
    return None


# ── Sources ───────────────────────────────────────────────────────────────────

def _from_quote_summary(symbol: str) -> Dict[str, Optional[float]]:
    """``Ticker.info``: everything in one request, when Yahoo is willing."""
    info = yf.Ticker(symbol).info or {}
    if not info:
        raise ValueError("empty payload")
    return {
        "pe_ratio": _finite(info.get("trailingPE")),
        "forward_pe": _finite(info.get("forwardPE")),
        # trailingPegRatio is the live name; pegRatio is kept for older
        # yfinance releases, where it was the only one.
        "peg_ratio": _finite(info.get("trailingPegRatio") or info.get("pegRatio")),
        "market_cap": _finite(info.get("marketCap")),
        "revenue_growth": _finite(info.get("revenueGrowth")),
        "earnings_growth": _finite(info.get("earningsGrowth")),
        "gross_margins": _finite(info.get("grossMargins")),
        "profit_margins": _finite(info.get("profitMargins")),
        "analyst_rating": info.get("recommendationKey") or None,
        "target_price": _finite(info.get("targetMeanPrice")),
        "fifty_two_high": _finite(info.get("fiftyTwoWeekHigh")),
        "fifty_two_low": _finite(info.get("fiftyTwoWeekLow")),
        "sector": info.get("sector") or None,
        "short_name": info.get("shortName") or None,
    }


def _from_valuation(symbol: str) -> Dict[str, Optional[float]]:
    """``Ticker.valuation``: the multiples, from the timeseries API.

    A different Yahoo service from quoteSummary, which is the point — this is
    what still answers when ``.info`` is being throttled.
    """
    frame = yf.Ticker(symbol).get_valuation_measures(periods=0)
    if frame is None or frame.empty or "Current" not in frame.columns:
        raise ValueError("no valuation measures")
    current = frame["Current"]

    def measure(*labels):
        for label in labels:
            for index in current.index:
                if str(index).casefold() == label.casefold():
                    return _finite(current.loc[index])
        return None

    return {
        "pe_ratio": measure("Trailing P/E"),
        "forward_pe": measure("Forward P/E"),
        "peg_ratio": measure("PEG Ratio (5yr expected)", "PEG Ratio"),
        "market_cap": measure("Market Cap"),
    }


def _from_income_statement(symbol: str) -> Dict[str, Optional[float]]:
    """Margins and growth, computed from the accounts they are ratios of.

    Margins come from the trailing-twelve-month statement where Yahoo has one,
    and from the latest annual statement otherwise. Growth always comes from
    the quarterly statement, which is what makes a year-on-year comparison
    possible. Currency cancels in every ratio here, so this path is correct for
    a Tel Aviv listing as it stands.
    """
    ticker = yf.Ticker(symbol)
    out: Dict[str, Optional[float]] = {}

    period = None
    for getter in (lambda: ticker.ttm_income_stmt, lambda: ticker.income_stmt):
        try:
            frame = getter()
        except Exception:                              # noqa: BLE001
            continue
        if frame is not None and not frame.empty:
            period = frame
            break

    if period is not None:
        revenue = _cell(_row(period, "Total Revenue", "TotalRevenue"))
        gross = _cell(_row(period, "Gross Profit", "GrossProfit"))
        net = _cell(_row(period, "Net Income", "NetIncome",
                         "Net Income Common Stockholders"))
        if revenue:
            out["gross_margins"] = gross / revenue if gross is not None else None
            out["profit_margins"] = net / revenue if net is not None else None
        out["_eps"] = _cell(_row(period, "Diluted EPS", "DilutedEPS",
                                 "Basic EPS", "BasicEPS"))

    try:
        quarterly = ticker.quarterly_income_stmt
    except Exception:                                  # noqa: BLE001
        quarterly = None
    if quarterly is not None and not quarterly.empty:
        out["revenue_growth"] = _growth(
            _row(quarterly, "Total Revenue", "TotalRevenue")
        )
        out["earnings_growth"] = _growth(
            _row(quarterly, "Net Income", "NetIncome",
                 "Net Income Common Stockholders")
        )
        out["_growth_basis"] = "רבעוני YoY"
    elif period is not None:
        out["revenue_growth"] = _growth(_row(period, "Total Revenue", "TotalRevenue"))
        out["_growth_basis"] = "שנתי"

    if not any(value is not None for key, value in out.items()
               if not key.startswith("_")):
        raise ValueError("no usable rows")
    return out


def _from_fast_info(symbol: str) -> Dict[str, Optional[float]]:
    """The light quote endpoint: market cap and the 52-week range, no multiples."""
    fast = yf.Ticker(symbol).fast_info

    def read(*names):
        for name in names:
            try:
                value = getattr(fast, name, None)
                if value is None and hasattr(fast, "get"):
                    value = fast.get(name)
                number = _finite(value)
                if number is not None:
                    return number
            except Exception:                          # noqa: BLE001
                continue
        return None

    return {
        "market_cap": read("market_cap", "marketCap"),
        "fifty_two_high": read("year_high", "yearHigh"),
        "fifty_two_low": read("year_low", "yearLow"),
    }


#: Tried in order, and only while something is still missing — so a healthy
#: quoteSummary costs exactly the one request it always did.
SOURCES = (
    ("quote_summary", _from_quote_summary),
    ("valuation", _from_valuation),
    ("income_stmt", _from_income_statement),
    ("fast_info", _from_fast_info),
)


# ── Cache ─────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_cache: Dict[str, tuple] = {}            # symbol → (Figures, stored at, ttl)


def reset_cache() -> None:
    with _lock:
        _cache.clear()


def _cached(symbol: str) -> Optional[Figures]:
    with _lock:
        entry = _cache.get(symbol)
        if entry is None:
            return None
        figures, stored_at, ttl = entry
        if time.monotonic() - stored_at > ttl:
            del _cache[symbol]
            return None
        return figures


def _store(symbol: str, figures: Figures) -> None:
    ttl = CACHE_TTL if figures.complete else CACHE_TTL_PARTIAL
    with _lock:
        if len(_cache) >= CACHE_LIMIT:
            for stale in sorted(_cache, key=lambda k: _cache[k][1])[: CACHE_LIMIT // 4]:
                del _cache[stale]
        _cache[symbol] = (figures, time.monotonic(), ttl)


# ── Public API ────────────────────────────────────────────────────────────────

def fetch(symbol: str, price: Optional[float] = None,
          use_cache: bool = True) -> Figures:
    """Every figure the analysis needs, from as many endpoints as it takes.

    Blocking — keep it off the event loop. ``price`` lets a trailing P/E be
    derived from earnings per share when no endpoint will quote one.
    """
    symbol = str(symbol or "").upper()
    if use_cache:
        hit = _cached(symbol)
        if hit is not None:
            return hit

    figures = Figures(values={name: None for name in FIELDS})
    extras: Dict[str, object] = {}

    for name, source in SOURCES:
        missing = [f for f in FIELDS if figures.values.get(f) is None]
        # "sector" and "short_name" alone are not worth another request: the
        # curated table covers the first and the ticker covers the second.
        if not [f for f in missing if f not in ("sector", "short_name")]:
            break
        try:
            answer = source(symbol) or {}
        except Exception as exc:                       # noqa: BLE001 — any may fail
            figures.errors.append(f"{name}: {exc.__class__.__name__}")
            logger.debug("Fundamentals source %s failed for %s: %s", name, symbol, exc)
            continue

        for key, value in answer.items():
            if key.startswith("_"):
                extras.setdefault(key, value)
                continue
            if value is not None and figures.values.get(key) is None:
                figures.values[key] = value
                figures.sources[key] = name

    # A trailing P/E nobody would quote, from the earnings that define it.
    # Skipped for Tel Aviv listings: the quote is in agorot and the statements
    # are not, and a hundredfold P/E is worse than a missing one.
    eps = _finite(extras.get("_eps"))
    if (figures.values.get("pe_ratio") is None and eps and eps > 0
            and price and not symbol.endswith(".TA")):
        figures.values["pe_ratio"] = price / eps
        figures.sources["pe_ratio"] = "derived"

    if figures.values.get("revenue_growth") is not None:
        figures.growth_basis = str(extras.get("_growth_basis") or "")

    if not figures.values.get("sector"):
        figures.values["sector"] = reference_data.lookup_sector(symbol)
        figures.sources["sector"] = "reference_data"
    if not figures.values.get("short_name"):
        figures.values["short_name"] = symbol

    if figures.errors:
        logger.info(
            "Fundamentals for %s assembled from %s (failures: %s)",
            symbol,
            ", ".join(sorted(set(figures.sources.values()))) or "nothing",
            "; ".join(figures.errors),
        )
    if use_cache:
        _store(symbol, figures)
    return figures


def status(symbol: str = "AAPL") -> dict:
    """Which endpoint is answering for multiples on this host, right now.

    The probe ticker has a P/E, margins and growth on any day the market
    exists, so empty figures here mean the fetching is broken rather than the
    company being unusual — the same trick ``news.status`` plays.
    """
    figures = fetch(symbol, use_cache=False)
    return {
        "probe_ticker": symbol,
        "sources": figures.sources,
        "errors": figures.errors,
        "healthy": figures.complete,
        "values": {
            name: figures.values.get(name)
            for name in ("pe_ratio", "forward_pe", "peg_ratio", "market_cap",
                         "revenue_growth", "profit_margins", "gross_margins")
        },
    }
