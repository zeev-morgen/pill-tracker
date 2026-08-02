from datetime import date

from ai_analyst import AIAnalyst, PositionContext
from market_data import MarketDataService
from store import Holding


def make_analyst() -> AIAnalyst:
    # client=object() — prompt-building tests never touch the API
    return AIAnalyst(MarketDataService(), client=object())


def test_build_prompt_includes_personal_position():
    analyst = make_analyst()
    position = PositionContext(
        quantity=10,
        purchase_date="2024-01-15",
        entry_price=100.0,
        current_price=120.0,
    )
    prompt = analyst._build_prompt("AAPL", "tech summary", "fundamentals", position)
    assert "Quantity held: 10" in prompt
    assert "Purchase date: 2024-01-15" in prompt
    assert "+20.00%" in prompt          # actual P/L injected
    assert "THIS entry point" in prompt  # personalized instruction present


def test_build_prompt_without_position_falls_back_to_general():
    analyst = make_analyst()
    prompt = analyst._build_prompt("AAPL", "tech summary", "fundamentals", None)
    assert "no position" in prompt
    assert "Quantity held" not in prompt


def test_position_context_pnl_math():
    ctx = PositionContext(
        quantity=5, purchase_date="2024-01-01", entry_price=50.0, current_price=40.0
    )
    assert ctx.pnl_pct == -20.0
    assert ctx.pnl_value == -50.0


def test_position_context_handles_missing_entry_price():
    ctx = PositionContext(
        quantity=5, purchase_date="2024-01-01", entry_price=None, current_price=40.0
    )
    assert ctx.pnl_pct is None
    assert ctx.pnl_value is None
