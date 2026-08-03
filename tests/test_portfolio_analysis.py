"""Whole-portfolio AI analysis: prompt construction and the API contract."""

import pytest
from fastapi.testclient import TestClient

from stock_monitor import dashboard
from stock_monitor.ai_analyst import StockAnalyst
from stock_monitor.config import NotificationConfig
from stock_monitor.notifier import NotificationDispatcher
from stock_monitor.webhook_server import create_webhook_app

REPORT = {
    "total_value": 12450.0,
    "total_pnl_value": -320.5,
    "positions": [
        {
            "ticker": "AVGO", "quantity": 6.76, "entry_price": 374.67,
            "current_price": 352.10, "market_value": 2380.2, "pnl_pct": -6.02,
            "pnl_value": -152.5, "atr_pct": 5.8, "sector": "Technology",
        },
        {
            "ticker": "CF", "quantity": 80, "entry_price": 75.0,
            "current_price": 74.9, "market_value": 5992.0, "pnl_pct": -0.13,
            "pnl_value": -8.0, "atr_pct": None, "sector": "Basic Materials",
        },
    ],
    "volatility": {
        "exposure_pct": 51.9, "threshold_pct": 30.0, "alert": True,
        "high_volatility_positions": [{"ticker": "AVGO", "atr_pct": 5.8}],
    },
    "sector": {
        "sectors": [{"sector": "Technology", "weight_pct": 51.9}],
        "concentrated_sectors": [{"sector": "Technology", "weight_pct": 51.9}],
        "threshold_pct": 40.0, "alert": True,
    },
    "allocation": {"by_index": [{"label": "S&P 500", "weight_pct": 66.0}]},
}


# ── Prompt construction ───────────────────────────────────────────────────────

def test_prompt_includes_every_position():
    prompt = StockAnalyst._build_portfolio_prompt(REPORT)
    assert "AVGO" in prompt and "CF" in prompt


def test_prompt_reports_totals_and_returns():
    prompt = StockAnalyst._build_portfolio_prompt(REPORT)
    assert "$12,450.00" in prompt
    assert "-$320.50" in prompt      # loss rendered as -$, not $-
    assert "-6.02%" in prompt


def test_prompt_surfaces_risk_alerts():
    prompt = StockAnalyst._build_portfolio_prompt(REPORT)
    assert "ריכוזיות" in prompt
    assert "51.9%" in prompt


def test_prompt_handles_missing_atr():
    # CF has atr_pct None — must not crash or print "None".
    prompt = StockAnalyst._build_portfolio_prompt(REPORT)
    assert "אין נתון" in prompt
    assert "None" not in prompt


def test_prompt_survives_a_minimal_report():
    minimal = {"total_value": 0, "total_pnl_value": 0, "positions": [
        {"ticker": "X", "quantity": 1, "entry_price": 1.0, "current_price": 1.0,
         "market_value": 1.0, "pnl_pct": 0.0, "pnl_value": 0.0, "atr_pct": None,
         "sector": "Unknown"}]}
    assert "X" in StockAnalyst._build_portfolio_prompt(minimal)


def test_empty_portfolio_short_circuits_without_calling_the_api():
    analyst = StockAnalyst.__new__(StockAnalyst)   # no client — must not be used
    assert "אין פוזיציות" in analyst.analyze_portfolio({"positions": []})


# ── API contract ──────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    return TestClient(create_webhook_app(NotificationDispatcher(NotificationConfig()), ""))


def test_analyze_returns_503_when_ai_not_configured(client):
    dashboard.set_analyst(None)
    response = client.post("/api/portfolio/analyze")
    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


def test_analyze_returns_the_analysis(client):
    class Stub:
        def analyze_portfolio(self, report):
            return "ניתוח לדוגמה"

    dashboard.set_analyst(Stub())
    try:
        response = client.post("/api/portfolio/analyze")
        assert response.status_code == 200
        assert response.json()["analysis"] == "ניתוח לדוגמה"
    finally:
        dashboard.set_analyst(None)


def test_analyze_is_post_only(client):
    # A GET must not spend Anthropic tokens via prefetch or refresh.
    assert client.get("/api/portfolio/analyze").status_code == 405
