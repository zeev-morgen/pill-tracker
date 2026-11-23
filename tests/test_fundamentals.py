"""Valuation multiples: the fallback chain, and never letting NaN or a
throttled endpoint look like a company without a P/E.

Every analysis was reporting "אין נתון" for P/E, growth and margins. The old
version of this file passed throughout, because its fixture fed the fetcher
the key the fetcher read — ``pegRatio`` — and yfinance stopped producing that
name long ago. So the fixtures here now carry the keys yfinance actually
publishes, and the first test asserts the live name directly.
"""

import math

import pandas as pd
import pytest

from stock_monitor import fundamentals
from stock_monitor.ai_analyst import StockAnalyst

#: The keys yfinance 1.8 really puts in ``Ticker.info``. Note trailingPegRatio:
#: the PEG figure moved to Yahoo's timeseries API and the old ``pegRatio`` key
#: no longer exists anywhere in the library.
INFO = {
    "trailingPE": 34.2, "forwardPE": 28.1, "trailingPegRatio": 1.8,
    "marketCap": 1.62e12, "revenueGrowth": 0.21, "earningsGrowth": 0.34,
    "grossMargins": 0.63, "profitMargins": 0.29, "recommendationKey": "buy",
    "targetMeanPrice": 410.0, "fiftyTwoWeekHigh": 400.0, "fiftyTwoWeekLow": 190.0,
    "sector": "Technology", "shortName": "Broadcom Inc.",
}

VALUATION = pd.DataFrame(
    {"Current": [1.5e12, 30.0, 25.0, 2.1]},
    index=["Market Cap", "Trailing P/E", "Forward P/E", "PEG Ratio (5yr expected)"],
)


def _statement(revenue, gross=None, net=None, eps=None, columns=1):
    rows, index = [], []
    for name, first in (("Total Revenue", revenue), ("Gross Profit", gross),
                        ("Net Income", net), ("Diluted EPS", eps)):
        if first is None:
            continue
        index.append(name)
        rows.append([first] * columns)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows, index=index, columns=list(range(columns)))


def _series_statement(name, values):
    """A statement with one row and several periods, newest column first."""
    return pd.DataFrame([values], index=[name], columns=list(range(len(values))))


class FakeTicker:
    """Each endpoint independently configurable, including to fail."""

    info_payload = {}
    valuation_payload = None
    ttm_payload = None
    annual_payload = None
    quarterly_payload = None
    fast_payload = {}

    def __init__(self, symbol):
        self.symbol = symbol

    @staticmethod
    def _answer(payload):
        if isinstance(payload, Exception):
            raise payload
        return payload

    @property
    def info(self):
        return self._answer(self.info_payload)

    def get_valuation_measures(self, freq="quarterly", periods=5):
        frame = self._answer(self.valuation_payload)
        return pd.DataFrame() if frame is None else frame

    @property
    def ttm_income_stmt(self):
        frame = self._answer(self.ttm_payload)
        return pd.DataFrame() if frame is None else frame

    @property
    def income_stmt(self):
        frame = self._answer(self.annual_payload)
        return pd.DataFrame() if frame is None else frame

    @property
    def quarterly_income_stmt(self):
        frame = self._answer(self.quarterly_payload)
        return pd.DataFrame() if frame is None else frame

    @property
    def fast_info(self):
        return self._answer(self.fast_payload)


@pytest.fixture(autouse=True)
def clean_cache():
    fundamentals.reset_cache()
    yield
    fundamentals.reset_cache()


@pytest.fixture
def ticker(monkeypatch):
    """A fresh FakeTicker subclass, so tests cannot leak payloads into each other."""
    stub = type("Stub", (FakeTicker,), {})
    monkeypatch.setattr(fundamentals.yf, "Ticker", stub)
    return stub


@pytest.fixture
def analyst():
    return StockAnalyst.__new__(StockAnalyst)   # no API client is needed


# ── The key that was simply wrong ─────────────────────────────────────────────

