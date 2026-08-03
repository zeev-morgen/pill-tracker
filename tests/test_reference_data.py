"""Curated sector/ETF table and the sector-resolution fallback chain."""

import pytest

from stock_monitor import reference_data as rd
from stock_monitor.portfolio_risk import Fundamentals, UNKNOWN_SECTOR

# The sectors the dashboard offers in its datalist — the curated table must not
# invent names outside this vocabulary, or the manual editor and the automatic
# source would disagree and split one sector into two slices.
VALID_SECTORS = {
    "Technology", "Healthcare", "Financial Services", "Consumer Cyclical",
    "Consumer Defensive", "Energy", "Basic Materials", "Industrials",
    "Utilities", "Real Estate", "Communication Services",
}


# ── Table integrity ───────────────────────────────────────────────────────────

def test_every_curated_sector_uses_the_shared_vocabulary():
    invalid = {t: s for t, s in rd.SECTOR_BY_TICKER.items() if s not in VALID_SECTORS}
    assert invalid == {}


def test_no_ticker_is_both_a_stock_and_an_etf():
    assert rd.KNOWN_ETFS & set(rd.SECTOR_BY_TICKER) == set()


def test_tickers_are_stored_uppercase():
    assert all(t == t.upper() for t in rd.SECTOR_BY_TICKER)
    assert all(t == t.upper() for t in rd.KNOWN_ETFS)


# ── Lookup ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "ticker,expected",
    [
        ("AVGO", "Technology"),
        ("AMZN", "Consumer Cyclical"),
        ("META", "Communication Services"),
        ("CF", "Basic Materials"),
        ("ABCL", "Healthcare"),
        ("JPM", "Financial Services"),
        ("XOM", "Energy"),
    ],
)
def test_known_tickers_resolve(ticker, expected):
    assert rd.lookup_sector(ticker) == expected


def test_lookup_is_case_and_whitespace_insensitive():
    assert rd.lookup_sector("  avgo ") == "Technology"


def test_unknown_ticker_returns_none_rather_than_guessing():
    assert rd.lookup_sector("ZZZZ") is None


def test_etfs_report_diversified_not_a_single_sector():
    # Filing a broad-market fund under one sector would distort concentration.
    assert rd.lookup_sector("SPY") == rd.DIVERSIFIED
    assert rd.is_known_etf("SPY") is True


def test_individual_stocks_are_not_flagged_as_etfs():
    assert rd.is_known_etf("AAPL") is False


# ── Fallback chain: yfinance → curated table → Unknown ────────────────────────

def _patch_yfinance(monkeypatch, info):
    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def info(self):
            if isinstance(info, Exception):
                raise info
            return info

    import stock_monitor.portfolio_risk as pr

    pr.fetch_fundamentals.cache_clear()
    monkeypatch.setattr(pr.yf, "Ticker", FakeTicker)
    return pr


def test_yfinance_wins_when_it_answers(monkeypatch):
    pr = _patch_yfinance(monkeypatch, {"sector": "Utilities", "quoteType": "EQUITY"})
    assert pr.fetch_fundamentals("AVGO").sector == "Utilities"


def test_curated_table_fills_in_when_yfinance_is_empty(monkeypatch):
    pr = _patch_yfinance(monkeypatch, {})
    assert pr.fetch_fundamentals("AVGO").sector == "Technology"


def test_curated_table_fills_in_when_yfinance_raises(monkeypatch):
    # This is the real-world case: throttled or blocked requests.
    pr = _patch_yfinance(monkeypatch, ConnectionError("blocked"))
    fundamentals = pr.fetch_fundamentals("CF")
    assert fundamentals.sector == "Basic Materials"


def test_etf_detected_from_the_table_when_yfinance_is_silent(monkeypatch):
    pr = _patch_yfinance(monkeypatch, {})
    assert pr.fetch_fundamentals("SPY").asset_type == "etf"


def test_uncovered_ticker_stays_unknown_for_manual_editing(monkeypatch):
    pr = _patch_yfinance(monkeypatch, {})
    fundamentals = pr.fetch_fundamentals("ZZZZ")
    assert fundamentals.sector == UNKNOWN_SECTOR
    assert fundamentals.asset_type == "stock"


def test_quote_type_still_identifies_unlisted_etfs(monkeypatch):
    pr = _patch_yfinance(monkeypatch, {"quoteType": "ETF"})
    assert pr.fetch_fundamentals("NEWETF").asset_type == "etf"
