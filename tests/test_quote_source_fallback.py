"""Choosing between Tiingo and Yahoo at the two call sites.

The subtlety worth testing: Yahoo does not fail. It answers 200 with the
previous session's prices. So "try Tiingo, fall back on error" would never fall
back and, more importantly, preferring Yahoo and falling back to Tiingo would
never reach Tiingo. The switch has to be on whether the data is from today.
"""

from datetime import date

import pandas as pd
import pytest

from stock_monitor import portfolio_risk, tiingo
from stock_monitor.data_feed import StockDataFeed


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv(tiingo.ENV_KEY, "test-key")


@pytest.fixture
def unconfigured(monkeypatch):
    monkeypatch.delenv(tiingo.ENV_KEY, raising=False)


def _quote(as_of, price=100.0):
    return {"price": price, "regular_close": 99.0, "session": "regular", "as_of": as_of}


# ── The portfolio path ────────────────────────────────────────────────────────

def test_yahoo_alone_when_tiingo_is_not_configured(unconfigured, monkeypatch):
    analyzer = portfolio_risk.PortfolioRiskAnalyzer(None)
    monkeypatch.setattr(analyzer, "_intraday_quotes",
                        lambda tickers, session: {"AVGO": _quote(None)})
    monkeypatch.setattr(tiingo, "get_quotes",
                        lambda t: pytest.fail("must not be called without a key"))

    assert analyzer._live_quotes(["AVGO"], "regular") == {"AVGO": _quote(None)}


def test_a_fresh_tiingo_quote_wins(configured, monkeypatch):
    today = portfolio_risk._market_today()
    analyzer = portfolio_risk.PortfolioRiskAnalyzer(None)
    monkeypatch.setattr(analyzer, "_intraday_quotes",
                        lambda tickers, session: {"AVGO": _quote(date(2020, 1, 1), 1.0)})
    monkeypatch.setattr(tiingo, "get_quotes", lambda t: {"AVGO": _quote(today, 421.0)})

    assert analyzer._live_quotes(["AVGO"], "regular")["AVGO"]["price"] == 421.0


def test_yahoo_is_not_even_asked_when_tiingo_covers_everything(configured, monkeypatch):
    """One provider answering fully means one request, not two."""
    today = portfolio_risk._market_today()
    analyzer = portfolio_risk.PortfolioRiskAnalyzer(None)
    monkeypatch.setattr(analyzer, "_intraday_quotes",
                        lambda tickers, session: pytest.fail("Yahoo should not be asked"))
    monkeypatch.setattr(tiingo, "get_quotes",
                        lambda t: {s: _quote(today) for s in ("AVGO", "JPM")})

    assert set(analyzer._live_quotes(["AVGO", "JPM"], "regular")) == {"AVGO", "JPM"}


def test_a_stale_tiingo_quote_is_discarded_for_yahoos(configured, monkeypatch):
    """A quote from an earlier session is not a live price from either source."""
    analyzer = portfolio_risk.PortfolioRiskAnalyzer(None)
    monkeypatch.setattr(analyzer, "_intraday_quotes",
                        lambda tickers, session: {"AVGO": _quote(None, 418.0)})
    monkeypatch.setattr(tiingo, "get_quotes",
                        lambda t: {"AVGO": _quote(date(2020, 1, 1), 1.0)})

    assert analyzer._live_quotes(["AVGO"], "regular")["AVGO"]["price"] == 418.0


def test_the_two_sources_are_merged_per_ticker(configured, monkeypatch):
    """A thinly traded name Tiingo cannot price still gets Yahoo's answer."""
    today = portfolio_risk._market_today()
    analyzer = portfolio_risk.PortfolioRiskAnalyzer(None)
    asked = []

    def yahoo(tickers, session):
        asked.extend(tickers)
        return {t: _quote(None, 50.0) for t in tickers}

    monkeypatch.setattr(analyzer, "_intraday_quotes", yahoo)
    monkeypatch.setattr(tiingo, "get_quotes", lambda t: {"AVGO": _quote(today, 421.0)})

    merged = analyzer._live_quotes(["AVGO", "ARYT"], "regular")
    assert asked == ["ARYT"], "only the uncovered ticker is asked of Yahoo"
    assert merged["AVGO"]["price"] == 421.0
    assert merged["ARYT"]["price"] == 50.0


def test_a_tiingo_outage_falls_through_to_yahoo(configured, monkeypatch):
    analyzer = portfolio_risk.PortfolioRiskAnalyzer(None)
    monkeypatch.setattr(analyzer, "_intraday_quotes",
                        lambda tickers, session: {"AVGO": _quote(None, 418.0)})
    monkeypatch.setattr(tiingo, "get_quotes", lambda t: {})

    assert analyzer._live_quotes(["AVGO"], "regular")["AVGO"]["price"] == 418.0


def test_no_tickers_asks_nobody(configured, monkeypatch):
    analyzer = portfolio_risk.PortfolioRiskAnalyzer(None)
    monkeypatch.setattr(tiingo, "get_quotes", lambda t: pytest.fail("no request"))
    assert analyzer._live_quotes([], "regular") == {}


# ── The monitor path ──────────────────────────────────────────────────────────

def test_the_monitor_prefers_tiingo(configured, monkeypatch):
    feed = StockDataFeed()
    monkeypatch.setattr(tiingo, "get_monitor_quotes",
                        lambda s: {"AVGO": {"symbol": "AVGO", "price": 421.0}})
    monkeypatch.setattr(portfolio_risk.yf, "download",
                        lambda *a, **k: pytest.fail("Yahoo should not be asked"))

    assert feed.get_current_data_batch(["AVGO"])["AVGO"]["price"] == 421.0


def test_symbols_tiingo_cannot_price_still_reach_yahoo(configured, monkeypatch):
    import yfinance as yf

    feed = StockDataFeed()
    asked = {}

    def fake_download(symbols, **kwargs):
        asked["symbols"] = symbols
        return pd.DataFrame()

    monkeypatch.setattr(yf, "download", fake_download)
    monkeypatch.setattr(tiingo, "get_monitor_quotes",
                        lambda s: {"AVGO": {"symbol": "AVGO", "price": 421.0}})

    result = feed.get_current_data_batch(["AVGO", "ARYT"])
    assert asked["symbols"] == ["ARYT"]
    assert result["AVGO"]["price"] == 421.0, "Tiingo's answer survives an empty Yahoo batch"


def test_a_yahoo_failure_does_not_discard_tiingos_answers(configured, monkeypatch):
    """The early returns used to hand back {} and throw the good data away."""
    import yfinance as yf

    feed = StockDataFeed()

    def boom(*args, **kwargs):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(yf, "download", boom)
    monkeypatch.setattr(tiingo, "get_monitor_quotes",
                        lambda s: {"AVGO": {"symbol": "AVGO", "price": 421.0}})

    assert feed.get_current_data_batch(["AVGO", "ARYT"])["AVGO"]["price"] == 421.0


def test_the_monitor_is_unchanged_without_a_key(unconfigured, monkeypatch):
    import yfinance as yf

    feed = StockDataFeed()
    asked = {}

    def fake_download(symbols, **kwargs):
        asked["symbols"] = symbols
        return pd.DataFrame()

    monkeypatch.setattr(yf, "download", fake_download)

    feed.get_current_data_batch(["AVGO", "ARYT"])
    assert asked["symbols"] == ["AVGO", "ARYT"], "every symbol still goes to Yahoo"
