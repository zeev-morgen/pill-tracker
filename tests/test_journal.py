"""Trade journal: recording a sale, the summary maths and the API contract."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from stock_monitor import dashboard
from stock_monitor.config import NotificationConfig
from stock_monitor.notifier import NotificationDispatcher
from stock_monitor.store import (
    ClosedPosition,
    Holding,
    HoldingError,
    closed_position_store,
    portfolio_store,
)
from stock_monitor.webhook_server import create_webhook_app


@pytest.fixture(autouse=True)
def clean_stores():
    """Both stores are module singletons — reset them around every test."""
    for holding in list(portfolio_store.all()):
        portfolio_store.delete(holding.ticker)
    closed_position_store._rows.clear()
    closed_position_store._next_id = 1
    yield
    for holding in list(portfolio_store.all()):
        portfolio_store.delete(holding.ticker)
    closed_position_store._rows.clear()
    closed_position_store._next_id = 1


@pytest.fixture(autouse=True)
def stub_risk_snapshot(monkeypatch):
    """Keep the sell endpoint off the network.

    It snapshots ATR and sector from live prices before closing a position;
    tests need that path exercised, not yfinance.
    """
    monkeypatch.setattr(
        dashboard._risk_analyzer,
        "collect_positions",
        lambda: [{"ticker": "AMZN", "atr_pct": 3.5, "sector": "Consumer Cyclical"}],
    )
    monkeypatch.setattr(dashboard, "get_data_feed", lambda: None)


@pytest.fixture
def client():
    return TestClient(create_webhook_app(NotificationDispatcher(NotificationConfig()), ""))


def _held(ticker="AMZN", quantity=10, entry_price=100.0, days_ago=45):
    return Holding.create(
        ticker=ticker,
        quantity=quantity,
        entry_price=entry_price,
        purchase_date=(date.today() - timedelta(days=days_ago)).isoformat(),
    )


# ── ClosedPosition maths ──────────────────────────────────────────────────────

def test_full_sale_computes_profit_and_holding_time():
    closed = ClosedPosition.from_sale(_held(), quantity_sold=10, exit_price=120.0)
    assert closed.pnl_pct == pytest.approx(20.0)
    assert closed.pnl_value == pytest.approx(200.0)
    assert closed.holding_days == 45
    assert closed.fraction_sold == pytest.approx(1.0)


def test_partial_sale_records_the_fraction_sold():
    closed = ClosedPosition.from_sale(_held(), quantity_sold=4, exit_price=120.0)
    assert closed.fraction_sold == pytest.approx(0.4)
    assert closed.as_dict()["is_partial"] is True
    # P/L is on the sold portion only, not the whole original position.
    assert closed.pnl_value == pytest.approx(80.0)


def test_loss_is_recorded_as_a_negative_return():
    closed = ClosedPosition.from_sale(_held(), quantity_sold=10, exit_price=80.0)
    assert closed.pnl_pct == pytest.approx(-20.0)
    assert closed.pnl_value == pytest.approx(-200.0)


def test_cannot_sell_more_than_is_held():
    with pytest.raises(HoldingError):
        ClosedPosition.from_sale(_held(quantity=10), quantity_sold=11, exit_price=120.0)


def test_sale_before_the_purchase_date_is_rejected():
    with pytest.raises(HoldingError):
        ClosedPosition.from_sale(
            _held(days_ago=10),
            quantity_sold=1,
            exit_price=120.0,
            sold_date=(date.today() - timedelta(days=30)).isoformat(),
        )


def test_holding_days_is_none_without_a_purchase_date():
    holding = Holding.create("AMZN", 10, 100.0)
    closed = ClosedPosition.from_sale(holding, quantity_sold=10, exit_price=110.0)
    assert closed.holding_days is None


# ── Sell endpoint ─────────────────────────────────────────────────────────────

def test_full_sale_removes_the_position(client):
    portfolio_store.upsert(_held())
    response = client.post("/api/holdings/AMZN/sell",
                           json={"quantity": 10, "exit_price": 120.0})
    assert response.status_code == 201
    assert response.json()["remaining_quantity"] == 0
    assert portfolio_store.get("AMZN") is None


def test_partial_sale_leaves_the_remainder_in_the_portfolio(client):
    portfolio_store.upsert(_held())
    response = client.post("/api/holdings/AMZN/sell",
                           json={"quantity": 4, "exit_price": 120.0})
    assert response.status_code == 201
    assert response.json()["remaining_quantity"] == pytest.approx(6.0)

    remaining = portfolio_store.get("AMZN")
    assert remaining.quantity == pytest.approx(6.0)
    # The remainder keeps its original cost basis and open date.
    assert remaining.entry_price == pytest.approx(100.0)
    assert remaining.purchase_date == date.today() - timedelta(days=45)


def test_sale_captures_atr_and_sector_at_close(client):
    """Both are unreconstructable once the position is gone, so they are frozen."""
    portfolio_store.upsert(_held())
    closed = client.post("/api/holdings/AMZN/sell",
                         json={"quantity": 10, "exit_price": 120.0}).json()["closed"]
    assert closed["atr_pct_at_close"] == pytest.approx(3.5)
    assert closed["sector"] == "Consumer Cyclical"


def test_sale_still_records_when_the_risk_snapshot_fails(client, monkeypatch):
    def boom():
        raise RuntimeError("yfinance is down")

    monkeypatch.setattr(dashboard._risk_analyzer, "collect_positions", boom)
    portfolio_store.upsert(_held())
    response = client.post("/api/holdings/AMZN/sell",
                           json={"quantity": 10, "exit_price": 120.0})
    assert response.status_code == 201
    assert response.json()["closed"]["atr_pct_at_close"] is None


def test_selling_an_unheld_ticker_is_404(client):
    assert client.post("/api/holdings/NOPE/sell",
                       json={"quantity": 1, "exit_price": 5.0}).status_code == 404


def test_overselling_is_rejected_with_422(client):
    portfolio_store.upsert(_held(quantity=3))
    response = client.post("/api/holdings/AMZN/sell",
                           json={"quantity": 5, "exit_price": 120.0})
    assert response.status_code == 422


def test_two_partial_sales_produce_two_journal_entries(client):
    portfolio_store.upsert(_held())
    client.post("/api/holdings/AMZN/sell", json={"quantity": 4, "exit_price": 120.0})
    client.post("/api/holdings/AMZN/sell", json={"quantity": 6, "exit_price": 130.0})

    entries = client.get("/api/journal").json()["entries"]
    assert [e["quantity"] for e in entries] == [6, 4] or [e["quantity"] for e in entries] == [4, 6]
    assert portfolio_store.get("AMZN") is None


# ── Journal listing ───────────────────────────────────────────────────────────

def test_empty_journal_reports_zeroed_summary(client):
    body = client.get("/api/journal").json()
    assert body["entries"] == []
    assert body["summary"] == {
        "count": 0, "total_pnl": 0, "win_rate_pct": 0.0, "avg_holding_days": None,
        # Trades whose sale-day exchange rate was never captured, and so are
        # missing from total_pnl. Empty here because there are no trades.
        "unconverted": [],
    }


def test_summary_aggregates_wins_losses_and_holding_time(client):
    closed_position_store.add(
        ClosedPosition.from_sale(_held("AAA", days_ago=10), 10, exit_price=120.0))
    closed_position_store.add(
        ClosedPosition.from_sale(_held("BBB", days_ago=30), 10, exit_price=90.0))

    summary = client.get("/api/journal").json()["summary"]
    assert summary["count"] == 2
    assert summary["total_pnl"] == pytest.approx(100.0)   # +200 and -100
    assert summary["win_rate_pct"] == pytest.approx(50.0)
    assert summary["avg_holding_days"] == 20


def test_summary_ignores_entries_without_a_holding_period(client):
    closed_position_store.add(
        ClosedPosition.from_sale(_held("AAA", days_ago=10), 10, exit_price=120.0))
    closed_position_store.add(
        ClosedPosition.from_sale(Holding.create("BBB", 10, 100.0), 10, exit_price=120.0))
    assert client.get("/api/journal").json()["summary"]["avg_holding_days"] == 10


# ── Personal note ─────────────────────────────────────────────────────────────

def test_personal_note_round_trips(client):
    entry = closed_position_store.add(
        ClosedPosition.from_sale(_held(), 10, exit_price=120.0))
    response = client.patch(f"/api/journal/{entry.id}",
                            json={"personal_note": "מכרתי מוקדם מדי"})
    assert response.status_code == 200
    assert response.json()["personal_note"] == "מכרתי מוקדם מדי"
    assert closed_position_store.get(entry.id).personal_note == "מכרתי מוקדם מדי"


def test_blank_note_clears_the_field(client):
    entry = closed_position_store.add(
        ClosedPosition.from_sale(_held(), 10, exit_price=120.0))
    client.patch(f"/api/journal/{entry.id}", json={"personal_note": "טקסט"})
    client.patch(f"/api/journal/{entry.id}", json={"personal_note": "   "})
    assert closed_position_store.get(entry.id).personal_note is None


def test_patch_cannot_overwrite_the_ai_verdict(client):
    """The rating and analysis are Claude's output — the UI must not set them."""
    entry = closed_position_store.add(
        ClosedPosition.from_sale(_held(), 10, exit_price=120.0))
    response = client.patch(f"/api/journal/{entry.id}",
                            json={"rating": "green", "ai_analysis": "מעולה"})
    assert response.status_code == 422
    assert closed_position_store.get(entry.id).rating is None