def test_the_peg_ratio_is_read_under_the_name_yfinance_publishes(ticker):
    """The bug that made this line permanently empty, in any market condition."""
    ticker.info_payload = {"trailingPegRatio": 1.8}
    assert fundamentals.fetch("AVGO").get("peg_ratio") == pytest.approx(1.8)


def test_the_retired_peg_key_is_still_honoured(ticker):
    """Older yfinance releases only had pegRatio; reading both costs nothing."""
    ticker.info_payload = {"pegRatio": 2.4}
    assert fundamentals.fetch("AVGO").get("peg_ratio") == pytest.approx(2.4)


def test_the_name_is_not_one_the_installed_yfinance_dropped():
    """A guard against the same class of bug: assert against the real library.

    If a future yfinance renames this again, this fails here rather than in a
    month of analyses quietly missing a line.
    """
    import inspect

    from yfinance.scrapers import quote

    assert "trailingPegRatio" in inspect.getsource(quote)


# ── One endpoint is not enough ────────────────────────────────────────────────

def test_a_healthy_quote_summary_answers_everything_in_one_request(ticker):
    ticker.info_payload = INFO
    ticker.valuation_payload = AssertionError("must not be asked")
    figures = fundamentals.fetch("AVGO")

    assert figures.get("pe_ratio") == pytest.approx(34.2)
    assert figures.get("profit_margins") == pytest.approx(0.29)
    assert figures.complete is True


def test_a_throttled_quote_summary_no_longer_empties_the_block(ticker):
    """The reported symptom: YFRateLimitError on .info took every figure with it."""
    ticker.info_payload = RuntimeError("YFRateLimitError")
    ticker.valuation_payload = VALUATION
    ticker.ttm_payload = _statement(1000.0, gross=630.0, net=290.0)
    ticker.quarterly_payload = _series_statement("Total Revenue",
                                                 [121.0, 115.0, 110.0, 105.0, 100.0])

    figures = fundamentals.fetch("AVGO")

    assert figures.get("pe_ratio") == pytest.approx(30.0)
    assert figures.get("forward_pe") == pytest.approx(25.0)
    assert figures.get("peg_ratio") == pytest.approx(2.1)
    assert figures.get("market_cap") == pytest.approx(1.5e12)
    assert figures.get("gross_margins") == pytest.approx(0.63)
    assert figures.get("profit_margins") == pytest.approx(0.29)
    assert figures.get("revenue_growth") == pytest.approx(0.21)


def test_each_figure_records_which_endpoint_supplied_it(ticker):
    ticker.info_payload = {"trailingPE": 34.2}
    ticker.valuation_payload = VALUATION
    ticker.ttm_payload = _statement(1000.0, gross=630.0)

    figures = fundamentals.fetch("AVGO")

    assert figures.sources["pe_ratio"] == "quote_summary"
    assert figures.sources["forward_pe"] == "valuation"
    assert figures.sources["gross_margins"] == "income_stmt"


def test_the_first_answer_wins_rather_than_the_last(ticker):
    ticker.info_payload = {"trailingPE": 34.2}
    ticker.valuation_payload = VALUATION          # also carries a P/E, of 30
    assert fundamentals.fetch("AVGO").get("pe_ratio") == pytest.approx(34.2)


def test_failures_are_recorded_even_when_the_figures_arrive(ticker):
    ticker.info_payload = TimeoutError("slow")
    ticker.valuation_payload = VALUATION
    figures = fundamentals.fetch("AVGO")

    assert "quote_summary: TimeoutError" in figures.errors
    assert figures.get("pe_ratio") is not None


def test_every_endpoint_failing_is_reported_as_no_data(ticker):
    for attribute in ("info_payload", "valuation_payload", "ttm_payload",
                      "annual_payload", "quarterly_payload", "fast_payload"):
        setattr(ticker, attribute, RuntimeError("down"))

    figures = fundamentals.fetch("AVGO")

    assert figures.any_data is False
    assert figures.complete is False
    assert len(figures.errors) >= 3


def test_an_empty_info_payload_counts_as_a_failure_not_an_answer(ticker):
    """Yahoo answers 200 with {} when throttling; believing it empties the block."""
    ticker.info_payload = {}
    ticker.valuation_payload = VALUATION
    assert fundamentals.fetch("AVGO").get("pe_ratio") == pytest.approx(30.0)


