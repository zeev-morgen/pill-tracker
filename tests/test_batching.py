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


def _daily_only(frame):
    """A download stub that answers the daily call and nothing else.

    collect_positions now makes two batch calls with different intervals — the
    6-month daily series and a 5-minute intraday one. A stub that ignores its
    arguments would hand daily bars back for both, so the intraday path has to
    opt in explicitly.
    """
    def stub(tickers, **kwargs):
        if kwargs.get("interval") == "5m":
            return pd.DataFrame()
        return frame(tickers) if callable(frame) else frame
    return stub


def _intraday(tickers, price=105.0):
    """A 5-minute series spanning a regular close and a later bar."""
    stamps = pd.DatetimeIndex([
        pd.Timestamp("2026-08-03 15:55", tz="America/New_York"),
        pd.Timestamp("2026-08-04 08:05", tz="America/New_York"),
    ])
    per_ticker = pd.DataFrame({"Close": [100.0, price]}, index=stamps)
    return pd.concat({t: per_ticker for t in tickers}, axis=1)


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

    monkeypatch.setattr(portfolio_risk.yf, "download", _daily_only(
        lambda t, **kw: fake_download(t, **kw)))
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
                        _daily_only(lambda tickers, **kw: _batch(covered)))
    report = PortfolioRiskAnalyzer(store).full_report()

    assert len(report["positions"]) == len(TICKERS)
    assert sorted(counting_ticker) == sorted(TICKERS[3:])


def test_a_failed_batch_degrades_to_per_ticker(monkeypatch, store, counting_ticker):
    def boom(tickers, **kwargs):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(portfolio_risk.yf, "download", _daily_only(boom))
    report = PortfolioRiskAnalyzer(store).full_report()

    assert len(report["positions"]) == len(TICKERS)
    assert sorted(counting_ticker) == sorted(TICKERS)


def test_a_single_holding_batch_is_handled(monkeypatch, counting_ticker):
    """yfinance returns flat columns for one ticker, not a MultiIndex."""
    store = PortfolioStore()
    store.upsert(Holding.create("AMZN", 10, 90.0))
    monkeypatch.setattr(portfolio_risk.yf, "download",
                        _daily_only(lambda tickers, **kw: _frame(120.0)))

    report = PortfolioRiskAnalyzer(store).full_report()
    assert report["positions"][0]["current_price"] == pytest.approx(120.0)
    assert counting_ticker == []


def test_an_empty_portfolio_makes_no_request(monkeypatch, counting_ticker):
    def explode(*args, **kwargs):
        raise AssertionError("no holdings means no request")

    monkeypatch.setattr(portfolio_risk.yf, "download", _daily_only(explode))
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


def test_no_quote_requests_when_the_market_is_shut_and_bars_are_current(
    monkeypatch, store, counting_feed
):
    """The close *is* the price then, so the whole report costs one request."""
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "closed")
    monkeypatch.setattr(portfolio_risk.yf, "download",
                        _daily_only(lambda t, **kw: _batch(TICKERS)))

    report = PortfolioRiskAnalyzer(store, data_feed=counting_feed).full_report()
    assert counting_feed.calls == []
    assert all(p["price_source"] == "bar" for p in report["positions"])


@pytest.mark.parametrize("session", ["pre", "regular", "after"])
def test_the_quote_is_fetched_while_a_session_is_running(
    monkeypatch, store, counting_feed, session
):
    """A daily close is behind any session in progress, including the regular one."""
    monkeypatch.setattr(data_feed, "get_market_session", lambda: session)
    monkeypatch.setattr(portfolio_risk.yf, "download",
                        _daily_only(lambda t, **kw: _batch(TICKERS)))

    report = PortfolioRiskAnalyzer(store, data_feed=counting_feed).full_report()
    assert sorted(counting_feed.calls) == sorted(TICKERS)
    assert all(p["price_source"] == "quote" for p in report["positions"])
    assert all(p["current_price"] == pytest.approx(105.0) for p in report["positions"])


# ── Skip reason ───────────────────────────────────────────────────────────────

def test_the_skip_reason_names_what_went_wrong(monkeypatch, store):
    class FailingTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **kwargs):
            raise ConnectionError("blocked")

    monkeypatch.setattr(portfolio_risk.yf, "Ticker", FailingTicker)
    monkeypatch.setattr(portfolio_risk.yf, "download",
                        _daily_only(lambda t, **kw: pd.DataFrame()))

    report = PortfolioRiskAnalyzer(store).full_report()
    assert report["skipped_tickers"] == TICKERS
    assert report["skip_reason"] == "ConnectionError"


