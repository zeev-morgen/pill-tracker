"""Batched quotes for the polling loop.

Quoting each watched symbol separately cost a fast_info call plus a history
call apiece — 26 requests a minute for thirteen symbols, 1,560 an hour. Yahoo
rate-limited the host for it (YFRateLimitError showed up in the dashboard), and
a throttled account then fails to return the very pre-market bars the tab is
waiting for. So the request count is what these tests are about.
"""

import asyncio
from datetime import datetime

import numpy as np
import pandas as pd
import pytest
import pytz

from stock_monitor import data_feed
from stock_monitor.config import AppConfig, NotificationConfig, StockConfig
from stock_monitor.data_feed import StockDataFeed
from stock_monitor.main import StockMonitorApp

NY = pytz.timezone("America/New_York")
SYMBOLS = ["AVGO", "JPM", "MU", "ORCL", "SOFI"]


def _series(day, price=100.0, volume=1000, include_extended=True):
    """A 5-minute session: regular bars, optionally an after-hours bar."""
    stamps = [f"{day} {h:02d}:{m:02d}" for h in range(9, 16) for m in (30, 55) if
              not (h == 9 and m == 30) or True]
    stamps = [s for s in stamps if "09:30" <= s.split(" ")[1] < "16:00"]
    if include_extended:
        stamps.append(f"{day} 18:00")
    index = pd.DatetimeIndex([pd.Timestamp(s, tz=NY) for s in stamps])
    closes = np.full(len(index), price)
    return pd.DataFrame({
        "Open": closes, "High": closes + 1, "Low": closes - 1,
        "Close": closes, "Volume": [volume] * len(index),
    }, index=index)


def _batch(tickers, frame):
    return pd.concat({t: frame for t in list(tickers)}, axis=1)


@pytest.fixture
def counting(monkeypatch):
    """Counts every kind of Yahoo call the feed can make."""
    calls = {"download": 0, "fast_info": 0, "history": 0}
    frame = pd.concat([_series("2026-08-05", 100.0), _series("2026-08-06", 105.0)])

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def fast_info(self):
            calls["fast_info"] += 1

            class FastInfo:
                last_price = 100.0
                previous_close = 99.0
                last_volume = 500
                day_high = 101.0
                day_low = 99.0
                open = 100.0

            return FastInfo()

        def history(self, **kwargs):
            calls["history"] += 1
            return frame

    def download(tickers, **kwargs):
        calls["download"] += 1
        return _batch(tickers, frame)

    monkeypatch.setattr(data_feed.yf, "Ticker", FakeTicker)
    monkeypatch.setattr(data_feed.yf, "download", download)
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "pre")
    calls["frame"] = frame
    return calls


# ── Request volume ────────────────────────────────────────────────────────────

def test_one_request_covers_every_symbol(counting):
    quotes = StockDataFeed().get_current_data_batch(SYMBOLS)

    assert sorted(quotes) == sorted(SYMBOLS)
    assert counting["download"] == 1
    assert counting["fast_info"] == 0 and counting["history"] == 0


def test_a_whole_monitor_cycle_costs_one_request(counting, monkeypatch):
    config = AppConfig(
        stocks=[StockConfig(symbol=s, alerts=[]) for s in SYMBOLS],
        notifications=NotificationConfig(),
    )
    app = StockMonitorApp(config)
    asyncio.run(app.monitor_cycle())

    assert counting["download"] == 1
    assert counting["fast_info"] == 0, "no per-symbol quote when the batch covers it"
    assert counting["history"] == 0


def test_an_empty_watchlist_makes_no_request(counting):
    assert StockDataFeed().get_current_data_batch([]) == {}
    assert counting["download"] == 0


# ── Fallbacks ─────────────────────────────────────────────────────────────────

def test_a_failed_batch_leaves_the_per_symbol_path_to_it(counting, monkeypatch):
    def boom(tickers, **kwargs):
        raise RuntimeError("YFRateLimitError")

    monkeypatch.setattr(data_feed.yf, "download", boom)
    assert StockDataFeed().get_current_data_batch(SYMBOLS) == {}