def test_fast_info_still_supplies_the_market_cap_and_range(ticker):
    ticker.info_payload = {}
    ticker.fast_payload = {"market_cap": 1.62e12, "year_high": 400.0, "year_low": 190.0}
    figures = fundamentals.fetch("AVGO")

    assert figures.get("market_cap") == pytest.approx(1.62e12)
    assert figures.get("fifty_two_high") == pytest.approx(400.0)
    assert figures.sources["market_cap"] == "fast_info"


# ── Margins and growth, computed from the accounts ────────────────────────────

def test_margins_come_from_the_trailing_twelve_months_when_there_is_one(ticker):
    ticker.info_payload = {}
    ticker.ttm_payload = _statement(2000.0, gross=1200.0, net=400.0)
    ticker.annual_payload = _statement(1000.0, gross=100.0, net=10.0)

    figures = fundamentals.fetch("AVGO")

    assert figures.get("gross_margins") == pytest.approx(0.60)
    assert figures.get("profit_margins") == pytest.approx(0.20)


def test_margins_fall_back_to_the_annual_statement(ticker):
    ticker.info_payload = {}
    ticker.ttm_payload = pd.DataFrame()
    ticker.annual_payload = _statement(1000.0, gross=630.0, net=290.0)

    assert fundamentals.fetch("AVGO").get("gross_margins") == pytest.approx(0.63)


def test_growth_compares_the_same_quarter_a_year_earlier(ticker):
    """Quarter against the previous quarter measures seasonality, not growth."""
    ticker.info_payload = {}
    ticker.quarterly_payload = _series_statement(
        "Total Revenue", [120.0, 118.0, 116.0, 114.0, 100.0]
    )

    figures = fundamentals.fetch("AVGO")

    assert figures.get("revenue_growth") == pytest.approx(0.20), "120 vs 100, not 118"
    assert figures.growth_basis == "רבעוני YoY"


def test_annual_growth_is_labelled_as_annual(ticker):
    """A yearly figure must not be passed off as Yahoo's quarterly definition."""
    ticker.info_payload = {}
    ticker.quarterly_payload = pd.DataFrame()
    ticker.annual_payload = _series_statement("Total Revenue", [110.0, 100.0])

    figures = fundamentals.fetch("AVGO")

    assert figures.get("revenue_growth") == pytest.approx(0.10)
    assert figures.growth_basis == "שנתי"


def test_a_loss_turning_to_profit_does_not_flip_the_sign(ticker):
    """Dividing by a negative base would report growth as a collapse."""
    ticker.info_payload = {}
    ticker.quarterly_payload = _series_statement("Net Income",
                                                 [50.0, 0.0, 0.0, 0.0, -100.0])

    assert fundamentals.fetch("AVGO").get("earnings_growth") == pytest.approx(1.5)


def test_a_zero_base_is_skipped_instead_of_dividing_by_it(ticker):
    ticker.info_payload = {}
    ticker.quarterly_payload = _series_statement("Total Revenue",
                                                 [100.0, 0.0, 0.0, 0.0, 0.0])
    assert fundamentals.fetch("AVGO").get("revenue_growth") is None


def test_zero_revenue_does_not_divide_a_margin(ticker):
    ticker.info_payload = {}
    ticker.ttm_payload = _statement(0.0, gross=0.0, net=0.0)
    figures = fundamentals.fetch("AVGO")

    assert figures.get("gross_margins") is None


def test_statement_rows_are_matched_however_they_are_spelled(ticker):
    """yfinance renames these between releases; a KeyError would cost the block."""
    ticker.info_payload = {}
    ticker.ttm_payload = pd.DataFrame(
        [[1000.0], [630.0]], index=["TotalRevenue", "grossprofit"], columns=[0]
    )

    assert fundamentals.fetch("AVGO").get("gross_margins") == pytest.approx(0.63)


# ── A P/E derived from earnings ───────────────────────────────────────────────