def test_no_skip_reason_when_nothing_was_skipped(monkeypatch, store):
    monkeypatch.setattr(portfolio_risk.yf, "download",
                        _daily_only(lambda t, **kw: _batch(TICKERS)))
    report = PortfolioRiskAnalyzer(store).full_report()
    assert report["skipped_tickers"] == []
    assert report["skip_reason"] is None


# ── Trailing empty bars ───────────────────────────────────────────────────────

def _frame_with_blank_last_bar(price=100.0, rows=60):
    """What a batch download hands back for a ticker with no bar yet today.

    yf.download indexes every ticker against the union of all their trading
    days, so a ticker missing the newest date gets a row of NaN prices with a
    zero volume — which survives dropna(how="all").
    """
    frame = _frame(price, rows)
    last = frame.index[-1]
    frame.loc[last, ["High", "Low", "Close"]] = np.nan
    frame["Volume"] = 1
    frame.loc[last, "Volume"] = 0
    return frame


def test_a_blank_last_bar_uses_the_last_real_close(monkeypatch):
    store = PortfolioStore()
    store.upsert(Holding.create("AMZN", 10, 90.0))
    monkeypatch.setattr(
        portfolio_risk.yf, "download",
        _daily_only(lambda t, **kw: pd.concat({"AMZN": _frame_with_blank_last_bar(123.0)}, axis=1)),
    )
    report = PortfolioRiskAnalyzer(store).full_report()

    assert report["skipped_tickers"] == [], report["skip_reason"]
    assert report["positions"][0]["current_price"] == pytest.approx(123.0)


def test_blank_last_bars_across_the_whole_portfolio(monkeypatch):
    """The production symptom: every holding reported as 'unusable price'."""
    store = PortfolioStore()
    for t in TICKERS:
        store.upsert(Holding.create(t, 10, 90.0))
    monkeypatch.setattr(
        portfolio_risk.yf, "download",
        lambda t, **kw: pd.concat(
            {x: _frame_with_blank_last_bar(100.0) for x in TICKERS}, axis=1
        ),
    )
    report = PortfolioRiskAnalyzer(store).full_report()

    assert report["skipped_tickers"] == []
    assert len(report["positions"]) == len(TICKERS)
    assert report["total_value"] == pytest.approx(len(TICKERS) * 1000.0)


def test_blank_bars_are_kept_out_of_the_atr_window(monkeypatch):
    """A NaN bar inside the window would otherwise poison the average."""
    store = PortfolioStore()
    store.upsert(Holding.create("AMZN", 10, 90.0))
    monkeypatch.setattr(
        portfolio_risk.yf, "download",
        _daily_only(lambda t, **kw: pd.concat({"AMZN": _frame_with_blank_last_bar()}, axis=1)),
    )
    assert PortfolioRiskAnalyzer(store).full_report()["positions"][0]["atr_pct"] is not None


def test_a_frame_of_nothing_but_blank_bars_is_skipped(monkeypatch):
    store = PortfolioStore()
    store.upsert(Holding.create("AMZN", 10, 90.0))
    blank = _frame()
    blank["Close"] = np.nan
    blank["Volume"] = 0
    monkeypatch.setattr(portfolio_risk.yf, "download",
                        _daily_only(lambda t, **kw: pd.concat({"AMZN": blank}, axis=1)))
    report = PortfolioRiskAnalyzer(store).full_report()
    assert report["skipped_tickers"] == ["AMZN"]
    assert report["skip_reason"] == "no price data"


# ── As-of date ────────────────────────────────────────────────────────────────

def _frame_ending(last_day, price=100.0, rows=60):
    idx = pd.bdate_range(end=last_day, periods=rows)
    close = np.full(rows, price)
    return pd.DataFrame({"High": close + 1, "Low": close - 1, "Close": close}, index=idx)


def test_the_report_says_which_session_the_price_is_from(monkeypatch):
    store = PortfolioStore()
    store.upsert(Holding.create("AMZN", 10, 90.0))
    monkeypatch.setattr(
        portfolio_risk.yf, "download",
        _daily_only(lambda t, **kw: pd.concat({"AMZN": _frame_ending("2026-08-03")}, axis=1)),
    )
    report = PortfolioRiskAnalyzer(store).full_report()

    assert report["positions"][0]["price_date"] == "2026-08-03"
    assert report["latest_bar_date"] == "2026-08-03"
    assert report["positions"][0]["price_is_stale"] is False


