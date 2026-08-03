"""Store tests — run against the in-memory fallback (no DATABASE_URL needed)."""

import pytest

from stock_monitor.store import AlertStore, Holding, HoldingError, PortfolioStore


# ── Holding validation ────────────────────────────────────────────────────────

def test_holding_normalizes_ticker():
    holding = Holding.create(ticker=" aapl ", quantity=10, entry_price=187.5)
    assert holding.ticker == "AAPL"
    assert holding.quantity == 10.0
    assert holding.entry_price == 187.5


def test_holding_accepts_numeric_strings_from_json():
    holding = Holding.create(ticker="MU", quantity="2.5", entry_price="98.25")
    assert holding.quantity == 2.5
    assert holding.entry_price == 98.25


@pytest.mark.parametrize("quantity", [0, -5])
def test_holding_rejects_non_positive_quantity(quantity):
    with pytest.raises(HoldingError):
        Holding.create(ticker="AAPL", quantity=quantity, entry_price=100)


@pytest.mark.parametrize("entry_price", [0, -10])
def test_holding_rejects_non_positive_entry_price(entry_price):
    with pytest.raises(HoldingError):
        Holding.create(ticker="AAPL", quantity=1, entry_price=entry_price)


def test_holding_rejects_invalid_ticker():
    with pytest.raises(HoldingError):
        Holding.create(ticker="AA PL;", quantity=1, entry_price=100)


def test_holding_rejects_empty_ticker():
    with pytest.raises(HoldingError):
        Holding.create(ticker="   ", quantity=1, entry_price=100)


def test_holding_rejects_non_numeric_input():
    with pytest.raises(HoldingError):
        Holding.create(ticker="AAPL", quantity="abc", entry_price=100)


# ── PortfolioStore ────────────────────────────────────────────────────────────

def test_upsert_and_get():
    store = PortfolioStore()
    store.upsert(Holding.create("aapl", 10, 187.5))
    fetched = store.get("AAPL")
    assert fetched.ticker == "AAPL"
    assert fetched.entry_price == 187.5


def test_upsert_edits_existing_position():
    store = PortfolioStore()
    store.upsert(Holding.create("MSFT", 5, 300))
    store.upsert(Holding.create("MSFT", 8, 310))
    assert len(store.all()) == 1
    assert store.get("MSFT").quantity == 8
    assert store.get("MSFT").entry_price == 310


def test_get_is_case_insensitive():
    store = PortfolioStore()
    store.upsert(Holding.create("NVDA", 3, 120))
    assert store.get("nvda") is not None


def test_delete():
    store = PortfolioStore()
    store.upsert(Holding.create("NVDA", 3, 120))
    assert store.delete("nvda") is True
    assert store.delete("nvda") is False
    assert store.get("NVDA") is None


def test_all_returns_every_position():
    store = PortfolioStore()
    store.upsert(Holding.create("AMZN", 1, 200))
    store.upsert(Holding.create("META", 2, 500))
    assert {h.ticker for h in store.all()} == {"AMZN", "META"}


# ── AlertStore ────────────────────────────────────────────────────────────────

def test_alert_store_returns_newest_first():
    store = AlertStore()
    store.add("AAPL", "price_change_pct", "first", "INFO")
    store.add("MU", "volume_spike", "second", "WARNING")
    recent = store.recent()
    assert [a.symbol for a in recent] == ["MU", "AAPL"]


def test_alert_store_respects_limit():
    store = AlertStore()
    for i in range(10):
        store.add("AAPL", "price_change_pct", f"msg {i}", "INFO")
    assert len(store.recent(3)) == 3


# ── Database initialization resilience ────────────────────────────────────────

@pytest.mark.parametrize(
    "bad_url",
    [
        "postgresql://...placeholder...",        # placeholder pasted verbatim
        "postgresql://u:p@no-such-host.invalid/db",
        "not-even-a-url",
        "",
    ],
)
def test_bad_database_url_degrades_instead_of_crashing(bad_url):
    """A broken DATABASE_URL must not take the monitor down with it."""
    from stock_monitor import db

    assert db.init_db(bad_url) is False
    assert db.is_enabled() is False


# ── Manual sector / asset-type overrides ──────────────────────────────────────

def test_sector_override_is_stored():
    store = PortfolioStore()
    store.upsert(Holding.create("AVGO", 6.76, 374.67, sector="Technology"))
    assert store.get("AVGO").sector == "Technology"


def test_blank_sector_means_no_override():
    # An empty field must fall back to yfinance, not store an empty sector.
    holding = Holding.create("AVGO", 1, 100, sector="   ")
    assert holding.sector is None


def test_sector_override_can_be_cleared_by_saving_blank():
    store = PortfolioStore()
    store.upsert(Holding.create("AVGO", 1, 100, sector="Technology"))
    store.upsert(Holding.create("AVGO", 1, 100, sector=""))
    assert store.get("AVGO").sector is None


def test_overlong_sector_is_rejected():
    with pytest.raises(HoldingError):
        Holding.create("AVGO", 1, 100, sector="x" * 65)


@pytest.mark.parametrize("asset_type,expected", [("etf", "etf"), ("ETF", "etf"), ("", None), (None, None)])
def test_asset_type_is_normalized(asset_type, expected):
    assert Holding.create("VOO", 1, 100, asset_type=asset_type).asset_type == expected


def test_invalid_asset_type_is_rejected():
    with pytest.raises(HoldingError):
        Holding.create("VOO", 1, 100, asset_type="bond")