def test_a_pe_can_be_derived_from_earnings_per_share(ticker):
    ticker.info_payload = {}
    ticker.valuation_payload = pd.DataFrame()
    ticker.ttm_payload = _statement(1000.0, net=100.0, eps=10.0)

    figures = fundamentals.fetch("AVGO", price=350.0)

    assert figures.get("pe_ratio") == pytest.approx(35.0)
    assert figures.sources["pe_ratio"] == "derived"


def test_a_quoted_pe_is_preferred_over_a_derived_one(ticker):
    ticker.info_payload = {"trailingPE": 34.2}
    ticker.ttm_payload = _statement(1000.0, eps=10.0)
    assert fundamentals.fetch("AVGO", price=350.0).get("pe_ratio") == pytest.approx(34.2)


def test_no_pe_is_derived_for_a_tel_aviv_listing(ticker):
    """The quote is in agorot and the statements are not: a hundredfold error."""
    ticker.info_payload = {}
    ticker.valuation_payload = pd.DataFrame()
    ticker.ttm_payload = _statement(1000.0, eps=5.0)

    assert fundamentals.fetch("ESLT.TA", price=16240.0).get("pe_ratio") is None


def test_no_pe_is_derived_from_a_loss(ticker):
    """A negative P/E is not a valuation, and zero EPS is not divisible."""
    ticker.info_payload = {}
    ticker.valuation_payload = pd.DataFrame()
    ticker.ttm_payload = _statement(1000.0, eps=-2.0)

    assert fundamentals.fetch("AVGO", price=350.0).get("pe_ratio") is None


def test_nothing_is_derived_without_a_price(ticker):
    ticker.info_payload = {}
    ticker.valuation_payload = pd.DataFrame()
    ticker.ttm_payload = _statement(1000.0, eps=10.0)

    assert fundamentals.fetch("AVGO").get("pe_ratio") is None


# ── NaN never reaches a prompt ────────────────────────────────────────────────

def test_nan_is_treated_as_missing_everywhere(ticker):
    """NaN is truthy and prints as "nan", which is worse than saying so."""
    ticker.info_payload = dict(INFO, marketCap=float("nan"), trailingPE=float("nan"))
    ticker.valuation_payload = pd.DataFrame()
    ticker.fast_payload = {"market_cap": float("nan")}

    figures = fundamentals.fetch("AVGO")

    assert figures.get("pe_ratio") is None
    assert figures.get("market_cap") is None


def test_infinity_is_treated_as_missing():
    assert fundamentals._finite(float("inf")) is None
    assert fundamentals._finite(float("nan")) is None
    assert fundamentals._finite(True) is None, "a bool is not a figure"
    assert fundamentals._finite("34.2") == pytest.approx(34.2)
    assert fundamentals._finite(12.5) == pytest.approx(12.5)
    assert fundamentals._finite(None) is None


def test_nan_never_reaches_the_prompt(ticker, analyst):
    ticker.info_payload = dict(INFO, marketCap=float("nan"), trailingPE=float("nan"),
                               fiftyTwoWeekHigh=float("nan"))
    ticker.valuation_payload = pd.DataFrame()
    extra = analyst._fetch_fundamentals("AVGO")
    prompt = analyst._build_prompt("AVGO", {"price": 352.1, "change_pct": -1.2},
                                   extra, [], {})

    assert "nan" not in prompt.lower()
    assert "אין נתון" in prompt


# ── Sector, name and the prompt ───────────────────────────────────────────────

def test_sector_falls_back_to_the_curated_table(ticker):
    ticker.info_payload = {}
    figures = fundamentals.fetch("AVGO")

    assert figures.get("sector") == "Technology"
    assert figures.sources["sector"] == "reference_data"


def test_the_short_name_falls_back_to_the_symbol(ticker):
    ticker.info_payload = RuntimeError("rate limited")
    assert fundamentals.fetch("AVGO").get("short_name") == "AVGO"