def test_patching_a_missing_entry_is_404(client):
    assert client.patch("/api/journal/9999", json={"personal_note": "x"}).status_code == 404


# ── AI review ─────────────────────────────────────────────────────────────────

def test_analyze_requires_a_configured_analyst(client):
    dashboard.set_analyst(None)
    entry = closed_position_store.add(
        ClosedPosition.from_sale(_held(), 10, exit_price=120.0))
    response = client.post(f"/api/journal/{entry.id}/analyze")
    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


def test_analyze_stores_the_rating_and_explanation(client):
    class Stub:
        def review_closed_position(self, entry):
            assert entry["ticker"] == "AMZN"      # the analyst sees the trade
            return {"rating": "green", "explanation": "יציאה במחיר טוב"}

    dashboard.set_analyst(Stub())
    try:
        entry = closed_position_store.add(
            ClosedPosition.from_sale(_held(), 10, exit_price=120.0))
        body = client.post(f"/api/journal/{entry.id}/analyze").json()
        assert body["rating"] == "green"
        assert body["ai_analysis"] == "יציאה במחיר טוב"
        # Persisted, so reopening the tab does not re-spend tokens.
        assert closed_position_store.get(entry.id).rating == "green"
    finally:
        dashboard.set_analyst(None)


def test_analyze_reports_a_missing_entry_rather_than_calling_the_api(client):
    class Stub:
        def review_closed_position(self, entry):
            raise AssertionError("must not be called for a missing entry")

    dashboard.set_analyst(Stub())
    try:
        assert client.post("/api/journal/9999/analyze").status_code == 404
    finally:
        dashboard.set_analyst(None)


