"""The Tiingo fallback quote source.

Yahoo answers this host with a 200 carrying the previous session's prices, so
the failure it is replacing is a *silent* one — there is no exception to catch
and no status code to branch on. That shapes everything here: the tests care
most about the cases where a wrong answer would look like a right one.

The provider is unreachable from CI, so every test drives a recorded response
shape. That means these prove the parsing and the fallback logic, not that
Tiingo's live payload matches — the first real contact happens on deploy.
"""

from datetime import date, datetime

import pytest

from stock_monitor import tiingo

# A response shaped like Tiingo's IEX endpoint, mid pre-market.
ROW = {
    "ticker": "AVGO",
    "timestamp": "2026-08-06T07:45:00.123456789-04:00",
    "last": 421.5,
    "tngoLast": 421.75,
    "prevClose": 418.28,
    "open": 419.0,
    "high": 422.1,
    "low": 418.9,
    "volume": 91234,
}


@pytest.fixture
def tiingo_api(monkeypatch):
    """Installs a key and a stand-in transport. Yields the mutable state."""
    monkeypatch.setenv(tiingo.ENV_KEY, "test-key")
    state = {"status": 200, "payload": [dict(ROW)], "seen": []}

    class FakeResponse:
        status_code = property(lambda self: state["status"])

        def json(self):
            payload = state["payload"]
            if isinstance(payload, Exception):
                raise payload
            return payload

    def fake_get(tickers):
        state["seen"].append(list(tickers))
        if isinstance(state.get("error"), Exception):
            raise state["error"]
        return FakeResponse()

    monkeypatch.setattr(tiingo, "_get", fake_get)
    return state


@pytest.fixture
def et_now(monkeypatch):
    """Pins the exchange clock so 'is this quote from today?' is decidable."""
    import datetime as dt

    from stock_monitor.data_feed import NYSE_TZ

    real = dt.datetime

    def _set(year, month, day, hour, minute):
        pinned = NYSE_TZ.localize(real(year, month, day, hour, minute))

        class FakeDatetime(real):
            @classmethod
            def now(cls, tz=None):
                return pinned if tz else pinned.replace(tzinfo=None)

        monkeypatch.setattr(tiingo, "datetime", FakeDatetime)

    return _set


# ── Configuration ─────────────────────────────────────────────────────────────

def test_without_a_key_nothing_is_requested(monkeypatch):
    """The feature is opt-in; an unconfigured deploy must behave as before."""
    monkeypatch.delenv(tiingo.ENV_KEY, raising=False)
    calls = []
    monkeypatch.setattr(tiingo, "_get", lambda t: calls.append(t))

    assert tiingo.get_quotes(["AVGO"]) == {}
    assert calls == [], "no request may be made without a key"


def test_a_whitespace_only_key_counts_as_unset(monkeypatch):
    monkeypatch.setenv(tiingo.ENV_KEY, "   ")
    assert not tiingo.is_configured()


def test_no_tickers_makes_no_request(tiingo_api):
    assert tiingo.get_quotes([]) == {}
    assert tiingo_api["seen"] == []


def test_the_whole_portfolio_goes_in_one_request(tiingo_api):
    tiingo_api["payload"] = [dict(ROW, ticker=t) for t in ("AVGO", "JPM", "MU")]
    tiingo.get_quotes(["AVGO", "JPM", "MU"])

    assert len(tiingo_api["seen"]) == 1, "batched, not one call per ticker"
    assert tiingo_api["seen"][0] == ["AVGO", "JPM", "MU"]


def test_duplicate_tickers_are_not_requested_twice(tiingo_api):
    tiingo.get_quotes(["AVGO", "AVGO", "JPM"])
    assert tiingo_api["seen"][0] == ["AVGO", "JPM"]


def test_the_key_travels_in_a_header_not_the_url(monkeypatch):
    """URLs reach access logs and error reports; this one is a credential."""
    monkeypatch.setenv(tiingo.ENV_KEY, "secret-key")
    captured = {}

    class FakeRequests:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            captured.update(url=url, params=params, headers=headers)
            raise RuntimeError("stop here")

    monkeypatch.setitem(__import__("sys").modules, "requests", FakeRequests)
    try:
        tiingo._get(["AVGO"])
    except RuntimeError:
        pass

    assert "secret-key" not in str(captured["params"])
    assert "secret-key" not in captured["url"]
    assert captured["headers"]["Authorization"] == "Token secret-key"


