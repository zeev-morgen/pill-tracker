"""Tel Aviv positions, and the two conversions that stand between them and a total.

The dangerous one is agorot. Yahoo quotes TASE equities in hundredths of a
shekel and labels them ``ILA``, so a share priced 3,450 is worth ₪34.50. Read
as shekels it is a hundredfold overstatement; read as dollars, three hundred
and sixty fold. Either mistake propagates silently into the portfolio total,
every sector weight and every allocation slice, and nothing on screen would
look obviously wrong. Most of these tests exist to pin that factor down.
"""

import pytest

from stock_monitor import fx


@pytest.fixture(autouse=True)
def clean_cache():
    fx.reset_cache()
    yield
    fx.reset_cache()


@pytest.fixture
def rate(monkeypatch):
    """Pins the rate at 3.60 shekels to the dollar."""
    monkeypatch.setattr(fx, "_fetch_rate", lambda: 3.60)
    return 3.60


# ── Agorot ────────────────────────────────────────────────────────────────────

def test_agorot_become_shekels(rate):
    """3,450 agorot is ₪34.50 — the conversion the whole feature rests on."""
    assert fx.to_shekels(3450, "ILA") == pytest.approx(34.50)


def test_shekels_stay_shekels(rate):
    assert fx.to_shekels(34.50, "ILS") == pytest.approx(34.50)


def test_agorot_reach_dollars_through_both_steps(rate):
    """₪34.50 at 3.60 is $9.583 — not $958, and not $12,417."""
    assert fx.to_usd(3450, "ILA") == pytest.approx(34.50 / 3.60)


def test_shekels_reach_dollars(rate):
    assert fx.to_usd(34.50, "ILS") == pytest.approx(34.50 / 3.60)


def test_the_currency_code_is_case_insensitive(rate):
    assert fx.to_usd(3450, "ila") == pytest.approx(fx.to_usd(3450, "ILA"))


def test_dollars_are_left_alone(rate, monkeypatch):
    """A US portfolio must not touch the FX layer at all."""
    monkeypatch.setattr(fx, "_fetch_rate", lambda: pytest.fail("no rate needed"))
    assert fx.to_usd(392.23, "USD") == 392.23


def test_a_missing_currency_is_treated_as_dollars(rate, monkeypatch):
    """Every holding predating this feature has no currency recorded."""
    monkeypatch.setattr(fx, "_fetch_rate", lambda: pytest.fail("no rate needed"))
    assert fx.to_usd(392.23, "") == 392.23
    assert fx.to_usd(392.23, None) == 392.23


def test_an_unknown_currency_converts_to_nothing(rate):
    """Better no number than a euro amount silently counted as dollars."""
    assert fx.to_usd(100, "EUR") is None
    assert fx.to_shekels(100, "EUR") is None


@pytest.mark.parametrize("amount", [None, "", "abc", float("nan")])
def test_an_unusable_amount_converts_to_none(rate, amount):
    assert fx.to_usd(amount, "ILA") is None


def test_a_negative_amount_converts_with_its_sign(rate):
    """Losses go through the same path as gains."""
    assert fx.to_usd(-3450, "ILA") == pytest.approx(-34.50 / 3.60)


# ── The rate ──────────────────────────────────────────────────────────────────

def test_the_rate_is_fetched_and_returned(rate):
    assert fx.usd_ils_rate() == pytest.approx(3.60)


def test_the_rate_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(fx, "_fetch_rate", lambda: calls.append(1) or 3.60)

    fx.usd_ils_rate()
    fx.usd_ils_rate()
    fx.usd_ils_rate()
    assert len(calls) == 1, "a portfolio refresh must not re-fetch per holding"


def test_force_bypasses_the_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(fx, "_fetch_rate", lambda: calls.append(1) or 3.60)

    fx.usd_ils_rate()
    fx.usd_ils_rate(force=True)
    assert len(calls) == 2


