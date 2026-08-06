"""Live quotes from Tiingo's IEX feed, used when Yahoo will not serve them.

Yahoo rate-limits the deployed host: its quote-summary endpoint answers 429
outright, and its chart endpoint answers 200 with the *previous* session's
bars, which is worse — a degraded response that looks like a healthy one. Our
own request volume was cut twenty-three-fold without effect, which points at
the shared outbound IP rather than at anything this app does, so there is no
politeness fix. A second source is the only way out.

Tiingo is that source for *current* prices only. Yahoo still supplies history,
ATR and fundamentals; those are daily bars and they arrive fine. This module
covers the one thing that is broken.

Nothing here may raise into a caller. A quote is an enrichment: if Tiingo is
down, misconfigured or slow, the portfolio must still render off Yahoo's bars.
Every entry point returns empty rather than propagating.
"""

from __future__ import annotations

import logging
import math
import os
import re
from datetime import date, datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

#: Batch quote endpoint. One request covers every ticker in the portfolio.
IEX_URL = "https://api.tiingo.com/iex/"

#: Seconds. Generous enough for a cold connection, short enough that a hung
#: provider cannot hold the portfolio request open.
TIMEOUT = 10

ENV_KEY = "TIINGO_API_KEY"


def api_key() -> str:
    return os.environ.get(ENV_KEY, "").strip()


def is_configured() -> bool:
    return bool(api_key())


def _finite(value) -> Optional[float]:
    """A usable number, or None. NaN is truthy and compares unequal to zero."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


#: Tiingo stamps quotes with nanosecond precision. datetime.fromisoformat
#: accepts 3 or 6 fractional digits and rejects 9, so the tail is trimmed
#: before parsing rather than the whole timestamp being thrown away.
_NANOS = re.compile(r"(\.\d{6})\d+")


def parse_timestamp(text) -> Optional[datetime]:
    """Tiingo's ISO-8601 stamp as an aware datetime, or None."""
    if not isinstance(text, str) or not text:
        return None
    cleaned = _NANOS.sub(r"\1", text.strip())
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        logger.debug("unparseable Tiingo timestamp: %r", text)
        return None
    return parsed if parsed.tzinfo else None


def _market_date(stamp: Optional[datetime]) -> Optional[date]:
    """The exchange-local date of a quote, not the server's date."""
    if stamp is None:
        return None
    from .data_feed import NYSE_TZ

    return stamp.astimezone(NYSE_TZ).date()


def _reference_close(row: dict, session: str) -> Optional[float]:
    """The close an extended-hours move should be measured against.

    Before the open that is the previous session's close, which is exactly what
    Tiingo's ``prevClose`` is. After the close it is *today's* regular close,
    which this endpoint does not carry — ``prevClose`` is the day before, and
    using it would report the post-market move as a full day's move plus the
    session's. Better to report no percentage than a wrong one.
    """
    return _finite(row.get("prevClose")) if session == "pre" else None


def _get(tickers: List[str]):
    """The HTTP call, isolated so tests can replace it without a network."""
    import requests

    return requests.get(
        IEX_URL,
        params={"tickers": ",".join(tickers)},
        # The key travels in a header, not the query string: URLs end up in
        # access logs and error reports, and this one is a credential.
        headers={"Authorization": f"Token {api_key()}",
                 "Content-Type": "application/json"},
        timeout=TIMEOUT,
    )


