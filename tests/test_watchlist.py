"""Editable watchlist: the store, the API, and how the monitor reads it."""

from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from stock_monitor import dashboard
from stock_monitor.config import AlertConfig, NotificationConfig, StockConfig, default_alerts
from stock_monitor.notifier import NotificationDispatcher
from stock_monitor.store import HoldingError, watchlist_store
from stock_monitor.webhook_server import create_webhook_app


@pytest.fixture(autouse=True)
def clean_watchlist():
    watchlist_store._symbols.clear()
    yield
    watchlist_store._symbols.clear()


@pytest.fixture
def client():
    return TestClient(create_webhook_app(NotificationDispatcher(NotificationConfig()), ""))


# ── Store ─────────────────────────────────────────────────────────────────────

def test_symbols_are_normalized_and_sorted():
    watchlist_store.add("msft")
    watchlist_store.add(" aapl ")
    assert watchlist_store.all() == ["AAPL", "MSFT"]


def test_adding_the_same_symbol_twice_is_idempotent():
    watchlist_store.add("AAPL")
    watchlist_store.add("aapl")
    assert watchlist_store.all() == ["AAPL"]


@pytest.mark.parametrize("symbol", ["", "   ", "A" * 17, "AA PL", "AA;PL"])
def test_invalid_symbols_are_rejected(symbol):
    with pytest.raises(HoldingError):
        watchlist_store.add(symbol)


def test_index_and_class_share_symbols_are_accepted():
    # ^GSPC (an index) and BRK-B / BF.B (share classes) are all legitimate.
    for symbol in ("^GSPC", "BRK-B", "BF.B"):
        assert watchlist_store.add(symbol) == symbol


def test_remove_reports_whether_anything_was_removed():
    watchlist_store.add("AAPL")
    assert watchlist_store.remove("aapl") is True
    assert watchlist_store.remove("AAPL") is False
    assert watchlist_store.all() == []


def test_seed_populates_an_empty_watchlist():
    watchlist_store.seed(["AMZN", "CF"])
    assert watchlist_store.all() == ["AMZN", "CF"]


def test_seed_never_overwrites_the_users_edits():
    watchlist_store.add("TSLA")
    watchlist_store.seed(["AMZN", "CF"])
    assert watchlist_store.all() == ["TSLA"]


def test_seed_skips_invalid_config_symbols_instead_of_crashing():
    watchlist_store.seed(["AMZN", "not a ticker", "CF"])
    assert watchlist_store.all() == ["AMZN", "CF"]


# ── API ───────────────────────────────────────────────────────────────────────

def test_api_add_and_list(client):
    response = client.post("/api/watchlist", json={"symbol": "nvda"})
    assert response.status_code == 201
    assert response.json()["added"] == "NVDA"
    assert client.get("/api/watchlist").json()["symbols"] == ["NVDA"]


def test_api_rejects_a_bad_symbol(client):
    assert client.post("/api/watchlist", json={"symbol": "no way"}).status_code == 422


def test_api_delete_returns_the_remaining_symbols(client):
    client.post("/api/watchlist", json={"symbol": "NVDA"})
    client.post("/api/watchlist", json={"symbol": "AMZN"})
    assert client.delete("/api/watchlist/NVDA").json()["symbols"] == ["AMZN"]


def test_api_delete_of_an_unwatched_symbol_is_404(client):
    assert client.delete("/api/watchlist/NOPE").status_code == 404


# ── Monitor integration ───────────────────────────────────────────────────────

def _watched(config_symbols):
    """Run watched_stocks() against a stand-in app, without building the real one."""
    from stock_monitor.main import StockMonitorApp

    config = SimpleNamespace(stocks=[
        StockConfig(symbol=s, alerts=[AlertConfig(type="price_change_pct", threshold_pct=9.9)])
        for s in config_symbols
    ])
    return StockMonitorApp.watched_stocks(SimpleNamespace(config=config))


def test_monitor_falls_back_to_config_while_the_watchlist_is_empty():
    assert [s.symbol for s in _watched(["AMZN", "CF"])] == ["AMZN", "CF"]


def test_watchlist_overrides_config_once_it_has_entries():
    watchlist_store.add("TSLA")
    assert [s.symbol for s in _watched(["AMZN", "CF"])] == ["TSLA"]