def test_a_holding_quoting_an_older_session_is_flagged(monkeypatch):
    """A blank final bar silently rolls the quote back a day — say so."""
    store = PortfolioStore()
    for t in ("FRESH", "STALE"):
        store.upsert(Holding.create(t, 10, 90.0))
    monkeypatch.setattr(portfolio_risk.yf, "download", _daily_only(
        lambda t, **kw: pd.concat({
            "FRESH": _frame_ending("2026-08-03"),
            "STALE": _frame_ending("2026-07-31"),
        }, axis=1, sort=True)))
    report = PortfolioRiskAnalyzer(store).full_report()
    by_ticker = {p["ticker"]: p for p in report["positions"]}

    assert report["latest_bar_date"] == "2026-08-03"
    assert by_ticker["FRESH"]["price_is_stale"] is False
    assert by_ticker["STALE"]["price_is_stale"] is True
    assert by_ticker["STALE"]["price_date"] == "2026-07-31"


def test_the_as_of_date_follows_the_bar_actually_used(monkeypatch):
    """After falling back past a blank bar, the date must move back with it."""
    store = PortfolioStore()
    store.upsert(Holding.create("AMZN", 10, 90.0))
    frame = _frame_ending("2026-08-03")
    frame.loc[frame.index[-1], "Close"] = np.nan     # no bar for the last day
    monkeypatch.setattr(portfolio_risk.yf, "download",
                        _daily_only(lambda t, **kw: pd.concat({"AMZN": frame}, axis=1)))

    position = PortfolioRiskAnalyzer(store).full_report()["positions"][0]
    assert position["price_date"] == "2026-07-31", "Aug 1-2 is a weekend"


# ── The chart endpoint lagging behind the quote endpoint ──────────────────────
#
# Observed in production for AVGO: both yf.download and Ticker.history returned
# a 2026-08-03 row with Close=null, while fast_info already carried that
# session's real close. Falling back to the previous bar quoted Friday's price
# on a Tuesday.

def _frame_with_unfilled_latest_bar(price=100.0, rows=60):
    frame = _frame(price, rows)
    frame.loc[frame.index[-1], "Close"] = np.nan
    return frame


@pytest.fixture
def live_quote_feed():
    class Feed:
        def __init__(self):
            self.calls = []

        def get_current_data(self, symbol):
            self.calls.append(symbol)
            return {"session": "closed", "price": 392.23, "since_close_pct": 1.2}

    return Feed()


def test_a_lagging_chart_endpoint_falls_through_to_the_quote(
    monkeypatch, live_quote_feed
):
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "closed")
    store = PortfolioStore()
    store.upsert(Holding.create("AVGO", 10, 300.0))
    monkeypatch.setattr(
        portfolio_risk.yf, "download",
        _daily_only(lambda t, **kw: pd.concat({"AVGO": _frame_with_unfilled_latest_bar(389.28)}, axis=1)),
    )

    position = PortfolioRiskAnalyzer(
        store, data_feed=live_quote_feed
    ).full_report()["positions"][0]

    assert position["current_price"] == pytest.approx(392.23)
    assert position["price_source"] == "quote"
    assert position["price_date"] is None, "a live quote belongs to no closed session"
    assert live_quote_feed.calls == ["AVGO"]


def test_without_a_quote_it_still_falls_back_to_the_last_close(monkeypatch):
    """No feed configured — the older close beats no price at all."""
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "closed")
    store = PortfolioStore()
    store.upsert(Holding.create("AVGO", 10, 300.0))
    monkeypatch.setattr(
        portfolio_risk.yf, "download",
        _daily_only(lambda t, **kw: pd.concat({"AVGO": _frame_with_unfilled_latest_bar(389.28)}, axis=1)),
    )

    position = PortfolioRiskAnalyzer(store).full_report()["positions"][0]
    assert position["current_price"] == pytest.approx(389.28)
    assert position["price_source"] == "bar"


def test_a_broken_quote_does_not_lose_the_position(monkeypatch):
    class BadFeed:
        def get_current_data(self, symbol):
            raise RuntimeError("429")

    monkeypatch.setattr(data_feed, "get_market_session", lambda: "closed")
    store = PortfolioStore()
    store.upsert(Holding.create("AVGO", 10, 300.0))
    monkeypatch.setattr(
        portfolio_risk.yf, "download",
        _daily_only(lambda t, **kw: pd.concat({"AVGO": _frame_with_unfilled_latest_bar(389.28)}, axis=1)),
    )

    position = PortfolioRiskAnalyzer(store, data_feed=BadFeed()).full_report()["positions"][0]
    assert position["current_price"] == pytest.approx(389.28)
    assert position["price_source"] == "bar"


