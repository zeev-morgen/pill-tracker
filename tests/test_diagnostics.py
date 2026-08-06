"""The diagnostics probe: it has to survive whatever Yahoo does to it.

It exists to be run when prices already look wrong, so every branch must
report rather than raise — a diagnostic that 500s tells you nothing.
"""

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from stock_monitor import dashboard
from stock_monitor.config import NotificationConfig
from stock_monitor.notifier import NotificationDispatcher
from stock_monitor.webhook_server import create_webhook_app


def _frame(rows=5, tz=None):
    idx = pd.bdate_range(end="2026-08-03", periods=rows, tz=tz)
    close = np.linspace(100.0, 104.0, rows)
    return pd.DataFrame({"Open": close, "High": close + 1,
                         "Low": close - 1, "Close": close,
                         "Volume": [1000] * rows}, index=idx)


@pytest.fixture
def client():
    return TestClient(create_webhook_app(NotificationDispatcher(NotificationConfig()), ""))


@pytest.fixture
def yf_stub(monkeypatch):
    """Installs stand-ins for the three Yahoo endpoints the probe reads."""
    import yfinance as yf

    state = {"download": _frame(), "history": _frame(),
             "fast": {"last_price": 104.0, "previous_close": 103.0, "last_volume": 1000}}

    def fake_download(tickers, **kwargs):
        result = state["download"]
        if isinstance(result, Exception):
            raise result
        return result

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **kwargs):
            result = state["history"]
            if isinstance(result, Exception):
                raise result
            return result

        @property
        def fast_info(self):
            result = state["fast"]
            if isinstance(result, Exception):
                raise result
            from types import SimpleNamespace
            return SimpleNamespace(**result)

    state["raw_chart"] = {"status": 200, "bars_today": 12, "premarket_bars_today": 12}

    def fake_raw_chart(symbol, host=dashboard.CHART_HOSTS[0]):
        result = state["raw_chart"]
        if isinstance(result, Exception):
            raise result
        return dict(result, ticker=symbol, host=host)

    monkeypatch.setattr(yf, "download", fake_download)
    monkeypatch.setattr(yf, "Ticker", FakeTicker)
    monkeypatch.setattr(dashboard, "raw_chart_probe", fake_raw_chart)
    return state


def test_it_reports_the_last_bars_from_each_source(client, yf_stub):
    body = client.get("/api/diagnostics/AMZN").json()

    assert body["ticker"] == "AMZN"
    assert body["market_session"] in ("pre", "regular", "after", "closed")
    assert [row[0] for row in body["download"]][-1] == "2026-08-03"
    assert [row[0] for row in body["history"]][-1] == "2026-08-03"
    assert body["fast_info"]["last_price"] == 104.0


def test_it_shows_when_the_sources_disagree(client, yf_stub):
    """The whole point: naming which endpoint is behind."""
    yf_stub["history"] = _frame()
    stale = _frame()
    stale.index = pd.bdate_range(end="2026-07-31", periods=len(stale))
    yf_stub["download"] = stale

    body = client.get("/api/diagnostics/AMZN").json()
    assert body["download"][-1][0] == "2026-07-31"
    assert body["history"][-1][0] == "2026-08-03"


def test_a_nan_close_is_reported_as_null_not_dropped(client, yf_stub):
    frame = _frame()
    frame.loc[frame.index[-1], "Close"] = float("nan")
    yf_stub["download"] = frame

    body = client.get("/api/diagnostics/AMZN").json()
    assert body["download"][-1] == ["2026-08-03", None], "a blank bar must be visible"


def test_a_timezone_aware_index_is_reported(client, yf_stub):
    yf_stub["download"] = _frame(tz="America/New_York")
    body = client.get("/api/diagnostics/AMZN").json()
    assert "New_York" in body["download_index_tz"]


@pytest.mark.parametrize("source", ["download", "history", "fast"])
def test_one_failing_source_does_not_sink_the_probe(client, yf_stub, source):
    yf_stub[source] = RuntimeError("429 Too Many Requests")
    body = client.get("/api/diagnostics/AMZN").json()

    key = {"download": "download", "history": "history", "fast": "fast_info"}[source]
    assert "RuntimeError" in str(body[key])
    # The other two still reported.
    assert body["server_utc"]