# ── Parsing ───────────────────────────────────────────────────────────────────

def test_a_quote_is_parsed(tiingo_api, et_now):
    et_now(2026, 8, 6, 7, 50)
    quote = tiingo.get_quotes(["AVGO"])["AVGO"]

    assert quote["price"] == 421.75, "tngoLast is preferred over last"
    assert quote["as_of"] == date(2026, 8, 6)


def test_iex_last_is_used_when_the_consolidated_print_is_missing(tiingo_api, et_now):
    """Outside regular hours one of the two is routinely null."""
    et_now(2026, 8, 6, 7, 50)
    tiingo_api["payload"] = [dict(ROW, tngoLast=None)]

    assert tiingo.get_quotes(["AVGO"])["AVGO"]["price"] == 421.5


def test_nanosecond_timestamps_parse(tiingo_api):
    """fromisoformat accepts 3 or 6 fractional digits and rejects 9."""
    parsed = tiingo.parse_timestamp("2026-08-06T07:45:00.123456789-04:00")

    assert parsed is not None, "a 9-digit fraction must not lose the whole quote"
    assert parsed.hour == 7 and parsed.minute == 45


@pytest.mark.parametrize("text", [
    "2026-08-06T07:45:00-04:00",
    "2026-08-06T11:45:00.123Z",
    "2026-08-06T11:45:00.123456Z",
])
def test_the_other_timestamp_shapes_parse(text):
    assert tiingo.parse_timestamp(text) is not None


@pytest.mark.parametrize("text", [None, "", "not a date", 12345, "2026-13-45T99:99:99"])
def test_an_unparseable_timestamp_is_none_not_an_exception(text):
    assert tiingo.parse_timestamp(text) is None


def test_a_naive_timestamp_is_rejected():
    """Without an offset the exchange-local date cannot be established."""
    assert tiingo.parse_timestamp("2026-08-06T07:45:00") is None


def test_a_quote_without_a_usable_timestamp_is_not_dated_today(tiingo_api, et_now):
    """Defaulting to today is how a stale price passes for a live one."""
    et_now(2026, 8, 6, 7, 50)
    tiingo_api["payload"] = [dict(ROW, timestamp=None)]

    assert tiingo.get_quotes(["AVGO"])["AVGO"]["as_of"] is None


def test_the_date_is_the_exchange_date_not_the_servers(tiingo_api, et_now):
    """A UTC server is already on the next day while New York still trades."""
    et_now(2026, 8, 6, 20, 30)
    tiingo_api["payload"] = [dict(ROW, timestamp="2026-08-07T00:30:00.000-00:00")]

    assert tiingo.get_quotes(["AVGO"])["AVGO"]["as_of"] == date(2026, 8, 6)


def test_the_ticker_is_upper_cased(tiingo_api, et_now):
    et_now(2026, 8, 6, 7, 50)
    tiingo_api["payload"] = [dict(ROW, ticker="avgo")]

    assert "AVGO" in tiingo.get_quotes(["AVGO"])


@pytest.mark.parametrize("price", [0, -1, None, float("nan"), "abc"])
def test_an_unusable_price_is_dropped_not_returned(tiingo_api, price):
    """Presence in the result means 'there is a live price here'."""
    tiingo_api["payload"] = [dict(ROW, tngoLast=price, last=price)]

    assert tiingo.get_quotes(["AVGO"]) == {}


def test_one_bad_row_does_not_lose_the_others(tiingo_api, et_now):
    et_now(2026, 8, 6, 7, 50)
    tiingo_api["payload"] = ["not a dict", dict(ROW, ticker="JPM")]

    assert list(tiingo.get_quotes(["AVGO", "JPM"])) == ["JPM"]


# ── The reference close ───────────────────────────────────────────────────────

def test_before_the_open_prev_close_is_the_reference(tiingo_api, et_now, monkeypatch):
    from stock_monitor import data_feed
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "pre")
    et_now(2026, 8, 6, 7, 50)

    assert tiingo.get_quotes(["AVGO"])["AVGO"]["regular_close"] == 418.28


