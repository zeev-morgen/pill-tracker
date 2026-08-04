"""Pre/post-market quotes and holding duration in the portfolio report."""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from stock_monitor import data_feed, portfolio_risk
from stock_monitor.portfolio_risk import Fundamentals, PortfolioRiskAnalyzer
from stock_monitor.store import Holding, PortfolioStore


def _history(days=60, price=100.0):
    idx = pd.date_range("2024-01-01", periods=days, freq="B")
    close = np.full(days, price)
    return pd.DataFrame({"High": close + 1, "Low": close - 1, "Close": close}, index=idx)


@pytest.fixture(autouse=True)
def stub_market(monkeypatch):
    """No network: fixed price history and fundamentals for every ticker."""
    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **kwargs):
            return _history()

    monkeypatch.setattr(portfolio_risk.yf, "Ticker", FakeTicker)
    monkeypatch.setattr(
        portfolio_risk, "fetch_fundamentals",
        lambda ticker: Fundamentals(ticker=ticker, sector="Technology", asset_type="stock"),
    )


@pytest.fixture
def session(monkeypatch):
    """Drive the market session, which is read from the clock, not the feed."""
    def _set(name):
        monkeypatch.setattr(data_feed, "get_market_session", lambda: name)
    return _set


class _Feed:
    def __init__(self, payload):
        self._payload = payload

    def get_current_data(self, symbol):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _store(**holding_kwargs):
    store = PortfolioStore()
    store.upsert(Holding.create("AMZN", 10, 90.0, **holding_kwargs))
    return store


# ── Extended-hours quotes ─────────────────────────────────────────────────────

def test_pre_market_price_and_move_reach_the_report(session):
    session("pre")
    analyzer = PortfolioRiskAnalyzer(
        _store(), data_feed=_Feed({"session": "pre", "price": 105.0, "since_close_pct": 5.0})
    )
    position = analyzer.full_report()["positions"][0]
    assert position["session"] == "pre"
    assert position["extended_price"] == pytest.approx(105.0)
    assert position["extended_change_pct"] == pytest.approx(5.0)


def test_after_hours_is_reported_the_same_way(session):
    session("after")
    analyzer = PortfolioRiskAnalyzer(
        _store(), data_feed=_Feed({"session": "after", "price": 96.0, "since_close_pct": -4.0})
    )
    position = analyzer.full_report()["positions"][0]
    assert position["session"] == "after"
    assert position["extended_change_pct"] == pytest.approx(-4.0)


def test_during_regular_hours_there_is_no_separate_extended_price(session):
    """The regular price is already on the row — repeating it would mislead."""
    session("regular")
    analyzer = PortfolioRiskAnalyzer(
        _store(), data_feed=_Feed({"session": "regular", "price": 105.0, "since_close_pct": 5.0})
    )
    position = analyzer.full_report()["positions"][0]
    assert position["session"] == "regular"
    assert position["extended_price"] is None
    assert position["extended_change_pct"] is None


def test_without_a_feed_the_report_still_builds(session):
    session("regular")
    position = PortfolioRiskAnalyzer(_store()).full_report()["positions"][0]
    assert position["session"] == "regular"
    assert position["extended_price"] is None
    assert position["market_value"] == pytest.approx(1000.0)


def test_a_failing_feed_costs_only_the_extended_columns(session):
    session("pre")
    analyzer = PortfolioRiskAnalyzer(_store(), data_feed=_Feed(RuntimeError("feed down")))
    position = analyzer.full_report()["positions"][0]
    assert position["extended_price"] is None
    assert position["pnl_pct"] == pytest.approx(11.11, abs=0.01)   # report is intact


def test_the_feed_can_be_attached_after_construction(session):
    """The dashboard's analyzer is built before the app owns a feed."""
    session("pre")
    analyzer = PortfolioRiskAnalyzer(_store())
    analyzer.set_data_feed(_Feed({"session": "pre", "price": 105.0, "since_close_pct": 5.0}))
    assert analyzer.full_report()["positions"][0]["extended_price"] == pytest.approx(105.0)


# ── Holding duration ──────────────────────────────────────────────────────────

def test_holding_days_counts_from_the_purchase_date():
    bought = date.today() - timedelta(days=45)
    position = PortfolioRiskAnalyzer(
        _store(purchase_date=bought.isoformat())
    ).full_report()["positions"][0]
    assert position["purchase_date"] == bought.isoformat()
    assert position["holding_days"] == 45


def test_a_position_opened_today_shows_zero_days():
    position = PortfolioRiskAnalyzer(
        _store(purchase_date=date.today().isoformat())
    ).full_report()["positions"][0]
    assert position["holding_days"] == 0


def test_holding_duration_is_absent_when_no_date_was_entered():
    position = PortfolioRiskAnalyzer(_store()).full_report()["positions"][0]
    assert position["purchase_date"] is None
    assert position["holding_days"] is None
