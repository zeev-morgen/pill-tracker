import numpy as np
import pandas as pd
import pytest

from stock_monitor.portfolio_risk import (
    RiskThresholds,
    build_allocation,
    build_sector_report,
    build_volatility_report,
    compute_atr,
)

THRESHOLDS = RiskThresholds(
    high_atr_pct=4.0, volatility_exposure_pct=30.0, sector_concentration_pct=40.0
)


def make_history(days: int = 60, price: float = 100.0, daily_range: float = 2.0) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=days, freq="B")
    close = np.full(days, price)
    return pd.DataFrame(
        {"High": close + daily_range / 2, "Low": close - daily_range / 2, "Close": close},
        index=idx,
    )


# ── compute_atr ───────────────────────────────────────────────────────────────

def test_atr_matches_constant_range():
    # With a constant daily range and flat closes, ATR converges to the range.
    atr = compute_atr(make_history(days=200, daily_range=3.0), period=14)
    assert atr == pytest.approx(3.0, rel=1e-3)


def test_atr_none_when_insufficient_data():
    assert compute_atr(make_history(days=10), period=14) is None


def test_atr_none_on_missing_columns():
    df = pd.DataFrame({"Close": np.arange(50.0)})
    assert compute_atr(df) is None


# ── volatility exposure ───────────────────────────────────────────────────────

def test_volatility_alert_triggers_above_threshold():
    positions = [
        {"ticker": "WILD", "market_value": 5000.0, "atr_pct": 6.5},
        {"ticker": "CALM", "market_value": 5000.0, "atr_pct": 1.0},
    ]
    report = build_volatility_report(positions, THRESHOLDS)
    assert report["exposure_pct"] == 50.0
    assert report["alert"] is True
    assert report["high_volatility_positions"][0]["ticker"] == "WILD"


def test_volatility_no_alert_below_threshold():
    positions = [
        {"ticker": "WILD", "market_value": 1000.0, "atr_pct": 6.5},
        {"ticker": "CALM", "market_value": 9000.0, "atr_pct": 1.0},
    ]
    report = build_volatility_report(positions, THRESHOLDS)
    assert report["exposure_pct"] == 10.0
    assert report["alert"] is False


def test_volatility_ignores_positions_without_atr():
    report = build_volatility_report(
        [{"ticker": "NEW", "market_value": 1000.0, "atr_pct": None}], THRESHOLDS
    )
    assert report["high_volatility_positions"] == []
    assert report["alert"] is False


def test_volatility_empty_portfolio():
    report = build_volatility_report([], THRESHOLDS)
    assert report["exposure_pct"] == 0.0
    assert report["alert"] is False


# ── sector concentration ──────────────────────────────────────────────────────

def test_sector_alert_on_concentration():
    positions = [
        {"ticker": "AAPL", "market_value": 6000.0, "sector": "Technology"},
        {"ticker": "XOM", "market_value": 4000.0, "sector": "Energy"},
    ]
    report = build_sector_report(positions, THRESHOLDS)
    assert report["alert"] is True
    assert report["concentrated_sectors"][0]["sector"] == "Technology"
    assert report["concentrated_sectors"][0]["weight_pct"] == 60.0


def test_sector_no_alert_when_diversified():
    positions = [
        {"ticker": "A", "market_value": 3000.0, "sector": "Technology"},
        {"ticker": "B", "market_value": 3500.0, "sector": "Energy"},
        {"ticker": "C", "market_value": 3500.0, "sector": "Healthcare"},
    ]
    report = build_sector_report(positions, THRESHOLDS)
    assert report["alert"] is False
    assert len(report["sectors"]) == 3


def test_sector_weights_sum_to_100():
    positions = [
        {"ticker": "A", "market_value": 1234.56, "sector": "Technology"},
        {"ticker": "B", "market_value": 7890.12, "sector": "Energy"},
    ]
    report = build_sector_report(positions, THRESHOLDS)
    assert sum(s["weight_pct"] for s in report["sectors"]) == pytest.approx(100.0, abs=0.05)


# ── allocation ────────────────────────────────────────────────────────────────

def test_allocation_normalizes_each_bucket_to_100():
    positions = [
        {"ticker": "AAPL", "market_value": 6000.0, "sector": "Technology",
         "indexes": ["S&P 500", "NASDAQ-100"]},
        {"ticker": "CF", "market_value": 4000.0, "sector": "Basic Materials",
         "indexes": ["S&P 500"]},
    ]
    alloc = build_allocation(positions)
    assert sum(s["weight_pct"] for s in alloc["by_sector"]) == pytest.approx(100.0, abs=0.05)
    # AAPL counts in two indexes, so raw index values exceed the portfolio
    # total — weights are still normalized per bucket.
    assert sum(s["weight_pct"] for s in alloc["by_index"]) == pytest.approx(100.0, abs=0.05)
    assert alloc["total_value"] == 10000.0


def test_allocation_empty_portfolio():
    alloc = build_allocation([])
    assert alloc["by_sector"] == []
    assert alloc["by_index"] == []


# ── Asset-type split: index funds vs individual stocks ────────────────────────

def test_asset_split_separates_etfs_from_stocks():
    positions = [
        {"ticker": "VOO", "market_value": 6000.0, "sector": "Diversified",
         "asset_type": "etf", "indexes": ["S&P 500"]},
        {"ticker": "AVGO", "market_value": 4000.0, "sector": "Technology",
         "asset_type": "stock", "indexes": ["S&P 500"]},
    ]
    split = build_allocation(positions)["by_asset_type"]
    assert split["etf_pct"] == 60.0
    assert split["stock_pct"] == 40.0
    assert split["etf_value"] == 6000.0
    assert split["stock_value"] == 4000.0


def test_asset_split_always_totals_100():
    positions = [
        {"ticker": "A", "market_value": 1234.56, "sector": "X", "asset_type": "etf", "indexes": []},
        {"ticker": "B", "market_value": 765.44, "sector": "Y", "asset_type": "stock", "indexes": []},
    ]
    split = build_allocation(positions)["by_asset_type"]
    assert split["etf_pct"] + split["stock_pct"] == pytest.approx(100.0, abs=0.05)


def test_positions_without_asset_type_count_as_stocks():
    # An older holding saved before the field existed must not vanish from the split.
    positions = [{"ticker": "A", "market_value": 500.0, "sector": "X", "indexes": []}]
    split = build_allocation(positions)["by_asset_type"]
    assert split["stock_pct"] == 100.0
    assert split["etf_pct"] == 0.0


def test_asset_split_on_empty_portfolio_is_zero_not_a_crash():
    split = build_allocation([])["by_asset_type"]
    assert split["etf_pct"] == 0.0 and split["stock_pct"] == 0.0