def test_after_the_close_no_reference_is_claimed(tiingo_api, et_now, monkeypatch):
    """prevClose is the day *before* today, so a post-market move measured
    against it would report a whole extra session's change."""
    from stock_monitor import data_feed
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "after")
    et_now(2026, 8, 6, 17, 0)

    assert tiingo.get_quotes(["AVGO"])["AVGO"]["regular_close"] is None


# ── Failure handling ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_a_bad_status_yields_no_quotes_and_no_exception(tiingo_api, status):
    tiingo_api["status"] = status
    assert tiingo.get_quotes(["AVGO"]) == {}


def test_a_transport_error_yields_no_quotes(tiingo_api):
    tiingo_api["error"] = OSError("connection reset")
    assert tiingo.get_quotes(["AVGO"]) == {}


def test_unparseable_json_yields_no_quotes(tiingo_api):
    tiingo_api["payload"] = ValueError("not json")
    assert tiingo.get_quotes(["AVGO"]) == {}


def test_a_non_list_payload_yields_no_quotes(tiingo_api):
    """Tiingo reports errors as a JSON object where a list is expected."""
    tiingo_api["payload"] = {"detail": "Error: Invalid token."}
    assert tiingo.get_quotes(["AVGO"]) == {}


# ── The monitor payload ───────────────────────────────────────────────────────

def test_the_monitor_payload_carries_the_fields_the_live_tab_reads(tiingo_api, et_now, monkeypatch):
    from stock_monitor import data_feed
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "pre")
    et_now(2026, 8, 6, 7, 50)

    quote = tiingo.get_monitor_quotes(["AVGO"])["AVGO"]
    for field in ("symbol", "price", "prev_close", "open_price", "change_pct",
                  "from_open_pct", "since_close_pct", "volume", "day_high",
                  "day_low", "session", "timestamp", "bar_time"):
        assert field in quote, field
    assert quote["day_high"] == 422.1
    assert quote["prev_close"] == 418.28
    assert quote["change_pct"] == pytest.approx((421.75 - 418.28) / 418.28 * 100)


def test_the_monitor_payload_reports_no_volume(tiingo_api, et_now, monkeypatch):
    """IEX volume is a fraction of the consolidated tape the spike average uses.

    Passing it through would make every session look like a volume collapse;
    the alert engine skips a symbol at 0, which leaves it silent instead.
    """
    from stock_monitor import data_feed
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "regular")
    et_now(2026, 8, 6, 11, 0)

    assert tiingo.get_monitor_quotes(["AVGO"])["AVGO"]["volume"] == 0


def test_a_stale_monitor_quote_is_dropped(tiingo_api, et_now, monkeypatch):
    """The whole point of the exercise: no more frozen prices badged as live."""
    from stock_monitor import data_feed
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "regular")
    et_now(2026, 8, 6, 11, 0)
    tiingo_api["payload"] = [dict(ROW, timestamp="2026-08-05T16:00:00.000-04:00")]

    assert tiingo.get_monitor_quotes(["AVGO"]) == {}


def test_the_monitor_shape_matches_what_the_yahoo_path_returns(tiingo_api, et_now, monkeypatch):
    """The two are interchangeable at the call site, so the keys must agree."""
    import inspect

    from stock_monitor.data_feed import StockDataFeed
    from stock_monitor import data_feed

    monkeypatch.setattr(data_feed, "get_market_session", lambda: "pre")
    et_now(2026, 8, 6, 7, 50)

    source = inspect.getsource(StockDataFeed._from_intraday)
    yahoo_keys = set(__import__("re").findall(r'^\s+"(\w+)":', source, __import__("re").M))
    ours = set(tiingo.get_monitor_quotes(["AVGO"])["AVGO"])

    assert yahoo_keys <= ours, f"missing from the Tiingo payload: {yahoo_keys - ours}"


def test_no_key_means_the_monitor_path_is_untouched(monkeypatch):
    monkeypatch.delenv(tiingo.ENV_KEY, raising=False)
    assert tiingo.get_monitor_quotes(["AVGO"]) == {}
