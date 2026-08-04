"""Averaging an extra purchase into an existing position.

Saving an existing ticker *replaces* it, so topping up by hand meant computing
the weighted average yourself — and typing the added quantity instead of the
new total silently deleted the shares already held.
"""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from stock_monitor.config import NotificationConfig
from stock_monitor.notifier import NotificationDispatcher
from stock_monitor.store import Holding, HoldingError, portfolio_store
from stock_monitor.webhook_server import create_webhook_app


@pytest.fixture(autouse=True)
def clean_portfolio():
    for holding in list(portfolio_store.all()):
        portfolio_store.delete(holding.ticker)
    yield
    for holding in list(portfolio_store.all()):
        portfolio_store.delete(holding.ticker)


@pytest.fixture
def client():
    return TestClient(create_webhook_app(NotificationDispatcher(NotificationConfig()), ""))


# ── The arithmetic ────────────────────────────────────────────────────────────

def test_the_worked_example():
    """6.76 @ $374.67 plus 3 @ $392.00 averages to $379.9968 over 9.76 shares."""
    holding = Holding.create("AVGO", 6.76, 374.67)
    updated = holding.add_shares(3, 392.00)

    assert updated.quantity == pytest.approx(9.76)
    assert updated.entry_price == pytest.approx(379.9968, abs=1e-4)


def test_the_average_lands_between_the_two_prices():
    updated = Holding.create("AMZN", 10, 100.0).add_shares(10, 200.0)
    assert updated.entry_price == pytest.approx(150.0)   # equal weights


def test_a_small_top_up_barely_moves_the_average():
    updated = Holding.create("AMZN", 100, 100.0).add_shares(1, 200.0)
    assert updated.entry_price == pytest.approx(100.9901, abs=1e-4)


def test_buying_lower_reduces_the_average():
    updated = Holding.create("AMZN", 10, 100.0).add_shares(10, 50.0)
    assert updated.entry_price == pytest.approx(75.0)
    assert updated.entry_price < 100.0


def test_fractional_shares_are_handled():
    updated = Holding.create("AMZN", 0.5, 100.0).add_shares(0.25, 200.0)
    assert updated.quantity == pytest.approx(0.75)
    assert updated.entry_price == pytest.approx(133.3333, abs=1e-4)


def test_shares_already_held_are_never_lost():
    """The failure mode this replaces: the old quantity being overwritten."""
    original = Holding.create("AVGO", 6.76, 374.67)
    updated = original.add_shares(3, 392.00)
    assert updated.quantity > original.quantity


def test_total_cost_is_preserved():
    """Quantity times average must equal what was actually paid, in total."""
    original = Holding.create("AMZN", 7.3, 118.44)
    updated = original.add_shares(4.1, 203.77)
    paid = original.quantity * original.entry_price + 4.1 * 203.77
    assert updated.quantity * updated.entry_price == pytest.approx(paid, abs=1e-4)


# ── Validation ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("quantity", [0, -1, "abc", None])
def test_a_bad_quantity_is_rejected(quantity):
    with pytest.raises(HoldingError):
        Holding.create("AMZN", 10, 100.0).add_shares(quantity, 200.0)


@pytest.mark.parametrize("price", [0, -5, "abc", None])
def test_a_bad_price_is_rejected(price):
    with pytest.raises(HoldingError):
        Holding.create("AMZN", 10, 100.0).add_shares(10, price)


def test_numeric_strings_from_json_are_accepted():
    updated = Holding.create("AMZN", 10, 100.0).add_shares("10", "200")
    assert updated.entry_price == pytest.approx(150.0)


# ── Metadata carried across ───────────────────────────────────────────────────

def test_the_original_purchase_date_survives():
    """Holding time runs from when the position was opened, not the top-up."""
    opened = date.today() - timedelta(days=45)
    updated = Holding.create(
        "AMZN", 10, 100.0, purchase_date=opened.isoformat()
    ).add_shares(5, 120.0, purchase_date=date.today().isoformat())
    assert updated.purchase_date == opened


def test_a_date_is_taken_only_when_none_was_recorded():
    bought = date.today() - timedelta(days=3)
    updated = Holding.create("AMZN", 10, 100.0).add_shares(
        5, 120.0, purchase_date=bought.isoformat()
    )
    assert updated.purchase_date == bought


def test_a_future_purchase_date_is_rejected():
    ahead = (date.today() + timedelta(days=1)).isoformat()
    with pytest.raises(HoldingError):
        Holding.create("AMZN", 10, 100.0).add_shares(5, 120.0, purchase_date=ahead)


def test_manual_overrides_are_kept():
    updated = Holding.create(
        "SPY", 10, 100.0, sector="Diversified", asset_type="etf"
    ).add_shares(5, 120.0)
    assert (updated.sector, updated.asset_type) == ("Diversified", "etf")


# ── Endpoint ──────────────────────────────────────────────────────────────────

def test_the_endpoint_averages_and_persists(client):
    portfolio_store.upsert(Holding.create("AVGO", 6.76, 374.67))
    response = client.post("/api/holdings/AVGO/add", json={"quantity": 3, "price": 392.0})

    assert response.status_code == 201
    body = response.json()
    assert body["holding"]["quantity"] == pytest.approx(9.76)
    assert body["holding"]["entry_price"] == pytest.approx(379.9968, abs=1e-4)
    # The caller gets the before-state, so the UI can show what changed.
    assert body["previous"] == {"quantity": 6.76, "entry_price": 374.67}

    stored = portfolio_store.get("AVGO")
    assert stored.quantity == pytest.approx(9.76)


def test_topping_up_twice_compounds_correctly(client):
    portfolio_store.upsert(Holding.create("AMZN", 10, 100.0))
    client.post("/api/holdings/AMZN/add", json={"quantity": 10, "price": 200.0})
    client.post("/api/holdings/AMZN/add", json={"quantity": 20, "price": 100.0})

    stored = portfolio_store.get("AMZN")
    assert stored.quantity == pytest.approx(40.0)
    # 1000 + 2000 + 2000 paid over 40 shares
    assert stored.entry_price == pytest.approx(125.0)


def test_adding_to_a_ticker_not_held_is_404(client):
    assert client.post("/api/holdings/NOPE/add",
                       json={"quantity": 1, "price": 5.0}).status_code == 404


def test_the_endpoint_rejects_bad_input(client):
    portfolio_store.upsert(Holding.create("AMZN", 10, 100.0))
    assert client.post("/api/holdings/AMZN/add",
                       json={"quantity": -1, "price": 5.0}).status_code == 422
    # Nothing was written.
    assert portfolio_store.get("AMZN").quantity == pytest.approx(10.0)


def test_it_is_case_insensitive_like_the_rest_of_the_api(client):
    portfolio_store.upsert(Holding.create("AMZN", 10, 100.0))
    assert client.post("/api/holdings/amzn/add",
                       json={"quantity": 10, "price": 200.0}).status_code == 201
    assert portfolio_store.get("AMZN").entry_price == pytest.approx(150.0)


def test_add_is_post_only(client):
    portfolio_store.upsert(Holding.create("AMZN", 10, 100.0))
    assert client.get("/api/holdings/AMZN/add").status_code == 405


def test_the_replace_route_still_replaces(client):
    """Editing must keep its old meaning — it is how a mistake gets corrected."""
    portfolio_store.upsert(Holding.create("AMZN", 10, 100.0))
    client.post("/api/holdings", json={"ticker": "AMZN", "quantity": 3, "entry_price": 50.0})
    stored = portfolio_store.get("AMZN")
    assert (stored.quantity, stored.entry_price) == (3.0, 50.0)