def test_the_cycle_falls_back_for_symbols_the_batch_missed(counting, monkeypatch):
    covered = SYMBOLS[:2]
    monkeypatch.setattr(
        data_feed.yf, "download",
        lambda t, **kw: _batch(covered, counting["frame"]),
    )
    config = AppConfig(
        stocks=[StockConfig(symbol=s, alerts=[]) for s in SYMBOLS],
        notifications=NotificationConfig(),
    )
    asyncio.run(StockMonitorApp(config).monitor_cycle())

    # One fast_info per uncovered symbol, none for the covered ones.
    assert counting["fast_info"] == len(SYMBOLS) - len(covered)


def test_a_symbol_with_no_priced_bars_is_omitted(counting, monkeypatch):
    blank = counting["frame"].copy()
    blank["Close"] = np.nan
    monkeypatch.setattr(data_feed.yf, "download", lambda t, **kw: _batch(t, blank))
    assert StockDataFeed().get_current_data_batch(SYMBOLS) == {}


# ── Derived values ────────────────────────────────────────────────────────────

def test_the_payload_matches_what_callers_expect(counting):
    quote = StockDataFeed().get_current_data_batch(["AVGO"])["AVGO"]

    for key in ("symbol", "price", "prev_close", "regular_close", "change_pct",
                "since_close_pct", "volume", "session", "timestamp"):
        assert key in quote, key
    assert quote["symbol"] == "AVGO"
    assert quote["session"] == "pre"


def test_nothing_non_finite_leaves_the_batch(counting):
    import json

    quote = StockDataFeed().get_current_data_batch(["AVGO"])["AVGO"]
    numbers = {k: v for k, v in quote.items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)}
    json.dumps(numbers, allow_nan=False)


def test_before_the_open_the_reference_is_the_last_close(counting, monkeypatch):
    """Pre-market moves are measured from the previous session's close.

    Reading one close further back made the day's change wrong all morning.
    """
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "pre")
    frame = pd.concat([_series("2026-08-04", 90.0), _series("2026-08-05", 100.0)])
    frame = pd.concat([frame, pd.DataFrame(
        {"Open": [110.0], "High": [110.0], "Low": [110.0],
         "Close": [110.0], "Volume": [10]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-08-06 08:00", tz=NY)]))])
    monkeypatch.setattr(data_feed.yf, "download", lambda t, **kw: _batch(t, frame))

    quote = StockDataFeed().get_current_data_batch(["AVGO"])["AVGO"]
    assert quote["regular_close"] == pytest.approx(100.0)
    assert quote["prev_close"] == pytest.approx(100.0), "not the session before it"
    assert quote["change_pct"] == pytest.approx(10.0)


def test_after_the_close_the_day_is_measured_against_the_session_before(
    counting, monkeypatch
):
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "after")
    frame = pd.concat([_series("2026-08-05", 90.0, include_extended=False),
                       _series("2026-08-06", 100.0)])
    monkeypatch.setattr(data_feed.yf, "download", lambda t, **kw: _batch(t, frame))

    quote = StockDataFeed().get_current_data_batch(["AVGO"])["AVGO"]
    assert quote["regular_close"] == pytest.approx(100.0)   # today's close
    assert quote["prev_close"] == pytest.approx(90.0)       # yesterday's


def test_volume_counts_only_todays_regular_session(counting, monkeypatch):
    """What the spike alert compares against a daily average.

    The quote endpoint's day_volume still carried yesterday's total before the
    open, which is what made a spike re-fire the next morning.
    """
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "regular")
    today = datetime.now(NY).strftime("%Y-%m-%d")
    frame = pd.concat([_series("2026-01-02", 90.0, volume=999_999),
                       _series(today, 100.0, volume=100)])
    monkeypatch.setattr(data_feed.yf, "download", lambda t, **kw: _batch(t, frame))

    quote = StockDataFeed().get_current_data_batch(["AVGO"])["AVGO"]
    assert quote["volume"] < 999_999, "yesterday's volume must not be counted"
    assert quote["volume"] % 100 == 0