# ── Correcting a recorded sale ────────────────────────────────────────────────

def _entry(**overrides):
    from datetime import date as _date

    from stock_monitor.store import ClosedPosition

    base = dict(
        id=1, ticker="MSTU", quantity=28.8, entry_price=25.27, exit_price=32.86,
        purchase_date=_date(2026, 8, 19), sold_date=_date(2026, 8, 31),
        holding_days=12, pnl_pct=30.05, pnl_value=218.70, fraction_sold=0.45,
        rating="green", ai_analysis="ניתוח ישן", personal_note="ההערה שלי",
    )
    base.update(overrides)
    return ClosedPosition(**base)


def test_a_corrected_entry_price_recomputes_the_return():
    """The reported case: shares sold from a second account had a different
    cost basis than the blended average the app recorded them against."""
    revised = _entry().revised(entry_price=21.00)

    assert revised.entry_price == 21.00
    assert revised.pnl_pct == pytest.approx((32.86 - 21.00) / 21.00 * 100)
    assert revised.pnl_value == pytest.approx((32.86 - 21.00) * 28.8)


def test_the_original_figures_are_left_alone():
    """revised() returns a copy — nothing is mutated under the caller."""
    entry = _entry()
    entry.revised(entry_price=21.00)

    assert entry.entry_price == 25.27
    assert entry.pnl_pct == 30.05


def test_a_corrected_quantity_rescales_the_partial_fraction():
    """The original position size is recoverable from quantity and fraction,
    so a new quantity can be expressed against the same total."""
    revised = _entry().revised(quantity=14.4)   # half of what was recorded

    assert revised.fraction_sold == pytest.approx(0.225)