def test_a_nan_quote_is_not_preferred_over_a_real_close(monkeypatch):
    class NanFeed:
        def get_current_data(self, symbol):
            return {"session": "closed", "price": float("nan")}

    monkeypatch.setattr(data_feed, "get_market_session", lambda: "closed")
    store = PortfolioStore()
    store.upsert(Holding.create("AVGO", 10, 300.0))
    monkeypatch.setattr(
        portfolio_risk.yf, "download",
        _daily_only(lambda t, **kw: pd.concat({"AVGO": _frame_with_unfilled_latest_bar(389.28)}, axis=1)),
    )

    position = PortfolioRiskAnalyzer(store, data_feed=NanFeed()).full_report()["positions"][0]
    assert position["current_price"] == pytest.approx(389.28)
    assert position["price_source"] == "bar"


def test_the_atr_window_still_excludes_the_unfilled_bar(monkeypatch, live_quote_feed):
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "closed")
    store = PortfolioStore()
    store.upsert(Holding.create("AVGO", 10, 300.0))
    monkeypatch.setattr(
        portfolio_risk.yf, "download",
        _daily_only(lambda t, **kw: pd.concat({"AVGO": _frame_with_unfilled_latest_bar(389.28)}, axis=1)),
    )

    position = PortfolioRiskAnalyzer(
        store, data_feed=live_quote_feed
    ).full_report()["positions"][0]
    assert position["atr_pct"] is not None


@pytest.mark.parametrize("volume", [0, np.nan, None])
def test_a_blank_bar_is_detected_whatever_its_volume_looks_like(
    monkeypatch, live_quote_feed, volume
):
    """Detection must not hinge on how yfinance happens to fill the blank row.

    dropna(how="all") removes the row when every field is NaN and keeps it when
    Volume is 0, so cleaning it before the check made the fallback fire only
    sometimes.
    """
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "closed")
    frame = _frame(389.28)
    frame.loc[frame.index[-1], ["High", "Low", "Close"]] = np.nan
    if volume is not None:
        frame["Volume"] = 1000
        frame.loc[frame.index[-1], "Volume"] = volume

    store = PortfolioStore()
    store.upsert(Holding.create("AVGO", 10, 300.0))
    monkeypatch.setattr(portfolio_risk.yf, "download",
                        _daily_only(lambda t, **kw: pd.concat({"AVGO": frame}, axis=1)))

    position = PortfolioRiskAnalyzer(
        store, data_feed=live_quote_feed
    ).full_report()["positions"][0]
    assert position["price_source"] == "quote"
    assert position["current_price"] == pytest.approx(392.23)


# ── The intraday batch ────────────────────────────────────────────────────────
#
# A daily close cannot be the current price while a session is running, so the
# live price used to cost a quote plus a history call per holding. For thirteen
# positions that was 26 requests a refresh — above the limit that got the whole
# portfolio throttled, and worst exactly at 09:00 when the tab is actually used.

@pytest.fixture
def counting_download(monkeypatch):
    """Counts each batch call and answers daily and intraday separately."""
    calls = {"daily": 0, "intraday": 0}

    def stub(tickers, **kwargs):
        if kwargs.get("interval") == "5m":
            calls["intraday"] += 1
            return _intraday(list(tickers), price=105.0)
        calls["daily"] += 1
        return _batch(list(tickers))

    monkeypatch.setattr(portfolio_risk.yf, "download", stub)
    return calls


@pytest.mark.parametrize("session", ["pre", "regular", "after"])
def test_a_running_session_costs_two_requests_for_any_portfolio_size(
    monkeypatch, store, counting_download, counting_feed, session
):
    monkeypatch.setattr(data_feed, "get_market_session", lambda: session)
    report = PortfolioRiskAnalyzer(store, data_feed=counting_feed).full_report()

    assert counting_download == {"daily": 1, "intraday": 1}
    assert counting_feed.calls == [], "the batch must replace the per-ticker quotes"
    assert len(report["positions"]) == len(TICKERS)
    assert all(p["price_source"] == "quote" for p in report["positions"])
    assert all(p["current_price"] == pytest.approx(105.0) for p in report["positions"])


def test_a_shut_market_with_current_bars_still_costs_one(
    monkeypatch, store, counting_download, counting_feed
):
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "closed")
    PortfolioRiskAnalyzer(store, data_feed=counting_feed).full_report()
    assert counting_download == {"daily": 1, "intraday": 0}
    assert counting_feed.calls == []


