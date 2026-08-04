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

    monkeypatch.setattr(yf, "download", fake_download)
    monkeypatch.setattr(yf, "Ticker", FakeTicker)
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
