"""Telegram portfolio command: status message and the AI-analysis button."""

import asyncio

import pytest

from stock_monitor.telegram_bot import TelegramCommandBot

REPORT = {
    "total_value": 10000.0,
    "total_pnl_value": -250.0,
    "positions": [
        {"ticker": "AVGO", "quantity": 6.76, "entry_price": 374.67,
         "current_price": 352.10, "market_value": 2380.2, "pnl_pct": -6.02,
         "pnl_value": -152.5, "atr_pct": 5.8, "sector": "Technology"},
        {"ticker": "CF", "quantity": 80, "entry_price": 75.0,
         "current_price": 95.0, "market_value": 7600.0, "pnl_pct": 26.67,
         "pnl_value": 1600.0, "atr_pct": None, "sector": "Basic Materials"},
    ],
    "volatility": {"exposure_pct": 23.8, "threshold_pct": 30.0, "alert": False,
                   "high_volatility_positions": []},
    "sector": {"sectors": [], "concentrated_sectors": [
        {"sector": "Basic Materials", "weight_pct": 76.0}], "threshold_pct": 40.0,
        "alert": True},
    "allocation": {"by_index": []},
}


class Recorder:
    """Captures what the bot would send instead of calling Telegram."""

    def __init__(self):
        self.messages = []

    def send(self, chat_id, text, reply_markup=None):
        self.messages.append({"text": text, "reply_markup": reply_markup})

    @property
    def last(self):
        return self.messages[-1]


def make_bot(recorder, analyst=None, report=REPORT):
    bot = TelegramCommandBot.__new__(TelegramCommandBot)
    bot._analyst = analyst
    bot._authorized_chat_id = ""
    bot._send = recorder.send
    bot._risk = type("R", (), {"full_report": staticmethod(lambda: report)})()
    return bot


def run(coro):
    # asyncio.run rather than get_event_loop(): the latter raises once any
    # earlier test has closed the loop, which made these tests pass alone and
    # fail in a full run.
    return asyncio.run(coro)


# ── Status message ────────────────────────────────────────────────────────────

def test_portfolio_lists_every_position():
    rec = Recorder()
    run(make_bot(rec)._send_portfolio(1))
    text = rec.last["text"]
    assert "AVGO" in text and "CF" in text


def test_portfolio_shows_totals_and_returns():
    rec = Recorder()
    run(make_bot(rec)._send_portfolio(1))
    text = rec.last["text"]
    assert "$10,000.00" in text
    assert "-$250.00" in text          # loss rendered as -$, not $-
    assert "-6.02%" in text and "+26.67%" in text


def test_portfolio_orders_by_position_size():
    rec = Recorder()
    run(make_bot(rec)._send_portfolio(1))
    text = rec.last["text"]
    assert text.index("CF") < text.index("AVGO")   # largest holding first


def test_portfolio_surfaces_risk_alerts():
    rec = Recorder()
    run(make_bot(rec)._send_portfolio(1))
    assert "ריכוזיות" in rec.last["text"]


def test_portfolio_handles_missing_atr():
    rec = Recorder()
    run(make_bot(rec)._send_portfolio(1))
    assert "None" not in rec.last["text"]


def test_empty_portfolio_points_at_the_dashboard():
    rec = Recorder()
    run(make_bot(rec, report={"positions": []})._send_portfolio(1))
    assert "התיק ריק" in rec.last["text"]


# ── AI analysis button ────────────────────────────────────────────────────────

def test_analysis_button_shown_when_ai_available():
    rec = Recorder()
    run(make_bot(rec, analyst=object())._send_portfolio(1))
    markup = rec.last["reply_markup"]
    assert markup["inline_keyboard"][0][0]["callback_data"] == "analyze_portfolio"


def test_no_button_without_ai():
    rec = Recorder()
    run(make_bot(rec)._send_portfolio(1))
    assert rec.last["reply_markup"] is None


def test_analysis_callback_returns_the_analysis():
    class Analyst:
        def analyze_portfolio(self, report):
            return f"ניתוח על {len(report['positions'])} פוזיציות"

    rec = Recorder()
    run(make_bot(rec, analyst=Analyst())._send_portfolio_analysis(1))
    assert "ניתוח על 2 פוזיציות" in rec.last["text"]


def test_analysis_callback_without_ai_explains_how_to_enable():
    rec = Recorder()
    run(make_bot(rec)._send_portfolio_analysis(1))
    assert "ANTHROPIC_API_KEY" in rec.last["text"]