def test_an_empty_frame_is_labelled(client, yf_stub):
    yf_stub["download"] = pd.DataFrame()
    assert client.get("/api/diagnostics/AMZN").json()["download"] == "empty"


def test_a_frame_without_a_close_column_is_labelled(client, yf_stub):
    yf_stub["history"] = pd.DataFrame({"Open": [1.0, 2.0]})
    assert "no Close column" in client.get("/api/diagnostics/AMZN").json()["history"]


def test_a_bad_ticker_is_rejected(client, yf_stub):
    assert client.get("/api/diagnostics/not%20a%20ticker").status_code == 422


def test_the_probe_is_not_public():
    """It reveals deployment behaviour, so it sits behind the dashboard password."""
    from stock_monitor.webhook_server import _PUBLIC_PATHS

    assert not any("/api/diagnostics".startswith(p) for p in _PUBLIC_PATHS)


def test_the_daily_probes_survive_the_intraday_loop(client, yf_stub):
    """The intraday loop once shadowed the route's `period` parameter.

    Assigning to it inside the nested probe made it local to the whole
    function, so the daily probes above raised UnboundLocalError and reported
    a string where the caller expected bars.
    """
    body = client.get("/api/diagnostics/AMZN").json()

    assert isinstance(body["download"], list), body["download"]
    assert isinstance(body["history"], list), body["history"]
    for span in ("1d", "2d", "5d"):
        assert f"intraday_{span}" in body
        assert f"intraday_{span}_has_today" in body
    assert body["period_in_use"] == "2d"


def test_the_probe_uses_the_range_the_app_uses(client, yf_stub):
    """A probe on a different range confirms the bug instead of exposing it."""
    from stock_monitor.data_feed import INTRADAY_PERIOD

    assert client.get("/api/diagnostics/AMZN").json()["period_in_use"] == INTRADAY_PERIOD


# ── The two discriminators ────────────────────────────────────────────────────

def test_a_control_ticker_is_probed_alongside(client, yf_stub):
    """One ticker missing today's bars can be the ticker. SPY missing them can't."""
    body = client.get("/api/diagnostics/AMZN").json()

    assert body["control_ticker"] == dashboard.CONTROL_TICKER
    assert isinstance(body["control_intraday"], list), body["control_intraday"]
    assert body["control_intraday_has_today"] is False  # stub bars are from 2026-08-03
    assert body["control_intraday_newest"]


def test_the_control_does_not_probe_itself(client, yf_stub):
    """Asking about SPY has to compare against something other than SPY."""
    body = client.get(f"/api/diagnostics/{dashboard.CONTROL_TICKER}").json()

    assert body["control_ticker"] != dashboard.CONTROL_TICKER
    assert body["control_ticker"] == dashboard._CONTROL_FALLBACK


def test_the_control_probe_uses_the_range_the_app_uses(client, yf_stub, monkeypatch):
    """Comparing on a different range would make the control meaningless."""
    import yfinance as yf
    from stock_monitor.data_feed import INTRADAY_PERIOD

    seen = []
    original = yf.download

    def spy(tickers, **kwargs):
        seen.append((tickers, kwargs.get("period"), kwargs.get("interval")))
        return original(tickers, **kwargs)

    monkeypatch.setattr(yf, "download", spy)
    client.get("/api/diagnostics/AMZN")

    control = [row for row in seen if row[0] == [dashboard.CONTROL_TICKER]]
    assert control, seen
    assert control[0][1] == INTRADAY_PERIOD
    assert control[0][2] == "5m"


def test_the_raw_chart_response_is_included(client, yf_stub):
    """Yahoo answered directly, with yfinance out of the path."""
    body = client.get("/api/diagnostics/AMZN").json()

    assert body["raw_chart_query1"]["status"] == 200
    assert body["raw_chart_query1"]["premarket_bars_today"] == 12
    assert body["raw_chart_query1"]["ticker"] == "AMZN"


def test_both_yahoo_edges_are_probed(client, yf_stub):
    """One edge carrying today's bars while the other doesn't makes the fix a hostname."""
    body = client.get("/api/diagnostics/AMZN").json()

    hosts = {body[f"raw_chart_{h.split('.')[0]}"]["host"] for h in dashboard.CHART_HOSTS}
    assert hosts == set(dashboard.CHART_HOSTS)