def test_an_expired_cache_refetches(monkeypatch):
    calls = []
    monkeypatch.setattr(fx, "_fetch_rate", lambda: calls.append(1) or 3.60)
    fx.usd_ils_rate()
    monkeypatch.setattr(fx, "TTL", -1)

    fx.usd_ils_rate()
    assert len(calls) == 2


def test_no_rate_means_no_conversion_not_a_guess(monkeypatch):
    """Converting at a made-up rate produces a total that looks right."""
    monkeypatch.setattr(fx, "_fetch_rate", lambda: None)
    assert fx.usd_ils_rate() is None
    assert fx.to_usd(3450, "ILA") is None


@pytest.mark.parametrize("bad", [0, -3.6, 0.4, 50.0, float("nan")])
def test_an_implausible_rate_is_rejected(monkeypatch, bad):
    """A parsing slip that returns 0 would divide the portfolio by zero."""
    monkeypatch.setattr(fx, "_fetch_rate", lambda: bad)
    assert fx.usd_ils_rate() is None


def test_a_bad_fetch_falls_back_to_the_last_good_rate(monkeypatch):
    """One failed refresh must not make Israeli positions vanish."""
    monkeypatch.setattr(fx, "_fetch_rate", lambda: 3.60)
    fx.usd_ils_rate()

    monkeypatch.setattr(fx, "TTL", -1)
    monkeypatch.setattr(fx, "_fetch_rate", lambda: None)
    assert fx.usd_ils_rate() == pytest.approx(3.60)


def test_a_fetch_exception_is_not_raised_at_the_caller(monkeypatch):
    import yfinance as yf

    def boom(*args, **kwargs):
        raise RuntimeError("429")

    monkeypatch.setattr(yf, "Ticker", boom)
    assert fx._fetch_rate() is None


def test_an_explicit_rate_overrides_the_cached_one(monkeypatch):
    """The portfolio values every row against one rate, passed down."""
    monkeypatch.setattr(fx, "_fetch_rate", lambda: pytest.fail("must not fetch"))
    assert fx.to_usd(3450, "ILA", rate=4.0) == pytest.approx(34.50 / 4.0)


# ── Labelling ─────────────────────────────────────────────────────────────────

def test_israeli_currencies_are_recognised():
    assert fx.is_israeli("ILA") and fx.is_israeli("ILS")
    assert not fx.is_israeli("USD")
    assert not fx.is_israeli("")


def test_agorot_and_shekels_get_different_symbols():
    """Printing agorot with a ₪ is still wrong by a hundred."""
    assert fx.display_symbol("ILA") != fx.display_symbol("ILS")
    assert fx.display_symbol("USD") == "$"


# ── The journal ───────────────────────────────────────────────────────────────

def test_a_tase_sale_records_its_currency_and_dollar_pnl(rate):
    """The journal is the historical record; unlabelled agorot are permanent."""
    from stock_monitor.store import ClosedPosition, Holding

    holding = Holding.create("POLI.TA", 10, 3000.0)
    closed = ClosedPosition.from_sale(holding, 10, 3450.0, currency="ILA")

    assert closed.currency == "ILA"
    assert closed.pnl_value == pytest.approx(4500.0), "agorot, as traded"
    assert closed.pnl_value_usd == pytest.approx(45.0 / 3.60), "₪45 at 3.60"


def test_a_us_sale_needs_no_conversion(rate, monkeypatch):
    from stock_monitor.store import ClosedPosition, Holding

    monkeypatch.setattr(fx, "_fetch_rate", lambda: pytest.fail("no rate needed"))
    closed = ClosedPosition.from_sale(Holding.create("AVGO", 2, 90.0), 2, 100.0)

    assert closed.currency == ""
    assert closed.pnl_value_usd == pytest.approx(20.0)


def test_the_dollar_pnl_uses_the_sale_day_rate_not_todays(rate):
    """Recomputing later would report a profit the trade never made."""
    from stock_monitor.store import ClosedPosition, Holding

    closed = ClosedPosition.from_sale(
        Holding.create("POLI.TA", 10, 3000.0), 10, 3450.0, currency="ILA")
    at_sale = closed.pnl_value_usd

    fx.reset_cache()
    # The rate moves afterwards; the stored figure must not.
    assert closed.pnl_value_usd == at_sale


