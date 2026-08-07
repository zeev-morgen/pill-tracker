"""Currency conversion, for holding Tel Aviv positions alongside US ones.

Two conversions are involved and they are easy to confuse:

  agorot → shekels   A fixed ÷100. Yahoo quotes almost every TASE security in
                     agorot and labels it ``ILA``, so a share showing 3,450 is
                     worth ₪34.50. Treating that number as shekels overstates
                     the position a hundredfold, which is the single most
                     damaging mistake available here.

  shekels → dollars  A live rate, so the portfolio can carry one total instead
                     of two.

The rate is cached: it moves slowly next to a stock price, and the portfolio
endpoint would otherwise fetch it once per holding.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

#: Yahoo's symbol for the pair. The quote is shekels per dollar — about 3.6 —
#: so shekels convert to dollars by dividing, not multiplying.
USD_ILS_SYMBOL = "ILS=X"

#: Seconds to reuse a fetched rate. Long enough that a portfolio refresh costs
#: no requests, short enough to track the day's move.
TTL = 900

#: Yahoo's currency codes. ILA is agorot — one hundredth of a shekel — and is
#: what TASE equities are quoted in; ILS is shekels. Both appear in the wild,
#: which is why the code is read rather than assumed from the .TA suffix.
AGOROT = "ILA"
SHEKEL = "ILS"
DOLLAR = "USD"

#: Sanity bounds for the pair. A rate outside these has to be a parsing
#: mistake or a bad tick, and silently dividing a portfolio by a wrong number
#: is worse than showing no conversion at all.
_MIN_RATE, _MAX_RATE = 1.0, 20.0

_lock = threading.Lock()
_cached: dict = {"rate": None, "at": 0.0}


def _finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fetch_rate() -> Optional[float]:
    """Shekels per dollar from Yahoo, or None.

    Daily bars rather than a quote: the pair's daily series keeps arriving even
    when the intraday endpoints are throttled, and an FX rate a few hours old
    moves a portfolio total by fractions of a percent.
    """
    import yfinance as yf

    try:
        frame = yf.Ticker(USD_ILS_SYMBOL).history(period="5d")
        if frame is None or frame.empty or "Close" not in frame.columns:
            return None
        priced = frame["Close"].dropna()
        if priced.empty:
            return None
        return _finite(priced.iloc[-1])
    except Exception as exc:
        logger.warning("USD/ILS fetch failed: %s", exc.__class__.__name__)
        return None


def usd_ils_rate(force: bool = False) -> Optional[float]:
    """Shekels per dollar, cached. None when the rate cannot be had.

    None is a real answer and callers must handle it: an unconvertible
    position is reported as such rather than folded into a total at a guessed
    rate, because a wrong total is indistinguishable from a right one.
    """
    now = time.monotonic()
    with _lock:
        if not force and _cached["rate"] and now - _cached["at"] < TTL:
            return _cached["rate"]

    rate = _fetch_rate()
    if rate is None or not (_MIN_RATE <= rate <= _MAX_RATE):
        if rate is not None:
            logger.warning("implausible USD/ILS rate %s — ignoring", rate)
        # Keep serving the last good rate rather than dropping to None on one
        # bad fetch; a slightly stale rate beats a portfolio that loses its
        # Israeli positions for a refresh.
        with _lock:
            return _cached["rate"]

    with _lock:
        _cached["rate"] = rate
        _cached["at"] = now
    return rate


def reset_cache() -> None:
    """Drop the cached rate. For tests and for the diagnostics probe."""
    with _lock:
        _cached["rate"] = None
        _cached["at"] = 0.0


def normalize_currency(code) -> str:
    """Yahoo's currency code, upper-cased. Empty string when absent."""
    return str(code or "").strip().upper()


def to_shekels(amount, currency) -> Optional[float]:
    """A price in its quoted currency, expressed in shekels."""
    value = _finite(amount)
    if value is None:
        return None
    code = normalize_currency(currency)
    if code == AGOROT:
        return value / 100.0
    if code == SHEKEL:
        return value
    return None


def to_usd(amount, currency, rate: Optional[float] = None) -> Optional[float]:
    """A price in its quoted currency, expressed in dollars.

    ``rate`` is passed in by callers converting many positions at once, so a
    whole portfolio is valued against one consistent rate rather than against
    whatever each lookup happened to see.
    """
    value = _finite(amount)
    if value is None:
        return None
    code = normalize_currency(currency)
    if code in ("", DOLLAR):
        return value

    shekels = to_shekels(value, code)
    if shekels is None:
        logger.debug("no conversion for currency %r", code)
        return None

    rate = _finite(rate) if rate is not None else usd_ils_rate()
    if not rate or rate <= 0:
        return None
    return shekels / rate


def currency_for_ticker(ticker) -> str:
    """The quote currency implied by a ticker's exchange suffix.

    Yahoo's ``.info`` is authoritative and can tell agorot from shekels, but it
    is also the endpoint Yahoo throttles first, and the live monitor prices
    every watched symbol on a loop — it cannot afford a metadata request per
    symbol per cycle. The suffix is free and correct for effectively every
    Tel Aviv listing, so it carries the common path; callers that already hold
    an ``.info`` payload should prefer what it says.
    """
    return AGOROT if str(ticker or "").upper().endswith(".TA") else ""


def is_israeli(currency) -> bool:
    return normalize_currency(currency) in (AGOROT, SHEKEL)


def display_symbol(currency) -> str:
    """The symbol to print a native-currency price with."""
    code = normalize_currency(currency)
    if code == AGOROT:
        return "אג׳"
    if code == SHEKEL:
        return "₪"
    return "$"