def test_a_failing_raw_chart_does_not_sink_the_probe(client, yf_stub):
    yf_stub["raw_chart"] = RuntimeError("401 Unauthorized")
    body = client.get("/api/diagnostics/AMZN").json()

    assert "RuntimeError" in body["raw_chart_query1"]
    assert isinstance(body["download"], list), "the rest still reported"


# ── raw_chart_probe itself ────────────────────────────────────────────────────

@pytest.fixture
def chart_response(monkeypatch):
    """Stands in for Yahoo's chart endpoint. Yields a mutable payload."""
    from curl_cffi import requests as http

    from stock_monitor.data_feed import NYSE_TZ

    now = pd.Timestamp.now(tz=NYSE_TZ)
    premarket = now.normalize() + pd.Timedelta(hours=7)  # 07:00 ET today
    state = {
        "status": 200,
        "payload": {"chart": {"result": [{
            "meta": {"regularMarketPrice": 418.28, "range": "2d",
                     "currentTradingPeriod": {"pre": {"start": 1}}},
            "timestamp": [int(premarket.timestamp()) + i * 300 for i in range(6)],
        }], "error": None}},
    }

    state["headers"] = {"age": "0", "x-cache": "Miss from cloudfront"}

    class FakeResponse:
        status_code = property(lambda self: state["status"])
        headers = property(lambda self: state["headers"])

        def json(self):
            return state["payload"]

    monkeypatch.setattr(http, "get", lambda *a, **k: FakeResponse())
    return state


def test_it_counts_todays_premarket_bars(chart_response):
    out = dashboard.raw_chart_probe("AVGO")

    assert out["transport"] == "curl_cffi"
    assert out["status"] == 200
    assert out["bars"] == 6
    assert out["bars_today"] == 6
    assert out["premarket_bars_today"] == 6, "all six are before 09:30 ET"
    assert out["regular_market_price"] == 418.28
    assert "AVGO" in out["url"]


def test_the_host_is_part_of_the_url(chart_response):
    for host in dashboard.CHART_HOSTS:
        assert host in dashboard.raw_chart_probe("AVGO", host)["url"]


def test_bars_from_the_regular_session_are_not_counted_as_premarket(chart_response):
    from stock_monitor.data_feed import NYSE_TZ

    open_bell = pd.Timestamp.now(tz=NYSE_TZ).normalize() + pd.Timedelta(hours=10)
    result = chart_response["payload"]["chart"]["result"][0]
    result["timestamp"] = [int(open_bell.timestamp()) + i * 300 for i in range(4)]

    out = dashboard.raw_chart_probe("AVGO")
    assert out["bars_today"] == 4
    assert out["premarket_bars_today"] == 0


def test_yesterdays_bars_only_report_zero_for_today(chart_response):
    from stock_monitor.data_feed import NYSE_TZ

    yesterday = pd.Timestamp.now(tz=NYSE_TZ).normalize() - pd.Timedelta(hours=6)
    result = chart_response["payload"]["chart"]["result"][0]
    result["timestamp"] = [int(yesterday.timestamp()) + i * 300 for i in range(3)]

    out = dashboard.raw_chart_probe("AVGO")
    assert out["bars"] == 3
    assert out["bars_today"] == 0
    assert out["premarket_bars_today"] == 0
    assert out["newest"], "the newest bar is still named, so its date is visible"


def test_cache_headers_are_reported(chart_response):
    """A stale CDN copy explains a day-behind feed; `age` is how it shows."""
    chart_response["headers"] = {"age": "43200", "x-cache": "Hit from cloudfront",
                                 "irrelevant": "ignored"}

    out = dashboard.raw_chart_probe("AVGO")
    assert out["cache"]["age"] == "43200"
    assert out["cache"]["x-cache"] == "Hit from cloudfront"
    assert "irrelevant" not in out["cache"]


def test_missing_cache_headers_are_omitted_not_nulled(chart_response):
    chart_response["headers"] = {}
    assert dashboard.raw_chart_probe("AVGO")["cache"] == {}


def test_an_error_payload_is_reported_not_raised(chart_response):
    chart_response["status"] = 429
    chart_response["payload"] = {"chart": {"result": None,
                                           "error": {"code": "Too Many Requests"}}}

    out = dashboard.raw_chart_probe("AVGO")
    assert out["status"] == 429
    assert "Too Many Requests" in out["error"]
