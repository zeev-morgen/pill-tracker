"""Noticing that the price feed as a whole has stopped advancing.

``price_is_stale`` ranks each holding against the freshest bar in the
portfolio. That catches one ticker lagging its neighbours and is blind to the
case that actually happened: every holding a full trading day behind, perfectly
consistent with each other, displayed without a mark while the market was open.
These tests are about the second case.
"""

from datetime import date

import pytest

from stock_monitor import portfolio_risk
from stock_monitor.portfolio_risk import _sessions_behind


@pytest.fixture
def et_now(monkeypatch):
    """Pins the exchange clock. Returns a setter taking (Y, M, D, hh, mm)."""
    import datetime as dt

    from stock_monitor.data_feed import NYSE_TZ

    real = dt.datetime

    def _set(year, month, day, hour, minute):
        pinned = NYSE_TZ.localize(real(year, month, day, hour, minute))

        class FakeDatetime(real):
            @classmethod
            def now(cls, tz=None):
                return pinned if tz else pinned.replace(tzinfo=None)

        monkeypatch.setattr(portfolio_risk, "datetime", FakeDatetime)

    return _set


# ── What counts as "the session we should have" ───────────────────────────────

def test_after_the_bell_todays_own_session_is_expected(et_now):
    et_now(2026, 8, 6, 10, 0)  # Thursday, market open
    assert portfolio_risk._expected_session() == date(2026, 8, 6)


def test_before_the_bell_yesterdays_close_is_the_newest_there_is(et_now):
    """At 07:00 there is no bar for today yet — that is not an outage."""
    et_now(2026, 8, 6, 7, 0)
    assert portfolio_risk._expected_session() == date(2026, 8, 5)


def test_the_bell_itself_counts_as_open(et_now):
    et_now(2026, 8, 6, 9, 30)
    assert portfolio_risk._expected_session() == date(2026, 8, 6)


def test_a_minute_before_the_bell_does_not(et_now):
    et_now(2026, 8, 6, 9, 29)
    assert portfolio_risk._expected_session() == date(2026, 8, 5)


def test_on_a_weekend_the_last_weekday_is_expected(et_now):
    et_now(2026, 8, 8, 12, 0)  # Saturday
    assert portfolio_risk._expected_session() == date(2026, 8, 7)


def test_on_monday_morning_fridays_close_is_expected(et_now):
    et_now(2026, 8, 10, 7, 0)  # Monday, pre-market
    assert portfolio_risk._expected_session() == date(2026, 8, 7)


# ── The lag itself ────────────────────────────────────────────────────────────

def test_todays_bar_during_the_session_is_not_behind(et_now):
    et_now(2026, 8, 6, 10, 0)
    assert _sessions_behind(date(2026, 8, 6)) == 0


def test_the_reported_outage_is_one_session_behind(et_now):
    """The real case: 10:00 ET Thursday, newest bar Wednesday's close."""
    et_now(2026, 8, 6, 10, 0)
    assert _sessions_behind(date(2026, 8, 5)) == 1


def test_yesterdays_bar_before_the_bell_is_not_behind(et_now):
    """The distinction that stops the banner crying wolf every morning."""
    et_now(2026, 8, 6, 7, 0)
    assert _sessions_behind(date(2026, 8, 5)) == 0


def test_the_weekend_is_not_counted_as_an_outage(et_now):
    et_now(2026, 8, 10, 10, 0)  # Monday morning, market open
    assert _sessions_behind(date(2026, 8, 7)) == 1, "only Monday is missing"


def test_a_long_gap_counts_weekdays_not_calendar_days(et_now):
    et_now(2026, 8, 10, 10, 0)  # Monday
    # Missing: Thu 6, Fri 7, Mon 10 — the weekend in between is not a session.
    assert _sessions_behind(date(2026, 8, 5)) == 3


def test_no_priced_holdings_reports_no_lag(et_now):
    """Nothing to be behind on; the skipped-tickers note covers that case."""
    et_now(2026, 8, 6, 10, 0)
    assert _sessions_behind(None) == 0


def test_a_bar_ahead_of_the_clock_is_not_negative(et_now):
    """Clock skew must not produce a negative lag that reads as truthy."""
    et_now(2026, 8, 6, 10, 0)
    assert _sessions_behind(date(2026, 8, 7)) == 0


# ── The field reaches the report ──────────────────────────────────────────────

def test_the_report_carries_the_lag(monkeypatch):
    analyzer = portfolio_risk.PortfolioRiskAnalyzer(None)
    monkeypatch.setattr(analyzer, "collect_positions", lambda: [])
    monkeypatch.setattr(portfolio_risk, "_sessions_behind", lambda bar: 2)

    assert analyzer.full_report()["feed_lag_days"] == 2


def test_the_lag_is_measured_against_the_newest_bar(monkeypatch):
    """Not against any single holding — the whole portfolio's best read."""
    analyzer = portfolio_risk.PortfolioRiskAnalyzer(None)
    monkeypatch.setattr(analyzer, "collect_positions", lambda: [
        {"ticker": "A", "quantity": 1, "entry_price": 1.0, "current_price": 1.0,
         "market_value": 1.0, "pnl_pct": 0.0, "pnl_value": 0.0, "atr_pct": None,
         "price_date": date(2026, 8, 4), "sector": "X", "sector_is_manual": False,
         "asset_type": "stock", "asset_type_is_manual": False},
        {"ticker": "B", "quantity": 1, "entry_price": 1.0, "current_price": 1.0,
         "market_value": 1.0, "pnl_pct": 0.0, "pnl_value": 0.0, "atr_pct": None,
         "price_date": date(2026, 8, 5), "sector": "X", "sector_is_manual": False,
         "asset_type": "stock", "asset_type_is_manual": False},
    ])
    seen = []
    monkeypatch.setattr(portfolio_risk, "_sessions_behind",
                        lambda bar: seen.append(bar) or 0)

    report = analyzer.full_report()
    assert seen == [date(2026, 8, 5)]
    assert report["latest_bar_date"] == "2026-08-05"


def test_a_uniformly_late_feed_flags_nothing_per_row(monkeypatch):
    """Why the portfolio-level field had to exist at all."""
    analyzer = portfolio_risk.PortfolioRiskAnalyzer(None)
    monkeypatch.setattr(analyzer, "collect_positions", lambda: [
        {"ticker": t, "quantity": 1, "entry_price": 1.0, "current_price": 1.0,
         "market_value": 1.0, "pnl_pct": 0.0, "pnl_value": 0.0, "atr_pct": None,
         "price_date": date(2026, 8, 5), "sector": "X", "sector_is_manual": False,
         "asset_type": "stock", "asset_type_is_manual": False}
        for t in ("A", "B", "C")
    ])
    monkeypatch.setattr(portfolio_risk, "_sessions_behind", lambda bar: 1)

    report = analyzer.full_report()
    assert not any(p["price_is_stale"] for p in report["positions"])
    assert report["feed_lag_days"] == 1, "the only signal there is"
