"""The news fallback chain.

The regression this module exists to prevent: in October 2026 Yahoo retired the
endpoint behind ``Ticker.news`` and began answering 404 with a JSON body.
yfinance parsed it, found no stream, and returned an empty list — no exception,
no log line. Every AI analysis quietly stopped carrying news for over a month.

Hence the rule under test: an empty answer is not an answer. A source that
returns nothing is tried past, not believed.
"""

from datetime import datetime, timedelta, timezone

import pytest
import requests

from stock_monitor import news


def _hours_ago(hours):
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def _nested(title, hours_ago=1, publisher="Reuters"):
    return {"content": {"title": title, "provider": {"displayName": publisher},
                        "pubDate": _hours_ago(hours_ago).isoformat()}}


def _flat(title, hours_ago=1, publisher="Bloomberg"):
    return {"title": title, "publisher": publisher, "link": "https://example.com/x",
            "providerPublishTime": _hours_ago(hours_ago).timestamp()}


def _sources(monkeypatch, **by_name):
    """Install a chain where each named source returns a list or raises."""
    def make(result):
        def source(ticker, limit):
            if isinstance(result, Exception):
                raise result
            return result
        return source

    monkeypatch.setattr(news, "SOURCES", [
        (name, make(by_name[name])) for name in ("ticker", "search", "rss")
        if name in by_name
    ])


# ── The chain ─────────────────────────────────────────────────────────────────

def test_the_first_source_that_answers_wins(monkeypatch):
    _sources(monkeypatch, ticker=[_nested("from yahoo")],
             search=[_flat("should not be reached")])
    result = news.fetch("AVGO")

    assert [i["title"] for i in result.items] == ["from yahoo"]
    assert result.source == "ticker"


def test_an_empty_first_source_is_not_believed(monkeypatch):
    """The whole point: [] means broken far more often than it means quiet."""
    _sources(monkeypatch, ticker=[], search=[_flat("real headline")])
    result = news.fetch("AVGO")

    assert [i["title"] for i in result.items] == ["real headline"]
    assert result.source == "search"


def test_a_raising_source_falls_through_to_the_next(monkeypatch):
    _sources(monkeypatch, ticker=RuntimeError("404"), search=[],
             rss=[_flat("from rss")])
    result = news.fetch("AVGO")

    assert result.source == "rss"
    assert result.items[0]["title"] == "from rss"


def test_every_source_failing_is_reported_as_unavailable(monkeypatch):
    _sources(monkeypatch, ticker=RuntimeError("boom"), search=TimeoutError("slow"),
             rss=ConnectionError("dns"))
    result = news.fetch("AVGO")

    assert result.items == []
    assert result.available is False, "a total failure must not look like quiet news"
    assert "ticker" in result.error and "search" in result.error


def test_a_ticker_nobody_writes_about_is_not_called_an_outage(monkeypatch):
    """Most Tel Aviv small-caps have no Yahoo coverage at all.

    Every source answered; none had anything. Reporting that as a broken feed
    would send the user chasing an outage that is really just a quiet ticker.
    """
    _sources(monkeypatch, ticker=[], search=[], rss=[])
    result = news.fetch("ARYT.TA")

    assert result.items == []
    assert result.available is True
    assert "empty" in result.error, "the detail is still recorded for diagnostics"


def test_nothing_recent_anywhere_counts_as_answered(monkeypatch):
    """Stale-only is a real answer: the feed works, the stock is just quiet."""
    _sources(monkeypatch, ticker=[_nested("old news", hours_ago=90)],
             search=[_flat("also old", hours_ago=200)])
    result = news.fetch("AVGO")

    assert result.items == []
    assert result.available is True, "a live feed with quiet news is not a failure"
    assert result.source == "ticker"


def test_a_stale_only_source_still_falls_through(monkeypatch):
    """Old headlines are not what was asked for, so the next source gets a turn."""
    _sources(monkeypatch, ticker=[_nested("old news", hours_ago=90)],
             search=[_flat("newer")])
    result = news.fetch("AVGO")

    assert [i["title"] for i in result.items] == ["newer"]
    assert result.source == "search"


def test_the_error_names_each_source_that_failed(monkeypatch):
    _sources(monkeypatch, ticker=ValueError("bad json"), search=TimeoutError("slow"))
    assert "ValueError" in news.fetch("AVGO").error
    assert "TimeoutError" in news.fetch("AVGO").error


# ── Filtering, ordering, deduplication ────────────────────────────────────────

def test_stale_items_are_dropped_but_fresh_ones_survive(monkeypatch):
    _sources(monkeypatch, ticker=[_nested("fresh", 2),
                                  _nested("stale", news.FRESH_WINDOW_HOURS + 10)])
    assert [i["title"] for i in news.fetch("AVGO").items] == ["fresh"]


def test_undated_items_are_kept(monkeypatch):
    """Yahoo omits the timestamp often enough that dropping them empties the feed."""
    _sources(monkeypatch, ticker=[{"content": {"title": "no date"}}])
    assert [i["title"] for i in news.fetch("AVGO").items] == ["no date"]


def test_newest_first(monkeypatch):
    _sources(monkeypatch, ticker=[_nested("older", 9), _nested("newer", 1)])
    assert [i["title"] for i in news.fetch("AVGO").items] == ["newer", "older"]