def get_quotes(tickers: List[str]) -> Dict[str, dict]:
    """Current prices for many tickers in one request.

    Returns the same shape ``PortfolioRiskAnalyzer._intraday_quotes`` produces —
    ``{price, regular_close, session, as_of}`` — so the two are interchangeable
    at the call site and neither has to know which source it came from.

    An unpriced ticker is omitted rather than returned with None, because the
    caller treats presence as "there is a live price here".
    """
    if not tickers or not is_configured():
        return {}

    try:
        response = _get(sorted({t for t in tickers if t}))
        status = getattr(response, "status_code", None)
        if status != 200:
            # 401 means the key is wrong and every later call will fail the
            # same way, so say which it is instead of logging a bare number.
            logger.warning(
                "Tiingo returned %s%s", status,
                " — check TIINGO_API_KEY" if status in (401, 403) else "",
            )
            return {}
        payload = response.json()
    except Exception as exc:
        logger.warning("Tiingo quote fetch failed: %s", exc.__class__.__name__)
        return {}

    if not isinstance(payload, list):
        logger.warning("Tiingo returned %s, expected a list", type(payload).__name__)
        return {}

    from .data_feed import get_market_session

    session = get_market_session()
    quotes: Dict[str, dict] = {}
    for row in payload:
        try:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").upper()
            # tngoLast is Tiingo's own consolidated print; `last` is IEX's.
            # Prefer the former and fall back, since outside of regular hours
            # one of the two is routinely null.
            price = _finite(row.get("tngoLast")) or _finite(row.get("last"))
            if not ticker or not price or price <= 0:
                continue
            stamp = parse_timestamp(row.get("timestamp"))
            quotes[ticker] = {
                "price": price,
                "regular_close": _reference_close(row, session),
                "session": session,
                # Exchange-local date of the last print. The caller uses this
                # to decide whether the quote is today's or a leftover, so a
                # missing timestamp must not silently pass for today.
                "as_of": _market_date(stamp),
                # The untouched row, so get_monitor_quotes can build its richer
                # payload from this same response instead of fetching twice.
                # Callers read named keys, so the extra one costs them nothing.
                "_row": row,
            }
        except Exception as exc:
            logger.debug("Tiingo row parse failed: %s", exc)
    return quotes


def get_monitor_quotes(symbols: List[str]) -> Dict[str, dict]:
    """Live-tab and alert-engine payloads, in ``_from_intraday``'s shape.

    Separate from ``get_quotes`` because the monitor needs more than a price:
    the day's open, high and low, and the change percentages computed off them.

    Volume is deliberately reported as 0 rather than passed through. Tiingo's
    IEX feed carries IEX's own volume, a small single-digit percentage of
    consolidated tape volume, while the spike alert compares it against a
    10-day average built from Yahoo's consolidated figures. Handing those two
    numbers to the same comparison would make every session look like a
    collapse in volume. The alert engine skips a symbol whose volume is 0, so
    reporting nothing leaves the alert silent instead of wrong.
    """
    if not symbols or not is_configured():
        return {}

    from .data_feed import NYSE_TZ, get_market_session

    session = get_market_session()
    today = datetime.now(NYSE_TZ).date()
    raw = get_quotes([str(s).upper() for s in symbols])
    if not raw:
        return {}

    out: Dict[str, dict] = {}
    for symbol, quote in raw.items():
        row = quote.get("_row") or {}
        price = quote.get("price")
        # A print from an earlier day is not a live quote. Letting it through
        # is how the dashboard came to show a frozen number badged as current.
        if not price or quote.get("as_of") != today:
            continue
        prev_close = _finite(row.get("prevClose"))
        open_price = _finite(row.get("open"))
        regular_close = quote.get("regular_close")
        out[symbol] = {
            "symbol": symbol,
            "price": price,
            "prev_close": prev_close,
            "open_price": open_price,
            "regular_close": regular_close,
            "change_pct": (
                (price - prev_close) / prev_close * 100.0 if prev_close else 0.0
            ),
            "from_open_pct": (
                (price - open_price) / open_price * 100.0 if open_price else None
            ),
            "since_close_pct": (
                (price - regular_close) / regular_close * 100.0
                if regular_close and session != "regular" else None
            ),
            "volume": 0,
            "day_high": _finite(row.get("high")),
            "day_low": _finite(row.get("low")),
            "session": session,
            "timestamp": datetime.now(NYSE_TZ),
            "bar_time": parse_timestamp(row.get("timestamp")),
            "source": "tiingo",
        }
    return out
