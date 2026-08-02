import numpy as np
import pandas as pd
import pytest

from alert_engine import (
    AlertThresholds,
    build_sector_report,
    build_volatility_report,
    compute_atr,
)

THRESHOLDS = AlertThresholds(
    high_atr_pct=4.0, volatility_exposure_pct=30.0, sector_concentration_pct=40.0
)


def make_history(days: int = 60, price: float = 100.0, daily_range: float = 2.0) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=days, freq="B")
    close = np.full(days, price)
    return pd.DataFrame(
        {
            "High": close + daily_range / 2,
            "Low": close - daily_range / 2,
            "Close": close,
        },
        index=idx,
    )


# ---------------------------------------------------------------- compute_atr

def test_atr_matches_constant_range():
    # With a constant daily range and flat closes, ATR converges to the range.
    df = make_history(days=200, price=100.0, daily_range=3.0)
    atr = compute_atr(df, period=14)
    assert atr == pytest.approx(3.0, rel=1e-3)


def test_atr_none_when_insufficient_data():
    df = make_history(days=10)
    assert compute_atr(df, period=14) is None


# ------------------------------------------------------- volatility exposure

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
    positions = [{"ticker": "NEW", "market_value": 1000.0, "atr_pct": None}]
    report = build_volatility_report(positions, THRESHOLDS)
    assert report["high_volatility_positions"] == []
    assert report["alert"] is False


def test_volatility_empty_portfolio():
    report = build_volatility_report([], THRESHOLDS)
    assert report["exposure_pct"] == 0.0
    assert report["alert"] is False


# ------------------------------------------------------ sector concentration

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