def test_the_same_headline_twice_is_returned_once(monkeypatch):
    """Syndicated wire copy arrives from several providers at once."""
    _sources(monkeypatch, ticker=[_nested("Broadcom beats"), _nested("broadcom BEATS")])
    assert len(news.fetch("AVGO").items) == 1


def test_the_limit_is_honoured(monkeypatch):
    _sources(monkeypatch, ticker=[_nested(f"item {n}", 1) for n in range(20)])
    assert len(news.fetch("AVGO", limit=3).items) == 3


def test_the_window_can_be_disabled(monkeypatch):
    _sources(monkeypatch, ticker=[_nested("ancient", 500)])
    assert len(news.fetch("AVGO", window_hours=None).items) == 1


# ── Individual sources ────────────────────────────────────────────────────────

def test_the_search_source_asks_for_news_about_the_ticker(monkeypatch):
    asked = {}

    class FakeSearch:
        def __init__(self, query, **kwargs):
            asked["query"] = query
            asked.update(kwargs)
            self.news = [_flat("found")]

    monkeypatch.setattr(news.yf, "Search", FakeSearch)
    items = news._from_search("AVGO", 4)

    assert asked["query"] == "AVGO"
    assert asked["news_count"] >= 4
    assert items[0]["title"] == "found"


def test_the_search_source_does_not_wait_half_a_minute(monkeypatch):
    """Someone is watching a Telegram chat while this runs."""
    asked = {}

    class FakeSearch:
        def __init__(self, query, **kwargs):
            asked.update(kwargs)
            self.news = []

    monkeypatch.setattr(news.yf, "Search", FakeSearch)
    news._from_search("AVGO", 4)

    assert asked["timeout"] <= 15


RSS_BODY = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Broadcom lifts guidance</title>
    <link>https://finance.yahoo.com/news/a.html</link>
    <pubDate>Tue, 11 Nov 2026 13:05:00 +0000</pubDate>
  </item>
  <item>
    <title></title>
    <link>https://finance.yahoo.com/news/empty.html</link>
  </item>
</channel></rss>"""


class _Resp:
    def __init__(self, content=RSS_BODY, status=200):
        self.content, self.status_code = content, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def test_the_rss_source_parses_titles_links_and_dates(monkeypatch):
    monkeypatch.setattr(news, "_http_get", lambda url, **kw: _Resp())
    items = news._from_rss("AVGO", 5)

    assert len(items) == 1, "the untitled item must be dropped"
    assert items[0]["title"] == "Broadcom lifts guidance"
    assert items[0]["link"].endswith("a.html")
    assert items[0]["providerPublishTime"] is not None


def test_the_rss_source_asks_for_the_right_ticker(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen["params"] = kwargs.get("params")
        return _Resp()

    monkeypatch.setattr(news, "_http_get", fake_get)
    news._from_rss("ESLT.TA", 5)

    assert seen["url"] == news.RSS_URL
    assert seen["params"]["s"] == "ESLT.TA"


def test_an_http_error_from_rss_propagates_to_the_chain(monkeypatch):
    """So fetch() records it as a failed source instead of silent emptiness."""
    monkeypatch.setattr(news, "_http_get", lambda url, **kw: _Resp(b"", 404))
    with pytest.raises(requests.HTTPError):
        news._from_rss("AVGO", 5)


def test_malformed_rss_raises_rather_than_returning_nothing(monkeypatch):
    monkeypatch.setattr(news, "_http_get", lambda url, **kw: _Resp(b"<not xml"))
    with pytest.raises(Exception):
        news._from_rss("AVGO", 5)


# ── Diagnostics ───────────────────────────────────────────────────────────────

def test_status_reports_the_version_and_the_live_source(monkeypatch):
    _sources(monkeypatch, ticker=[_nested("probe")])
    report = news.status()

    assert report["source"] == "ticker"
    assert report["items"] == 1
    assert report["healthy"] is True
    assert report["yfinance_version"]
    assert report["error"] == ""


def test_status_reports_the_failure_when_nothing_answers(monkeypatch):
    _sources(monkeypatch, ticker=RuntimeError("dead"))
    report = news.status()

    assert report["source"] == ""
    assert report["healthy"] is False
    assert "RuntimeError" in report["error"]


def test_the_diagnostics_route_serves_the_status(monkeypatch):
    """So the live host can be asked what it sees, instead of being guessed at."""
    from fastapi.testclient import TestClient

    from stock_monitor.config import NotificationConfig
    from stock_monitor.notifier import NotificationDispatcher
    from stock_monitor.webhook_server import create_webhook_app

    _sources(monkeypatch, ticker=[_nested("probe")])
    client = TestClient(create_webhook_app(
        NotificationDispatcher(NotificationConfig()), ""))

    body = client.get("/api/diagnostics/news").json()
    assert body["healthy"] is True
    assert body["yfinance_version"]
    assert body["source"] == "ticker"


def test_status_calls_a_silently_empty_feed_unhealthy(monkeypatch):
    """The October 2026 failure: 200 OK, no items, no error, for every ticker.

    The probe ticker always has coverage, so this is the one place where an
    empty answer is taken as proof of breakage rather than of a quiet day.
    """
    _sources(monkeypatch, ticker=[], search=[], rss=[])
    report = news.status()

    assert report["healthy"] is False
    assert report["probe_ticker"] == news.PROBE_TICKER
