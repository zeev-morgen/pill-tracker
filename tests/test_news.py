"""Pre-market news scan: parsing yfinance's shapes, freshness, caching, API."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from stock_monitor import dashboard, news_monitor
from stock_monitor.config import NotificationConfig
from stock_monitor.news_monitor import FRESH_WINDOW_HOURS, NewsMonitor, _normalize, _published_at
from stock_monitor.notifier import NotificationDispatcher
from stock_monitor.store import Holding, portfolio_store
from stock_monitor.webhook_server import create_webhook_app


def _hours_ago(hours):
    return datetime.now(timezone.utc) - timedelta(hours=hours)


@pytest.fixture(autouse=True)
def clean_portfolio():
    for holding in list(portfolio_store.all()):
        portfolio_store.delete(holding.ticker)
    yield
    for holding in list(portfolio_store.all()):
        portfolio_store.delete(holding.ticker)


# ── Timestamp parsing ─────────────────────────────────────────────────────────

def test_reads_the_modern_nested_pubdate():
    item = {"content": {"pubDate": "2026-08-01T12:00:00Z"}}
    assert _published_at(item) == datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def test_reads_the_legacy_unix_timestamp():
    epoch = int(datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc).timestamp())
    assert _published_at({"providerPublishTime": epoch}).year == 2026


def test_missing_or_unparsable_timestamps_return_none():
    assert _published_at({"content": {}}) is None
    assert _published_at({"content": {"pubDate": "not a date"}}) is None


# ── Normalization ─────────────────────────────────────────────────────────────

def test_normalizes_the_nested_shape():
    item = {"content": {
        "title": "Earnings beat",
        "provider": {"displayName": "Reuters"},
        "canonicalUrl": {"url": "https://example.com/a"},
        "pubDate": _hours_ago(2).isoformat(),
    }}
    result = _normalize(item, "AMZN")
    assert result["ticker"] == "AMZN"
    assert result["title"] == "Earnings beat"
    assert result["publisher"] == "Reuters"
    assert result["link"] == "https://example.com/a"
    assert result["age_hours"] == pytest.approx(2.0, abs=0.2)


def test_normalizes_the_flat_legacy_shape():
    item = {
        "title": "Old style",
        "publisher": "Bloomberg",
        "link": "https://example.com/b",
        "providerPublishTime": int(_hours_ago(1).timestamp()),
    }
    result = _normalize(item, "CF")
    assert (result["publisher"], result["link"]) == ("Bloomberg", "https://example.com/b")


def test_an_item_without_a_title_is_dropped():
    assert _normalize({"content": {"provider": {"displayName": "X"}}}, "AMZN") is None


# ── Scanning ──────────────────────────────────────────────────────────────────

def _fake_yf(news_by_ticker):
    class FakeTicker:
        def __init__(self, symbol):
            self._symbol = symbol

        @property
        def news(self):
            result = news_by_ticker[self._symbol]
            if isinstance(result, Exception):
                raise result
            return result

    return FakeTicker


def _item(title, hours_ago):
    return {"content": {"title": title, "provider": {"displayName": "P"},
                        "pubDate": _hours_ago(hours_ago).isoformat()}}


def test_scan_collects_headlines_for_every_holding(monkeypatch):
    portfolio_store.upsert(Holding.create("AMZN", 1, 100.0))
    portfolio_store.upsert(Holding.create("CF", 1, 50.0))
    monkeypatch.setattr(news_monitor.yf, "Ticker", _fake_yf({
        "AMZN": [_item("A", 1)], "CF": [_item("C", 2)],
    }))
    titles = {i["title"] for i in NewsMonitor(portfolio_store).scan()}
    assert titles == {"A", "C"}


def test_stale_headlines_are_filtered_out(monkeypatch):
    portfolio_store.upsert(Holding.create("AMZN", 1, 100.0))
    monkeypatch.setattr(news_monitor.yf, "Ticker", _fake_yf({
        "AMZN": [_item("fresh", 3), _item("stale", FRESH_WINDOW_HOURS + 5)],
    }))
    assert [i["title"] for i in NewsMonitor(portfolio_store).scan()] == ["fresh"]


def test_undated_headlines_are_kept(monkeypatch):
    """yfinance omits the timestamp often enough that dropping them empties the feed."""
    portfolio_store.upsert(Holding.create("AMZN", 1, 100.0))
    monkeypatch.setattr(news_monitor.yf, "Ticker", _fake_yf({
        "AMZN": [{"content": {"title": "no date", "provider": {"displayName": "P"}}}],
    }))
    assert [i["title"] for i in NewsMonitor(portfolio_store).scan()] == ["no date"]


def test_results_are_ordered_newest_first(monkeypatch):
    portfolio_store.upsert(Holding.create("AMZN", 1, 100.0))
    monkeypatch.setattr(news_monitor.yf, "Ticker", _fake_yf({
        "AMZN": [_item("older", 8), _item("newer", 1)],
    }))
    assert [i["title"] for i in NewsMonitor(portfolio_store).scan()] == ["newer", "older"]


def test_one_ticker_cannot_crowd_out_the_others(monkeypatch):
    portfolio_store.upsert(Holding.create("AMZN", 1, 100.0))
    portfolio_store.upsert(Holding.create("CF", 1, 50.0))
    monkeypatch.setattr(news_monitor.yf, "Ticker", _fake_yf({
        "AMZN": [_item(f"noise {n}", 1) for n in range(20)],
        "CF": [_item("signal", 2)],
    }))
    items = NewsMonitor(portfolio_store).scan()
    assert sum(1 for i in items if i["ticker"] == "AMZN") == news_monitor.MAX_ITEMS_PER_TICKER
    assert any(i["title"] == "signal" for i in items)


def test_a_failing_ticker_does_not_sink_the_whole_scan(monkeypatch):
    portfolio_store.upsert(Holding.create("AMZN", 1, 100.0))
    portfolio_store.upsert(Holding.create("CF", 1, 50.0))
    monkeypatch.setattr(news_monitor.yf, "Ticker", _fake_yf({
        "AMZN": RuntimeError("rate limited"), "CF": [_item("survived", 1)],
    }))
    assert [i["title"] for i in NewsMonitor(portfolio_store).scan()] == ["survived"]


def test_an_empty_portfolio_scans_nothing(monkeypatch):
    def explode(symbol):
        raise AssertionError("no holdings means no fetches")

    monkeypatch.setattr(news_monitor.yf, "Ticker", explode)
    assert NewsMonitor(portfolio_store).scan() == []


# ── Caching ───────────────────────────────────────────────────────────────────

def test_snapshot_scans_on_first_call_then_serves_the_cache(monkeypatch):
    portfolio_store.upsert(Holding.create("AMZN", 1, 100.0))
    calls = []

    class Counting:
        def __init__(self, symbol):
            calls.append(symbol)

        news = [_item("A", 1)]

    monkeypatch.setattr(news_monitor.yf, "Ticker", Counting)
    monitor = NewsMonitor(portfolio_store)
    first = monitor.snapshot()
    assert len(first["items"]) == 1 and first["scanned_at"] is not None
    monitor.snapshot()
    assert len(calls) == 1, "a warm cache must not re-fetch"


def test_max_age_zero_forces_a_rescan(monkeypatch):
    portfolio_store.upsert(Holding.create("AMZN", 1, 100.0))
    calls = []

    class Counting:
        def __init__(self, symbol):
            calls.append(symbol)

        news = [_item("A", 1)]

    monkeypatch.setattr(news_monitor.yf, "Ticker", Counting)
    monitor = NewsMonitor(portfolio_store)
    monitor.snapshot()
    monitor.snapshot(max_age_minutes=0)
    assert len(calls) == 2


# ── API ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    portfolio_store.upsert(Holding.create("AMZN", 1, 100.0))
    monkeypatch.setattr(news_monitor.yf, "Ticker", _fake_yf({"AMZN": [_item("headline", 1)]}))
    dashboard.set_news_monitor(NewsMonitor(portfolio_store))
    try:
        yield TestClient(create_webhook_app(NotificationDispatcher(NotificationConfig()), ""))
    finally:
        dashboard.set_news_monitor(None)


def test_api_returns_items_and_the_scan_time(client):
    body = client.get("/api/news").json()
    assert [i["title"] for i in body["items"]] == ["headline"]
    assert body["scanned_at"] is not None
    assert body["fresh_window_hours"] == FRESH_WINDOW_HOURS


def test_api_accepts_a_forced_rescan(client):
    assert client.get("/api/news", params={"max_age_minutes": 0}).status_code == 200


@pytest.mark.anyio
async def test_daily_scan_runs_the_scan(monkeypatch):
    portfolio_store.upsert(Holding.create("AMZN", 1, 100.0))
    monkeypatch.setattr(news_monitor.yf, "Ticker", _fake_yf({"AMZN": [_item("A", 1)]}))
    monitor = NewsMonitor(portfolio_store)
    await monitor.daily_scan()
    assert monitor.snapshot()["items"], "the scheduled job must fill the cache"


@pytest.fixture
def anyio_backend():
    return "asyncio"
