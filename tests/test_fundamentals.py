"""Valuation multiples: fallbacks, and never letting NaN reach the prompt."""

import math

import pytest

from stock_monitor import ai_analyst
from stock_monitor.ai_analyst import StockAnalyst, _fast_get, _finite_or_none

FULL = {
    "trailingPE": 34.2, "forwardPE": 28.1, "pegRatio": 1.8,
    "marketCap": 1.62e12, "revenueGrowth": 0.21, "earningsGrowth": 0.34,
    "grossMargins": 0.63, "profitMargins": 0.29, "recommendationKey": "buy",
    "targetMeanPrice": 410.0, "fiftyTwoWeekHigh": 400.0, "fiftyTwoWeekLow": 190.0,
    "sector": "Technology", "shortName": "Broadcom Inc.",
}


@pytest.fixture
def analyst():
    return StockAnalyst.__new__(StockAnalyst)   # no API client is needed


def _stub_yf(monkeypatch, info, fast=None):
    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def info(self):
            if isinstance(info, Exception):
                raise info
            return info

        @property
        def fast_info(self):
            return fast or {}

    monkeypatch.setattr(ai_analyst.yf, "Ticker", FakeTicker)


# ── NaN handling ──────────────────────────────────────────────────────────────

def test_fast_get_treats_nan_as_missing():
    """NaN is truthy, so an unguarded value would print as 'nan'."""
    assert _fast_get({"year_high": float("nan")}, "year_high") is None
    assert _fast_get({"year_high": 400.0}, "year_high") == 400.0


def test_finite_or_none_filters_nan_and_infinity():
    assert _finite_or_none(float("nan")) is None
    assert _finite_or_none(float("inf")) is None
    assert _finite_or_none(12.5) == 12.5
    assert _finite_or_none("Technology") == "Technology"
    assert _finite_or_none(None) is None


def test_nan_from_info_never_reaches_the_prompt(analyst, monkeypatch):
    nan_info = dict(FULL, marketCap=float("nan"), trailingPE=float("nan"),
                    fiftyTwoWeekHigh=float("nan"), fiftyTwoWeekLow=float("nan"))
    _stub_yf(monkeypatch, nan_info)
    extra = analyst._fetch_fundamentals("AVGO")
    assert extra["market_cap"] is None and extra["pe_ratio"] is None

    prompt = analyst._build_prompt("AVGO", {"price": 352.1, "change_pct": -1.2}, extra, [], {})
    assert "nan" not in prompt.lower()
    assert "אין נתון" in prompt


def test_nan_from_fast_info_never_reaches_the_prompt(analyst, monkeypatch):
    _stub_yf(monkeypatch, {}, fast={"market_cap": float("nan"), "year_high": float("nan")})
    extra = analyst._fetch_fundamentals("AVGO")
    prompt = analyst._build_prompt("AVGO", {"price": 352.1, "change_pct": -1.2}, extra, [], {})
    assert "nan" not in prompt.lower()


# ── Fallbacks ─────────────────────────────────────────────────────────────────

def test_full_info_produces_every_multiple(analyst, monkeypatch):
    _stub_yf(monkeypatch, FULL)
    prompt = analyst._build_prompt(
        "AVGO", {"price": 352.1, "change_pct": -1.2},
        analyst._fetch_fundamentals("AVGO"), [], {},
    )
    for expected in ("P/E נוכחי: 34.2", "P/E עתידי: 28.1", "PEG: 1.80",
                     "שווי שוק: $1.6T", "מרווח גולמי: 63.0%", "המלצת אנליסטים: buy"):
        assert expected in prompt


def test_fast_info_supplies_market_cap_when_info_is_empty(analyst, monkeypatch):
    """.info comes back empty far more often than fast_info does."""
    _stub_yf(monkeypatch, {}, fast={"market_cap": 1.62e12, "year_high": 400.0, "year_low": 190.0})
    extra = analyst._fetch_fundamentals("AVGO")
    assert extra["market_cap"] == pytest.approx(1.62e12)
    assert extra["fifty_two_high"] == pytest.approx(400.0)
    # Multiples are simply not on that endpoint — they stay missing.
    assert extra["pe_ratio"] is None


def test_sector_falls_back_to_the_curated_table(analyst, monkeypatch):
    _stub_yf(monkeypatch, {})
    assert analyst._fetch_fundamentals("AVGO")["sector"] == "Technology"


def test_a_raising_info_degrades_instead_of_propagating(analyst, monkeypatch):
    _stub_yf(monkeypatch, RuntimeError("rate limited"))
    extra = analyst._fetch_fundamentals("AVGO")
    assert extra["pe_ratio"] is None
    assert extra["short_name"] == "AVGO"      # falls back to the symbol


def test_missing_multiples_are_labelled_not_omitted(analyst, monkeypatch):
    """The section must still appear, so the model knows the data is absent."""
    _stub_yf(monkeypatch, {})
    prompt = analyst._build_prompt(
        "AVGO", {"price": 352.1, "change_pct": -1.2},
        analyst._fetch_fundamentals("AVGO"), [], {},
    )
    assert prompt.count("אין נתון") >= 4
    assert "None" not in prompt
