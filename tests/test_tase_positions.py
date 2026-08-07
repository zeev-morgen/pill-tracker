"""A Tel Aviv holding sitting in the same portfolio as US ones.

Two things have to hold at once: the price columns stay in the currency the
broker quotes, so the table can be checked against a broker screen; and the
value and P&L columns are all in dollars, so the totals are a number rather
than a sum of mixed units.
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stock_monitor import fx, portfolio_risk
from stock_monitor.portfolio_risk import Fundamentals, PortfolioRiskAnalyzer
from stock_monitor.store import Holding


def _history(rows=30, close=100.0):
    idx = pd.bdate_range(end=date.today(), periods=rows)
    values = np.linspace(close * 0.9, close, rows)
    return pd.DataFrame({"Open": values, "High": values * 1.01,
                         "Low": values * 0.99, "Close": values,
                         "Volume": [1000] * rows}, index=idx)


class _Store:
    def __init__(self, holdings):
        self._holdings = holdings

    def all(self):
        return self._holdings


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    # Cleared on the way in only: tests replace fetch_fundamentals outright,
    # so by teardown the name no longer refers to the cached original.
    portfolio_risk.fetch_fundamentals.cache_clear()
    fx.reset_cache()
    monkeypatch.setattr(fx, "_fetch_rate", lambda: 3.60)
    yield
    fx.reset_cache()


@pytest.fixture
def analyzer(monkeypatch):
    """A portfolio of one TASE share at 3,450 agorot and one US share at $100."""
    holdings = [
        Holding.create("POLI.TA", 10, 3000.0),
        Holding.create("AVGO", 2, 90.0),
    ]
    prices = {"POLI.TA": 3450.0, "AVGO": 100.0}
    currencies = {"POLI.TA": "ILA", "AVGO": "USD"}

    monkeypatch.setattr(
        portfolio_risk, "fetch_fundamentals",
        lambda t: Fundamentals(ticker=t, sector="Financial Services", name=t,
                               currency=currencies[t]),
    )
    inst = PortfolioRiskAnalyzer(_Store(holdings))
    monkeypatch.setattr(
        inst, "_fetch_histories",
        lambda tickers, **kw: {t: _history(close=prices[t]) for t in tickers},
    )
    monkeypatch.setattr(inst, "_live_quotes", lambda tickers, session: {})
    monkeypatch.setattr(inst, "_quote", lambda ticker: {})
    return inst


def _by_ticker(positions):
    return {p["ticker"]: p for p in positions}


# ── Currency detection ────────────────────────────────────────────────────────

def test_the_ta_suffix_implies_agorot_when_yahoo_says_nothing():
    """.info is the endpoint Yahoo throttles first, so it cannot be relied on."""
    assert portfolio_risk._resolve_currency("POLI.TA", {}) == "ILA"


def test_yahoos_own_answer_wins():
    """Some TASE listings really are quoted in shekels, not agorot."""
    assert portfolio_risk._resolve_currency("XYZ.TA", {"currency": "ILS"}) == "ILS"


def test_a_us_ticker_has_no_currency_override():
    assert portfolio_risk._resolve_currency("AVGO", {}) == ""


def test_the_suffix_check_ignores_case():
    assert portfolio_risk._resolve_currency("poli.ta", {}) == "ILA"


# ── The position ──────────────────────────────────────────────────────────────

def test_the_price_stays_in_agorot(analyzer):
    """So the row can be read against a broker screen without arithmetic."""
    position = _by_ticker(analyzer.collect_positions())["POLI.TA"]

    assert position["current_price"] == pytest.approx(3450.0)
    assert position["entry_price"] == pytest.approx(3000.0)
    assert position["currency"] == "ILA"


def test_the_value_is_in_dollars(analyzer):
    """10 shares × 3,450 agorot = ₪345, which at 3.60 is $95.83."""
    position = _by_ticker(analyzer.collect_positions())["POLI.TA"]

    assert position["market_value"] == pytest.approx(345.0 / 3.60)
    assert position["native_market_value"] == pytest.approx(34500.0)


def test_the_dollar_value_is_not_the_agorot_figure(analyzer):
    """The failure this whole module guards: 34,500 counted as $34,500."""
    position = _by_ticker(analyzer.collect_positions())["POLI.TA"]

    assert position["market_value"] < 200, "agorot leaked into the dollar total"


def test_profit_percent_is_currency_free(analyzer):
    """3,000 → 3,450 is +15% whichever unit both sides are measured in."""
    position = _by_ticker(analyzer.collect_positions())["POLI.TA"]

    assert position["pnl_pct"] == pytest.approx(15.0)


def test_profit_value_is_converted(analyzer):
    """450 agorot × 10 = ₪45, which at 3.60 is $12.50."""
    position = _by_ticker(analyzer.collect_positions())["POLI.TA"]

    assert position["pnl_value"] == pytest.approx(45.0 / 3.60)


def test_a_us_position_is_untouched(analyzer):
    position = _by_ticker(analyzer.collect_positions())["AVGO"]

    assert position["current_price"] == pytest.approx(100.0)
    assert position["market_value"] == pytest.approx(200.0)
    assert position["pnl_value"] == pytest.approx(20.0)


def test_the_total_adds_dollars_to_dollars(analyzer):
    report = analyzer.full_report()

    assert report["total_value"] == pytest.approx(345.0 / 3.60 + 200.0, abs=0.02)
    assert report["has_foreign"] is True
    assert report["fx_rate"] == pytest.approx(3.60)


def test_a_purely_american_portfolio_reports_no_rate(monkeypatch):
    """No FX request is made, and the header has nothing to explain."""
    monkeypatch.setattr(fx, "_fetch_rate", lambda: pytest.fail("no rate needed"))
    monkeypatch.setattr(
        portfolio_risk, "fetch_fundamentals",
        lambda t: Fundamentals(ticker=t, sector="Technology", name=t, currency="USD"),
    )
    inst = PortfolioRiskAnalyzer(_Store([Holding.create("AVGO", 2, 90.0)]))
    monkeypatch.setattr(inst, "_fetch_histories", lambda tickers, **kw: {
        t: _history(close=100.0) for t in tickers})
    monkeypatch.setattr(inst, "_live_quotes", lambda tickers, session: {})
    monkeypatch.setattr(inst, "_quote", lambda ticker: {})

    report = inst.full_report()
    assert report["has_foreign"] is False
    assert report["fx_rate"] is None


def test_sector_weights_are_computed_on_converted_values(analyzer):
    """Unconverted, one Israeli holding would swamp every weight in the report."""
    report = analyzer.full_report()
    total = sum(s["market_value"] for s in report["sector"]["sectors"])

    assert total == pytest.approx(report["total_value"], abs=0.02)


# ── When the rate cannot be had ───────────────────────────────────────────────

def test_without_a_rate_the_israeli_position_is_skipped_not_guessed(analyzer, monkeypatch):
    """Counting agorot as dollars is a hundredfold error in the total.

    Dropping the row loses information; keeping it loses correctness, and the
    banner already exists to report a skipped ticker.
    """
    fx.reset_cache()
    monkeypatch.setattr(fx, "_fetch_rate", lambda: None)

    positions = _by_ticker(analyzer.collect_positions())
    assert "POLI.TA" not in positions
    assert "AVGO" in positions, "a missing FX rate must not cost the US rows"
    assert "POLI.TA" in analyzer.skipped_tickers


def test_the_skip_reason_names_the_cause(analyzer, monkeypatch):
    fx.reset_cache()
    monkeypatch.setattr(fx, "_fetch_rate", lambda: None)
    analyzer.collect_positions()

    assert "FX" in analyzer.skip_reason


def test_every_row_uses_the_same_rate(monkeypatch):
    """Two rows valued at different rates would not reconcile with the total."""
    seen = []

    def drifting():
        seen.append(1)
        return 3.60 + len(seen) * 0.1

    monkeypatch.setattr(fx, "_fetch_rate", drifting)
    monkeypatch.setattr(fx, "TTL", -1)  # cache disabled, so each call refetches
    monkeypatch.setattr(
        portfolio_risk, "fetch_fundamentals",
        lambda t: Fundamentals(ticker=t, sector="X", name=t, currency="ILA"),
    )
    inst = PortfolioRiskAnalyzer(_Store([
        Holding.create("AAA.TA", 10, 100.0), Holding.create("BBB.TA", 10, 100.0)]))
    monkeypatch.setattr(inst, "_fetch_histories", lambda tickers, **kw: {
        t: _history(close=1000.0) for t in tickers})
    monkeypatch.setattr(inst, "_live_quotes", lambda tickers, session: {})
    monkeypatch.setattr(inst, "_quote", lambda ticker: {})

    positions = _by_ticker(inst.collect_positions())
    assert positions["AAA.TA"]["market_value"] == pytest.approx(
        positions["BBB.TA"]["market_value"]
    ), "identical holdings valued differently"
