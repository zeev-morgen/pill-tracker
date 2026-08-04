"""NaN from yfinance must never reach the JSON encoder.

FastAPI's JSONResponse serializes with ``json.dumps(allow_nan=False)``, so a
single NaN anywhere in the payload raises ValueError and fails the entire
request — which is how one unpriceable holding took down the whole portfolio
tab with a bare "שגיאה בשליפת נתוני התיק".
"""

import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from stock_monitor import dashboard, portfolio_risk
from stock_monitor.config import NotificationConfig
from stock_monitor.data_feed import StockDataFeed
from stock_monitor.notifier import NotificationDispatcher
from stock_monitor.portfolio_risk import Fundamentals, PortfolioRiskAnalyzer
from stock_monitor.store import Holding, PortfolioStore, portfolio_store
from stock_monitor.webhook_server import create_webhook_app


def _history(last_close=100.0, rows=60):
    idx = pd.date_range("2026-01-01", periods=rows, freq="B")
    close = np.full(rows, 100.0)
    close[-1] = last_close
    return pd.DataFrame({"High": close + 1, "Low": close - 1, "Close": close}, index=idx)


def _unpriceable(rows=60):
    """Every bar blank — nothing to fall back to."""
    frame = _history(rows=rows)
    frame["Close"] = float("nan")
    return frame


@pytest.fixture(autouse=True)
def stub_fundamentals(monkeypatch):
    monkeypatch.setattr(
        portfolio_risk, "fetch_fundamentals",
        lambda t: Fundamentals(ticker=t, sector="Technology"),
    )


def _stub_history(monkeypatch, frame):
    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **kwargs):
            if isinstance(frame, Exception):
                raise frame
            return frame

    monkeypatch.setattr(portfolio_risk.yf, "Ticker", FakeTicker)


def _store(*tickers):
    store = PortfolioStore()
    for t in tickers:
        store.upsert(Holding.create(t, 10, 90.0))
    return store


# ── The exact production failure ──────────────────────────────────────────────

def test_a_nan_price_would_break_json_if_it_got_through():
    """Guards the premise: this is why a single NaN failed the whole endpoint."""
    with pytest.raises(ValueError):
        json.dumps({"price": float("nan")}, allow_nan=False)


def test_a_trailing_nan_close_falls_back_to_the_last_real_one(monkeypatch):
    """A blank final bar is normal — the price before it is still a price."""
    _stub_history(monkeypatch, _history(last_close=float("nan")))
    report = PortfolioRiskAnalyzer(_store("AMZN")).full_report()

    assert report["skipped_tickers"] == []
    assert report["positions"][0]["current_price"] == pytest.approx(100.0)
    json.dumps(report, allow_nan=False)      # the encoder FastAPI actually uses


def test_an_all_nan_history_skips_the_holding(monkeypatch):
    _stub_history(monkeypatch, _unpriceable())
    report = PortfolioRiskAnalyzer(_store("AMZN")).full_report()

    assert report["positions"] == []
    assert report["skipped_tickers"] == ["AMZN"]
    json.dumps(report, allow_nan=False)


def test_one_bad_ticker_does_not_take_the_others_with_it(monkeypatch):
    good = _history(last_close=120.0)
    bad = _unpriceable()

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **kwargs):
            return bad if self.symbol == "BROKEN" else good

    monkeypatch.setattr(portfolio_risk.yf, "Ticker", FakeTicker)
    report = PortfolioRiskAnalyzer(_store("AMZN", "BROKEN")).full_report()

    assert [p["ticker"] for p in report["positions"]] == ["AMZN"]
    assert report["skipped_tickers"] == ["BROKEN"]
    assert report["total_value"] == pytest.approx(1200.0)


@pytest.mark.parametrize("price", [float("inf"), 0.0, -5.0])
def test_unusable_prices_are_all_rejected(monkeypatch, price):
    """NaN is excluded: it now means 'no bar', and falls back to the last one."""
    _stub_history(monkeypatch, _history(last_close=price))
    report = PortfolioRiskAnalyzer(_store("AMZN")).full_report()
    assert report["skipped_tickers"] == ["AMZN"]
    json.dumps(report, allow_nan=False)