def test_a_missing_sector_alone_does_not_trigger_more_requests(ticker):
    """The curated table covers it, so it is not worth another round trip."""
    ticker.info_payload = dict(INFO)
    del ticker.info_payload["sector"]
    ticker.valuation_payload = AssertionError("must not be asked for a sector")

    assert fundamentals.fetch("AVGO").get("sector") == "Technology"


def test_full_data_produces_every_line_of_the_prompt(ticker, analyst):
    ticker.info_payload = INFO
    prompt = analyst._build_prompt(
        "AVGO", {"price": 352.1, "change_pct": -1.2},
        analyst._fetch_fundamentals("AVGO"), [], {},
    )
    for expected in ("P/E נוכחי: 34.2", "P/E עתידי: 28.1", "PEG: 1.80",
                     "שווי שוק: $1.6T", "מרווח גולמי: 63.0%",
                     "המלצת אנליסטים: buy"):
        assert expected in prompt


def test_missing_multiples_are_labelled_not_omitted(ticker, analyst):
    """The section must still appear, so the model knows the data is absent."""
    for attribute in ("info_payload", "valuation_payload", "ttm_payload",
                      "annual_payload", "quarterly_payload", "fast_payload"):
        setattr(ticker, attribute, RuntimeError("down"))

    prompt = analyst._build_prompt(
        "AVGO", {"price": 352.1, "change_pct": -1.2},
        analyst._fetch_fundamentals("AVGO"), [], {},
    )

    assert prompt.count("אין נתון") >= 4
    assert "None" not in prompt
    assert "N/A" not in prompt, "an English gap reads as a different kind of gap"


def test_the_prompt_says_when_the_block_is_empty_because_of_a_failure(ticker, analyst):
    """A block of "אין נתון" otherwise reads as a company with no multiples."""
    for attribute in ("info_payload", "valuation_payload", "ttm_payload",
                      "annual_payload", "quarterly_payload", "fast_payload"):
        setattr(ticker, attribute, RuntimeError("down"))

    prompt = analyst._build_prompt(
        "AVGO", {"price": 352.1, "change_pct": -1.2},
        analyst._fetch_fundamentals("AVGO"), [], {},
    )

    assert "תקלה בשליפה" in prompt
    assert "אל תסיק" in prompt


def test_no_such_warning_when_the_figures_arrived(ticker, analyst):
    ticker.info_payload = INFO
    prompt = analyst._build_prompt(
        "AVGO", {"price": 352.1, "change_pct": -1.2},
        analyst._fetch_fundamentals("AVGO"), [], {},
    )
    assert "תקלה בשליפה" not in prompt


def test_the_prompt_states_whether_growth_is_quarterly_or_annual(ticker, analyst):
    ticker.info_payload = {}
    ticker.quarterly_payload = pd.DataFrame()
    ticker.annual_payload = _series_statement("Total Revenue", [110.0, 100.0])

    prompt = analyst._build_prompt(
        "AVGO", {"price": 352.1, "change_pct": -1.2},
        analyst._fetch_fundamentals("AVGO"), [], {},
    )

    assert "צמיחת הכנסות (שנתי): 10.0%" in prompt


# ── Caching ───────────────────────────────────────────────────────────────────

def test_the_same_symbol_is_not_re_fetched(ticker):
    """Multiples change once a quarter; every analysis used to re-roll the dice."""
    calls = []

    class Counting(FakeTicker):
        info_payload = INFO

        def __init__(self, symbol):
            calls.append(symbol)
            super().__init__(symbol)

    import pytest as _pytest
    _pytest.MonkeyPatch().setattr(fundamentals.yf, "Ticker", Counting)
    try:
        fundamentals.fetch("AVGO")
        before = len(calls)
        fundamentals.fetch("AVGO")
        assert len(calls) == before
    finally:
        fundamentals.reset_cache()


def test_a_complete_answer_is_kept_for_hours_and_a_partial_one_is_not(ticker):
    ticker.info_payload = INFO
    fundamentals.fetch("AVGO")
    assert fundamentals._cache["AVGO"][2] == fundamentals.CACHE_TTL

    ticker.info_payload = {"trailingPE": 34.2}
    ticker.valuation_payload = pd.DataFrame()
    fundamentals.fetch("NVDA")
    assert fundamentals._cache["NVDA"][2] == fundamentals.CACHE_TTL_PARTIAL, \
        "a throttle must not be cached for six hours"