def test_a_sale_without_a_rate_records_no_dollar_pnl(monkeypatch):
    """None, not a guess — and the summary reports the gap by name."""
    from stock_monitor.store import ClosedPosition, Holding

    monkeypatch.setattr(fx, "_fetch_rate", lambda: None)
    closed = ClosedPosition.from_sale(
        Holding.create("POLI.TA", 10, 3000.0), 10, 3450.0, currency="ILA")

    assert closed.pnl_value_usd is None
    assert closed.pnl_value == pytest.approx(4500.0), "the agorot figure survives"


def test_the_journal_total_adds_dollars_only(rate):
    """Summing pnl_value across currencies produces a plausible wrong number."""
    from stock_monitor.dashboard import journal_summary
    from stock_monitor.store import ClosedPosition, Holding

    tase = ClosedPosition.from_sale(
        Holding.create("POLI.TA", 10, 3000.0), 10, 3450.0, currency="ILA")
    us = ClosedPosition.from_sale(Holding.create("AVGO", 2, 90.0), 2, 100.0)

    summary = journal_summary([tase.as_dict(), us.as_dict()])
    assert summary["total_pnl"] == pytest.approx(round(45.0 / 3.60, 2) + 20.0, abs=0.02)
    assert summary["total_pnl"] < 100, "4,500 agorot leaked into the dollar total"
    assert summary["unconverted"] == []


def test_an_unconvertible_trade_is_named_not_silently_dropped(monkeypatch):
    from stock_monitor.dashboard import journal_summary
    from stock_monitor.store import ClosedPosition, Holding

    monkeypatch.setattr(fx, "_fetch_rate", lambda: None)
    tase = ClosedPosition.from_sale(
        Holding.create("POLI.TA", 10, 3000.0), 10, 3450.0, currency="ILA")

    summary = journal_summary([tase.as_dict()])
    assert summary["total_pnl"] == 0
    assert summary["unconverted"] == ["POLI.TA"]


def test_a_journal_row_written_before_this_feature_reads_as_dollars(rate):
    """Every existing entry has no currency, and they were all US trades."""
    from stock_monitor.store import ClosedPosition

    legacy = ClosedPosition(
        ticker="CF", quantity=8, entry_price=75.0, exit_price=92.0,
        sold_date=__import__("datetime").date(2026, 8, 3),
        pnl_pct=22.67, pnl_value=136.0,
    )
    row = legacy.as_dict()
    assert row["currency"] == ""
    assert row["pnl_value_usd"] == pytest.approx(136.0)


# ── The live-monitor tab ──────────────────────────────────────────────────────

def test_the_live_quote_carries_its_currency():
    """The screenshot bug: Teva at 10,500 agorot rendered as $10,500."""
    from stock_monitor.data_feed import quote_currency

    assert quote_currency("TEVA.TA") == "ILA"
    assert quote_currency("AVGO") == ""


def test_the_suffix_rule_ignores_case():
    from stock_monitor.data_feed import quote_currency

    assert quote_currency("teva.ta") == "ILA"


def test_the_price_cache_keeps_the_currency():
    """It is dropped there and the live tab has nothing left to format with."""
    from stock_monitor import dashboard

    dashboard.update_price_cache({
        "symbol": "TEVA.TA", "price": 10500.0, "change_pct": 1.06,
        "volume": 0, "session": "pre", "currency": "ILA",
    })
    assert dashboard._price_cache["TEVA.TA"]["currency"] == "ILA"
    dashboard._price_cache.pop("TEVA.TA", None)


def test_a_us_quote_still_has_no_currency():
    from stock_monitor import dashboard

    dashboard.update_price_cache({
        "symbol": "AVGO", "price": 418.28, "change_pct": 0.1,
        "volume": 100, "session": "pre",
    })
    assert dashboard._price_cache["AVGO"]["currency"] == ""
    dashboard._price_cache.pop("AVGO", None)