def test_the_fraction_never_exceeds_a_full_exit():
    revised = _entry().revised(quantity=1000)
    assert revised.fraction_sold == 1.0


def test_dates_recompute_the_holding_time():
    from datetime import date as _date

    revised = _entry().revised(purchase_date=_date(2026, 8, 1))
    assert revised.holding_days == 30


def test_a_money_change_drops_the_ai_verdict():
    """A green light beside numbers it never saw is worse than none."""
    revised = _entry().revised(entry_price=21.00)

    assert revised.rating is None
    assert revised.ai_analysis is None


def test_a_date_change_keeps_the_ai_verdict():
    """Holding time is part of the picture, not what the grade is about."""
    from datetime import date as _date

    revised = _entry().revised(purchase_date=_date(2026, 8, 1))
    assert revised.rating == "green"


def test_the_personal_note_always_survives():
    """It is the user's own writing, not something derived from the figures."""
    assert _entry().revised(entry_price=21.00).personal_note == "ההערה שלי"


def test_the_dollar_value_keeps_the_rate_the_sale_actually_got():
    """Re-converting at today's rate would restate a realised figure — the
    exact mistake pnl_value_usd exists to prevent."""
    entry = _entry(currency="ILA", pnl_value=-113355.74, pnl_value_usd=-379.66)
    revised = entry.revised(exit_price=entry.exit_price)   # no change, same value

    implied_before = entry.pnl_value / entry.pnl_value_usd
    implied_after = revised.pnl_value / revised.pnl_value_usd
    assert implied_after == pytest.approx(implied_before)


def test_a_dollar_trade_needs_no_conversion():
    revised = _entry().revised(entry_price=21.00)
    assert revised.pnl_value_usd == pytest.approx(revised.pnl_value)


@pytest.mark.parametrize("field,value", [
    ("quantity", 0), ("entry_price", -1), ("exit_price", "abc"),
])
def test_an_impossible_figure_is_rejected(field, value):
    from stock_monitor.store import HoldingError

    with pytest.raises(HoldingError):
        _entry().revised(**{field: value})


def test_a_sale_before_the_purchase_is_rejected():
    from datetime import date as _date

    from stock_monitor.store import HoldingError

    with pytest.raises(HoldingError):
        _entry().revised(sold_date=_date(2026, 8, 1))


def test_an_unknown_field_is_refused():
    with pytest.raises(ValueError):
        _entry().revised(ticker="OTHER")


# ── Over HTTP ─────────────────────────────────────────────────────────────────

def test_a_sale_can_be_corrected_over_http(client):
    from datetime import date as _date

    from stock_monitor.store import ClosedPosition

    stored = closed_position_store.add(ClosedPosition(
        ticker="MSTU", quantity=28.8, entry_price=25.27, exit_price=32.86,
        sold_date=_date(2026, 8, 31), pnl_pct=30.05, pnl_value=218.70,
        fraction_sold=0.45, rating="green"))

    body = client.put(f"/api/journal/{stored.id}",
                      json={"entry_price": 21.00}).json()

    assert body["entry_price"] == pytest.approx(21.00)
    assert body["pnl_pct"] == pytest.approx(56.48, abs=0.01)
    assert body["rating"] is None


def test_correcting_a_missing_entry_is_a_404(client):
    assert client.put("/api/journal/99999", json={"entry_price": 1.0}).status_code == 404


def test_a_request_with_nothing_to_change_is_refused(client):
    assert client.put("/api/journal/1", json={}).status_code == 422


def test_an_invalid_correction_is_refused_with_a_reason(client):
    from datetime import date as _date

    from stock_monitor.store import ClosedPosition

    stored = closed_position_store.add(ClosedPosition(
        ticker="CF", quantity=8, entry_price=75.0, exit_price=92.0,
        sold_date=_date(2026, 8, 3), pnl_pct=22.67, pnl_value=136.0))

    response = client.put(f"/api/journal/{stored.id}", json={"entry_price": 0})
    assert response.status_code == 422
    assert "מחיר כניסה" in response.json()["detail"]


def test_the_note_endpoint_still_only_takes_a_note(client):
    """PATCH sets one field as given; PUT restates the trade. Keeping them
    apart is what stops a note save from silently rewriting figures."""
    assert client.patch("/api/journal/1", json={"entry_price": 5}).status_code == 422