def test_a_configured_symbol_keeps_its_tuned_alert_rules():
    watchlist_store.add("AMZN")
    assert _watched(["AMZN"])[0].alerts[0].threshold_pct == 9.9


def test_a_symbol_added_from_the_ui_gets_default_alerts():
    watchlist_store.add("TSLA")
    alerts = _watched(["AMZN"])[0].alerts
    assert [a.type for a in alerts] == [a.type for a in default_alerts()]
    assert alerts, "a runtime symbol with no alerts could never fire one"


# ── Price cache ───────────────────────────────────────────────────────────────

def test_pruning_drops_quotes_for_symbols_no_longer_watched():
    dashboard._price_cache.clear()
    for symbol in ("AMZN", "TSLA"):
        dashboard.update_price_cache({
            "symbol": symbol, "price": 1.0, "change_pct": 0.0,
            "volume": 1, "session": "regular",
        })
    dashboard.prune_price_cache(["AMZN"])
    assert list(dashboard._price_cache) == ["AMZN"]
    dashboard._price_cache.clear()


# ── Rejecting symbols that do not exist ───────────────────────────────────────

def test_an_unknown_symbol_is_rejected_with_a_reason(client, monkeypatch):
    """A misspelled ticker used to be stored and then silently never appear,
    which is indistinguishable from the feature being broken."""
    from stock_monitor import dashboard

    monkeypatch.setattr(dashboard, "symbol_is_known", lambda symbol: False)
    response = client.post("/api/watchlist", json={"symbol": "ESTL.TA"})

    assert response.status_code == 422
    assert "ESTL.TA" in response.json()["detail"]
    assert "ESTL.TA" not in watchlist_store.all(), "a rejected symbol must not be stored"


def test_the_rejection_mentions_the_tase_suffix(client, monkeypatch):
    from stock_monitor import dashboard

    monkeypatch.setattr(dashboard, "symbol_is_known", lambda symbol: False)
    detail = client.post("/api/watchlist", json={"symbol": "NOPE"}).json()["detail"]

    assert ".TA" in detail


def test_a_known_symbol_is_added_and_marked_verified(client, monkeypatch):
    from stock_monitor import dashboard

    monkeypatch.setattr(dashboard, "symbol_is_known", lambda symbol: True)
    body = client.post("/api/watchlist", json={"symbol": "TEVA.TA"}).json()

    assert body["added"] == "TEVA.TA"
    assert body["verified"] is True


def test_an_unreachable_source_does_not_block_a_valid_symbol(client, monkeypatch):
    """Yahoo throttling says nothing about whether the ticker is real."""
    from stock_monitor import dashboard

    monkeypatch.setattr(dashboard, "symbol_is_known", lambda symbol: None)
    response = client.post("/api/watchlist", json={"symbol": "AVGO"})

    assert response.status_code == 201
    assert response.json()["verified"] is False, "added on trust, and says so"
    assert "AVGO" in watchlist_store.all()


def test_a_malformed_symbol_is_rejected_before_the_network(client, monkeypatch):
    """No point asking Yahoo about a string that cannot be a ticker."""
    from stock_monitor import dashboard

    monkeypatch.setattr(
        dashboard, "symbol_is_known",
        lambda symbol: pytest.fail("must not reach the data source"),
    )
    assert client.post("/api/watchlist", json={"symbol": "bad symbol!"}).status_code == 422


def test_verification_distinguishes_empty_from_broken(monkeypatch, real_symbol_check):
    """False means 'no such symbol'; None means 'could not find out'."""
    import yfinance as yf

    symbol_is_known = real_symbol_check

    class Empty:
        def history(self, **kwargs):
            return pd.DataFrame()

    class Boom:
        def history(self, **kwargs):
            raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(yf, "Ticker", lambda s: Empty())
    assert symbol_is_known("ESTL.TA") is False

    monkeypatch.setattr(yf, "Ticker", lambda s: Boom())
    assert symbol_is_known("AVGO") is None


def test_a_frame_of_only_blank_closes_counts_as_unknown(monkeypatch, real_symbol_check):
    import numpy as np
    import yfinance as yf

    symbol_is_known = real_symbol_check

    blank = pd.DataFrame({"Close": [np.nan, np.nan]})
    monkeypatch.setattr(yf, "Ticker", lambda s: type("T", (), {
        "history": lambda self, **kw: blank})())

    assert symbol_is_known("ESTL.TA") is False
