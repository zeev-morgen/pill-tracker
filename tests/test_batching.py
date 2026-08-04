"""Request volume against Yahoo.

Yahoo rate-limits per IP, and a shared cloud host reaches that limit quickly.
Every avoidable request per holding, on every refresh, is what got the whole
portfolio throttled at once — so the call count is the thing under test here,
not just the values that come back.
"""

import numpy as np
import pandas as pd
import pytest

from stock_monitor import data_feed, portfolio_risk
from stock_monitor.portfolio_risk import Fundamentals, PortfolioRiskAnalyzer
from stock_monitor.store import Holding, PortfolioStore

TICKERS = ["AMZN", "CF", "MU", "ORCL", "SEDG"]


def _frame(price=100.0, rows=60):
    idx = pd.date_range("2026-01-01", periods=rows, freq="B")
    close = np.full(rows, price)
    return pd.DataFrame({"High": close + 1, "Low": close - 1, "Close": close}, index=idx)


def _batch(tickers, price=100.0):
    """What yf.download returns for several tickers: MultiIndex columns."""
    frames = {t: _frame(price) for t in tickers}
    return pd.concat(frames, axis=1)


@pytest.fixture(autouse=True)
def stub_fundamentals(monkeypatch):
    monkeypatch.setattr(
        portfolio_risk, "fetch_fundamentals",
        lambda t: Fundamentals(ticker=t, sector="Technology"),
    )
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "regular")


@pytest.fixture
def store():
    store = PortfolioStore()
    for t in TICKERS:
        store.upsert(Holding.create(t, 10, 90.0))
    return store


@pytest.fixture
def counting_ticker(monkeypatch):
    """Counts per-ticker history calls — the ones batching is meant to remove."""
    calls = []

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **kwargs):
            calls.append(self.symbol)
            return _frame()

    monkeypatch.setattr(portfolio_risk.yf, "Ticker", FakeTicker)
    return calls


# ── Batching ──────────────────────────────────────────────────────────────────

def test_one_request_covers_the_whole_portfolio(monkeypatch, store, counting_ticker):
    downloads = []

    def fake_download(tickers, **kwargs):
        downloads.append(list(tickers))
        return _batch(TICKERS)

    monkeypatch.setattr(portfolio_risk.yf, "download", fake_download)
    report = PortfolioRiskAnalyzer(store).full_report()

    assert len(report["positions"]) == len(TICKERS)
    assert downloads == [TICKERS], "the batch must ask for every ticker at once"
    assert counting_ticker == [], "no per-ticker fetch when the batch covers it"


def test_tickers_missing_from_the_batch_fall_back_individually(
    monkeypatch, store, counting_ticker
):
    """A partial answer still yields most positions without refetching them all."""
    covered = TICKERS[:3]
    monkeypatch.setattr(portfolio_risk.yf, "download",
                        lambda tickers, **kw: _batch(covered))
    report = PortfolioRiskAnalyzer(store).full_report()

    assert len(report["positions"]) == len(TICKERS)
    assert sorted(counting_ticker) == sorted(TICKERS[3:])


def test_a_failed_batch_degrades_to_per_ticker(monkeypatch, store, counting_ticker):
    def boom(tickers, **kwargs):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(portfolio_risk.yf, "download", boom)
    report = PortfolioRiskAnalyzer(store).full_report()

    assert len(report["positions"]) == len(TICKERS)
    assert sorted(counting_ticker) == sorted(TICKERS)


def test_a_single_holding_batch_is_handled(monkeypatch, counting_ticker):
    """yfinance returns flat columns for one ticker, not a MultiIndex."""
    store = PortfolioStore()
    store.upsert(Holding.create("AMZN", 10, 90.0))
    monkeypatch.setattr(portfolio_risk.yf, "download", lambda tickers, **kw: _frame(120.0))

    report = PortfolioRiskAnalyzer(store).full_report()
    assert report["positions"][0]["current_price"] == pytest.approx(120.0)
    assert counting_ticker == []


def test_an_empty_portfolio_makes_no_request(monkeypatch, counting_ticker):
    def explode(*args, **kwargs):
        raise AssertionError("no holdings means no request")

    monkeypatch.setattr(portfolio_risk.yf, "download", explode)
    assert PortfolioRiskAnalyzer(PortfolioStore()).full_report()["positions"] == []


# ── Extended-hours quotes are session-gated ───────────────────────────────────

@pytest.fixture
def counting_feed():
    class Feed:
        def __init__(self):
            self.calls = []

        def get_current_data(self, symbol):
            self.calls.append(symbol)
            return {"session": "pre", "price": 105.0, "since_close_pct": 5.0}

    return Feed()


@pytest.mark.parametrize("session", ["regular", "closed"])
def test_no_feed_requests_outside_extended_hours(
    monkeypatch, store, counting_feed, session
):
    """The session is clock arithmetic, so this costs nothing to check first."""
    monkeypatch.setattr(data_feed, "get_market_session", lambda: session)
    monkeypatch.setattr(portfolio_risk.yf, "download", lambda t, **kw: _batch(TICKERS))

    report = PortfolioRiskAnalyzer(store, data_feed=counting_feed).full_report()
    assert counting_feed.calls == []
    assert all(p["session"] == session for p in report["positions"])


@pytest.mark.parametrize("session", ["pre", "after"])
def test_the_feed_is_queried_during_extended_hours(
    monkeypatch, store, counting_feed, session
):
    monkeypatch.setattr(data_feed, "get_market_session", lambda: session)
    monkeypatch.setattr(portfolio_risk.yf, "download", lambda t, **kw: _batch(TICKERS))

    PortfolioRiskAnalyzer(store, data_feed=counting_feed).full_report()
    assert sorted(counting_feed.calls) == sorted(TICKERS)


# ── Skip reason ───────────────────────────────────────────────────────────────

def test_the_skip_reason_names_what_went_wrong(monkeypatch, store):
    class FailingTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **kwargs):
            raise ConnectionError("blocked")

    monkeypatch.setattr(portfolio_risk.yf, "Ticker", FailingTicker)
    monkeypatch.setattr(portfolio_risk.yf, "download", lambda t, **kw: pd.DataFrame())

    report = PortfolioRiskAnalyzer(store).full_report()
    assert report["skipped_tickers"] == TICKERS
    assert report["skip_reason"] == "ConnectionError"


def test_no_skip_reason_when_nothing_was_skipped(monkeypatch, store):
    monkeypatch.setattr(portfolio_risk.yf, "download", lambda t, **kw: _batch(TICKERS))
    report = PortfolioRiskAnalyzer(store).full_report()
    assert report["skipped_tickers"] == []
    assert report["skip_reason"] is None