def test_lagging_daily_bars_pull_in_the_intraday_batch_even_when_shut(
    monkeypatch, store, counting_download
):
    """The chart endpoint publishes a blank row most mornings; that is the cue."""
    def stub(tickers, **kwargs):
        if kwargs.get("interval") == "5m":
            counting_download["intraday"] += 1
            return _intraday(list(tickers), price=105.0)
        counting_download["daily"] += 1
        return pd.concat(
            {t: _frame_with_blank_last_bar() for t in list(tickers)}, axis=1
        )

    monkeypatch.setattr(portfolio_risk.yf, "download", stub)
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "closed")
    report = PortfolioRiskAnalyzer(store).full_report()

    assert counting_download["intraday"] == 1
    assert all(p["current_price"] == pytest.approx(105.0) for p in report["positions"])


def test_the_extended_move_is_derived_from_the_batch(
    monkeypatch, store, counting_download
):
    """The batch carries the regular close, not a precomputed percentage."""
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "pre")
    report = PortfolioRiskAnalyzer(store).full_report()

    position = report["positions"][0]
    assert position["extended_price"] == pytest.approx(105.0)
    # 105.0 against the 100.0 regular close in _intraday()
    assert position["extended_change_pct"] == pytest.approx(5.0)


def test_a_failed_intraday_batch_falls_back_to_the_per_ticker_quote(
    monkeypatch, store, counting_feed
):
    def stub(tickers, **kwargs):
        if kwargs.get("interval") == "5m":
            raise RuntimeError("429 Too Many Requests")
        return _batch(list(tickers))

    monkeypatch.setattr(portfolio_risk.yf, "download", stub)
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "pre")
    report = PortfolioRiskAnalyzer(store, data_feed=counting_feed).full_report()

    assert sorted(counting_feed.calls) == sorted(TICKERS)
    assert all(p["current_price"] == pytest.approx(105.0) for p in report["positions"])


def test_a_ticker_missing_from_the_intraday_batch_falls_back_alone(
    monkeypatch, store, counting_feed
):
    covered = TICKERS[:3]

    def stub(tickers, **kwargs):
        if kwargs.get("interval") == "5m":
            return _intraday(covered, price=105.0)
        return _batch(list(tickers))

    monkeypatch.setattr(portfolio_risk.yf, "download", stub)
    monkeypatch.setattr(data_feed, "get_market_session", lambda: "pre")
    PortfolioRiskAnalyzer(store, data_feed=counting_feed).full_report()

    assert sorted(counting_feed.calls) == sorted(TICKERS[3:])


# ── Volume spikes are an intraday measure ─────────────────────────────────────

def test_a_volume_spike_only_fires_during_the_regular_session():
    """The quote's volume field is not today's outside the session.

    Before the open it still carries yesterday's final total, so a genuine
    spike one day was re-detected as a fresh spike the next morning — and
    again after the close, once per cooldown, until 20:00.
    """
    from stock_monitor.alert_engine import AlertEngine
    from stock_monitor.config import AlertConfig, AppConfig, StockConfig

    config = AppConfig(stocks=[StockConfig(symbol="MU", alerts=[
        AlertConfig(type="volume_spike", multiplier=2.5, cooldown_minutes=60)])])

    class Feed:
        def get_average_daily_volume(self, symbol):
            return 10_000_000

    engine = AlertEngine(config, Feed())
    alert = config.stocks[0].alerts[0]
    yesterdays_spike = 32_000_000        # 3.2x the average

    fired = {}
    for session in ("regular", "pre", "after", "closed"):
        engine._cooldowns.clear()
        fired[session] = engine._check_volume_spike(
            "MU", alert, yesterdays_spike, session) is not None

    assert fired == {"regular": True, "pre": False, "after": False, "closed": False}


def test_an_unknown_session_still_evaluates():
    """Callers that pass no session keep the old behaviour rather than going mute."""
    from stock_monitor.alert_engine import AlertEngine
    from stock_monitor.config import AlertConfig, AppConfig, StockConfig

    config = AppConfig(stocks=[StockConfig(symbol="MU", alerts=[])])

    class Feed:
        def get_average_daily_volume(self, symbol):
            return 10_000_000

    engine = AlertEngine(config, Feed())
    alert = AlertConfig(type="volume_spike", multiplier=2.5, time_adjusted=False)
    assert engine._check_volume_spike("MU", alert, 32_000_000) is not None