def test_a_frame_without_a_close_column_is_skipped_not_raised(monkeypatch):
    """yfinance occasionally returns a frame of a different shape."""
    _stub_history(monkeypatch, pd.DataFrame({"Open": [1.0, 2.0]}))
    report = PortfolioRiskAnalyzer(_store("AMZN")).full_report()
    assert report["skipped_tickers"] == ["AMZN"]


def test_an_unexpected_exception_skips_only_that_holding(monkeypatch):
    """The whole per-holding body is guarded, not just the fetch."""
    _stub_history(monkeypatch, RuntimeError("boom"))
    report = PortfolioRiskAnalyzer(_store("AMZN")).full_report()
    assert report["skipped_tickers"] == ["AMZN"]
    json.dumps(report, allow_nan=False)


def test_skipped_list_resets_between_runs(monkeypatch):
    _stub_history(monkeypatch, _unpriceable())
    analyzer = PortfolioRiskAnalyzer(_store("AMZN"))
    analyzer.full_report()
    _stub_history(monkeypatch, _history(last_close=120.0))
    assert analyzer.full_report()["skipped_tickers"] == []


# ── Extended-hours quotes ─────────────────────────────────────────────────────

def test_nan_from_the_feed_blanks_the_column_not_the_report(monkeypatch):
    class NanFeed:
        def get_current_data(self, symbol):
            return {"session": "pre", "price": float("nan"),
                    "since_close_pct": float("nan")}

    _stub_history(monkeypatch, _history())
    report = PortfolioRiskAnalyzer(_store("AMZN"), data_feed=NanFeed()).full_report()
    position = report["positions"][0]
    assert position["extended_price"] is None
    assert position["extended_change_pct"] is None
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize(
    "value, expected",
    [(float("nan"), None), (float("inf"), None), (191.0, 191.0), (None, None)],
)
def test_data_feed_treats_nan_as_missing(value, expected):
    """NaN survives every `is not None` check, so it must be rejected here.

    fast_info is read with getattr, so the stub has to be an object — a dict
    returns None for everything and the assertions pass without testing.
    """
    fast_info = SimpleNamespace(last_price=value)
    assert StockDataFeed._fi_get(fast_info, "last_price") == expected


def test_fi_get_falls_through_to_the_next_name(monkeypatch):
    """A NaN under one key must not stop the alias from being tried."""
    fast_info = SimpleNamespace(last_price=float("nan"), regularMarketPrice=191.0)
    assert StockDataFeed._fi_get(fast_info, "last_price", "regularMarketPrice") == 191.0


# ── Through the real endpoint ─────────────────────────────────────────────────

def test_the_portfolio_endpoint_survives_a_nan_price(monkeypatch):
    for holding in list(portfolio_store.all()):
        portfolio_store.delete(holding.ticker)
    portfolio_store.upsert(Holding.create("AMZN", 10, 90.0))
    _stub_history(monkeypatch, _unpriceable())
    monkeypatch.setattr(dashboard, "get_data_feed", lambda: None)

    client = TestClient(create_webhook_app(NotificationDispatcher(NotificationConfig()), ""))
    try:
        response = client.get("/api/portfolio")
        assert response.status_code == 200, response.text
        assert response.json()["skipped_tickers"] == ["AMZN"]
    finally:
        portfolio_store.delete("AMZN")


def test_a_systemic_failure_names_the_exception_type(monkeypatch):
    """A bare 'שגיאה' left nothing to diagnose from the browser."""
    def boom():
        raise ZeroDivisionError("something structural")

    monkeypatch.setattr(dashboard._risk_analyzer, "full_report", boom)
    monkeypatch.setattr(dashboard, "get_data_feed", lambda: None)
    client = TestClient(create_webhook_app(NotificationDispatcher(NotificationConfig()), ""))
    response = client.get("/api/portfolio")
    assert response.status_code == 502
    assert "ZeroDivisionError" in response.json()["detail"]
