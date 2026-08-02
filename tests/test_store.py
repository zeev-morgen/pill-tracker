from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from store import Holding, PortfolioStore


def make_store() -> PortfolioStore:
    return PortfolioStore(path=None)  # in-memory only


def test_upsert_and_get():
    store = make_store()
    holding = Holding(ticker="aapl", quantity=10, purchase_date=date(2024, 1, 2))
    store.upsert(holding)
    fetched = store.get("AAPL")
    assert fetched is not None
    assert fetched.ticker == "AAPL"  # normalized to uppercase
    assert fetched.quantity == 10


def test_upsert_overwrites_existing_ticker():
    store = make_store()
    store.upsert(Holding(ticker="MSFT", quantity=5, purchase_date=date(2024, 1, 2)))
    store.upsert(Holding(ticker="MSFT", quantity=8, purchase_date=date(2024, 6, 1)))
    assert len(store.all()) == 1
    assert store.get("MSFT").quantity == 8


def test_delete():
    store = make_store()
    store.upsert(Holding(ticker="NVDA", quantity=3, purchase_date=date(2024, 1, 2)))
    assert store.delete("nvda") is True
    assert store.delete("nvda") is False
    assert store.get("NVDA") is None


@pytest.mark.parametrize("quantity", [0, -5])
def test_rejects_non_positive_quantity(quantity):
    with pytest.raises(ValidationError):
        Holding(ticker="AAPL", quantity=quantity, purchase_date=date(2024, 1, 2))


def test_rejects_future_purchase_date():
    with pytest.raises(ValidationError):
        Holding(ticker="AAPL", quantity=1, purchase_date=date.today() + timedelta(days=1))


def test_rejects_invalid_ticker_characters():
    with pytest.raises(ValidationError):
        Holding(ticker="AA PL;", quantity=1, purchase_date=date(2024, 1, 2))


def test_persistence_round_trip(tmp_path):
    path = tmp_path / "portfolio.json"
    store = PortfolioStore(path=path)
    store.upsert(Holding(ticker="AAPL", quantity=2.5, purchase_date=date(2024, 3, 4)))

    reloaded = PortfolioStore(path=path)
    holding = reloaded.get("AAPL")
    assert holding is not None
    assert holding.quantity == 2.5
    assert holding.purchase_date == date(2024, 3, 4)