def test_an_expired_entry_is_re_fetched(ticker, monkeypatch):
    """The lifetime is fixed when the entry is stored, so it is patched first."""
    monkeypatch.setattr(fundamentals, "CACHE_TTL", -1)
    monkeypatch.setattr(fundamentals, "CACHE_TTL_PARTIAL", -1)
    ticker.info_payload = INFO
    fundamentals.fetch("AVGO")

    ticker.info_payload = dict(INFO, trailingPE=40.0)
    assert fundamentals.fetch("AVGO").get("pe_ratio") == pytest.approx(40.0)


def test_an_expired_entry_is_dropped_rather_than_kept(ticker, monkeypatch):
    monkeypatch.setattr(fundamentals, "CACHE_TTL", -1)
    ticker.info_payload = INFO
    fundamentals.fetch("AVGO")
    fundamentals._cached("AVGO")

    assert "AVGO" not in fundamentals._cache


def test_the_cache_can_be_bypassed(ticker):
    ticker.info_payload = INFO
    fundamentals.fetch("AVGO")
    ticker.info_payload = dict(INFO, trailingPE=40.0)

    assert fundamentals.fetch("AVGO", use_cache=False).get("pe_ratio") == 40.0


def test_the_cache_does_not_grow_without_bound(ticker, monkeypatch):
    monkeypatch.setattr(fundamentals, "CACHE_LIMIT", 8)
    ticker.info_payload = INFO
    for n in range(40):
        fundamentals.fetch(f"SYM{n}")

    assert len(fundamentals._cache) <= 8


def test_symbols_are_cached_case_insensitively(ticker):
    ticker.info_payload = INFO
    fundamentals.fetch("avgo")
    assert "AVGO" in fundamentals._cache


# ── Diagnostics ───────────────────────────────────────────────────────────────

def test_status_reports_the_sources_and_a_verdict(ticker):
    ticker.info_payload = INFO
    report = fundamentals.status("AVGO")

    assert report["healthy"] is True
    assert report["sources"]["pe_ratio"] == "quote_summary"
    assert report["values"]["pe_ratio"] == pytest.approx(34.2)


def test_status_calls_an_empty_result_unhealthy(ticker):
    """The probe ticker has a P/E on any day the market exists."""
    for attribute in ("info_payload", "valuation_payload", "ttm_payload",
                      "annual_payload", "quarterly_payload", "fast_payload"):
        setattr(ticker, attribute, RuntimeError("down"))

    report = fundamentals.status("AVGO")

    assert report["healthy"] is False
    assert report["errors"]


def test_status_does_not_serve_a_cached_answer(ticker):
    """A diagnostic that reports the cache cannot diagnose the live state."""
    ticker.info_payload = INFO
    fundamentals.fetch("AVGO")
    ticker.info_payload = RuntimeError("down")
    ticker.valuation_payload = RuntimeError("down")
    ticker.ttm_payload = RuntimeError("down")
    ticker.annual_payload = RuntimeError("down")
    ticker.quarterly_payload = RuntimeError("down")
    ticker.fast_payload = RuntimeError("down")

    assert fundamentals.status("AVGO")["healthy"] is False


def test_the_diagnostics_route_serves_the_status(ticker):
    from fastapi.testclient import TestClient

    from stock_monitor.config import NotificationConfig
    from stock_monitor.notifier import NotificationDispatcher
    from stock_monitor.webhook_server import create_webhook_app

    ticker.info_payload = INFO
    client = TestClient(create_webhook_app(
        NotificationDispatcher(NotificationConfig()), ""))

    body = client.get("/api/diagnostics/fundamentals").json()
    assert body["healthy"] is True
    assert body["sources"]["pe_ratio"] == "quote_summary"


def test_math_is_not_needed_for_a_clean_number():
    """Guard for the import removal: _finite does its own finiteness check."""
    assert math.isfinite(fundamentals._finite(1.0))
