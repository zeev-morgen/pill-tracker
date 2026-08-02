import pytest
from pydantic import ValidationError

from store import Holding, PortfolioStore


def make_store() -> PortfolioStore:
    return PortfolioStore(path=None)  # in-memory only


def test_upsert_and_get():
    store = make_store()
    holding = Holding(ticker="aapl", quantity=10, entry_price=187.5)
    store.upsert(holding)
    fetched = store.get("AAPL")
    assert fetched is not None
    assert fetched.ticker == "AAPL"  # normalized to uppercase
    assert fetched.quantity == 10
    assert fetched.entry_price == 187.5


def test_upsert_overwrites_existing_ticker():
    store = make_store()
    store.upsert(Holding(ticker="MSFT", quantity=5, entry_price=300))
    store.upsert(Holding(ticker="MSFT", quantity=8, entry_price=310))
    assert len(store.all()) == 1
    assert store.get("MSFT").quantity == 8
    assert store.get("MSFT").entry_price == 310


def test_delete():
    store = make_store()
    store.upsert(Holding(ticker="NVDA", quantity=3, entry_price=120))
    assert store.delete("nvda") is True
    assert store.delete("nvda") is False
    assert store.get("NVDA") is None


@pytest.mark.parametrize("quantity", [0, -5])
def test_rejects_non_positive_quantity(quantity):
    with pytest.raises(ValidationError):
        Holding(ticker="AAPL", quantity=quantity, entry_price=100)


@pytest.mark.parametrize("entry_price", [0, -10])
def test_rejects_non_positive_entry_price(entry_price):
    with pytest.raises(ValidationError):
        Holding(ticker="AAPL", quantity=1, entry_price=entry_price)


def test_rejects_invalid_ticker_characters():
    with pytest.raises(ValidationError):
        Holding(ticker="AA PL;", quantity=1, entry_price=100)


def test_persistence_round_trip(tmp_path):
    path = tmp_path / "portfolio.json"
    store = PortfolioStore(path=path)
    store.upsert(Holding(ticker="AAPL", quantity=2.5, entry_price=187.5))

    reloaded = PortfolioStore(path=path)
    holding = reloaded.get("AAPL")
    assert holding is not None
    assert holding.quantity == 2.5
    assert holding.entry_price == 187.5


def test_load_skips_legacy_records_without_entry_price(tmp_path):
    path = tmp_path / "portfolio.json"
    path.write_text(
        '[{"ticker": "OLD", "quantity": 5, "purchase_date": "2024-01-02"},'
        ' {"ticker": "NEW", "quantity": 3, "entry_price": 50.0}]',
        encoding="utf-8",
    )
    store = PortfolioStore(path=path)
    assert store.get("OLD") is None      # legacy record dropped, not crashing
    assert store.get("NEW").entry_price == 50.0
