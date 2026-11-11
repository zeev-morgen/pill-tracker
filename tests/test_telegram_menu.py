"""The Telegram bot as a thing you tap rather than a thing you remember.

Three concerns here: that a tapped keyboard label routes to the same place as
the typed command, that the new screens (news, summary, alerts) say the right
thing in each state, and that a quote is shown in the currency it is quoted in.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from stock_monitor import messages, mute, news
from stock_monitor import telegram_bot as tb
from stock_monitor.store import AlertRecord
from stock_monitor.telegram_bot import MENU_KEYBOARD, TelegramCommandBot, route

REPORT = {
    "total_value": 10_000.0,
    "total_pnl_value": -250.0,
    "positions": [
        {"ticker": "AVGO", "quantity": 6.76, "entry_price": 374.67,
         "current_price": 352.10, "market_value": 2380.2, "pnl_pct": -6.02,
         "pnl_value": -152.5, "atr_pct": 5.8, "sector": "Technology",
         "currency": "USD"},
        {"ticker": "ESLT.TA", "quantity": 10, "entry_price": 15000.0,
         "current_price": 16240.0, "market_value": 4511.1, "pnl_pct": 8.27,
         "pnl_value": 344.4, "atr_pct": None, "sector": "Industrials",
         "currency": "ILA"},
    ],
    "volatility": {"exposure_pct": 23.8, "threshold_pct": 30.0, "alert": False,
                   "high_volatility_positions": []},
    "sector": {"sectors": [], "concentrated_sectors": [], "threshold_pct": 40.0,
               "alert": False},
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

    def text(self, index=-1):
        return strip_bidi(self.messages[index]["text"])


def strip_bidi(text):
    for mark in (messages.RLM, "⁦", "⁩"):
        text = text.replace(mark, "")
    return text


class _Feed:
    def __init__(self, quotes=None):
        self._quotes = quotes or {}

    def get_current_data(self, symbol):
        return self._quotes.get(symbol)

    def get_current_data_batch(self, symbols):
        return {s: self._quotes[s] for s in symbols if s in self._quotes}


class _Monitor:
    """Stands in for NewsMonitor."""

    def __init__(self, snapshot):
        self._snapshot = snapshot

    def snapshot(self, max_age_minutes=60):
        return self._snapshot


def make_bot(recorder, analyst=None, report=REPORT, quotes=None, news_monitor=None,
             alerts=()):
    bot = TelegramCommandBot.__new__(TelegramCommandBot)
    bot._analyst = analyst
    bot._authorized_chat_id = ""
    bot._send = recorder.send
    bot._answer_callback = lambda *args, **kwargs: None
    bot._risk = type("R", (), {"full_report": staticmethod(lambda: report)})()
    bot._feed = _Feed(quotes)
    bot._rc_ref = {}
    bot._news = news_monitor
    bot._config_syms = list((quotes or {}))
    return bot


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def unmuted():
    mute.unmute_everything()
    yield
    mute.unmute_everything()


# ── Routing: tapped and typed reach the same place ────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("💼 התיק שלי", "portfolio"),
    ("תיק", "portfolio"),
    ("/portfolio", "portfolio"),
    ("📋 מעקב", "status"),
    ("/status", "status"),
    ("📰 חדשות", "news"),
    ("/news", "news"),
    ("🧾 סיכום", "summary"),
    ("/summary", "summary"),
    ("🔔 התראות", "alerts"),
    ("/alerts", "alerts"),
    ("❓ עזרה", "help"),
    ("/help", "help"),
    ("/start", "start"),
    ("שלום", "start"),
    ("תפריט", "menu"),
])
def test_the_keyboard_labels_route_like_the_commands(text, expected):
    assert route(text) == expected


def test_a_ticker_is_not_mistaken_for_a_command():
    assert route("AVGO") is None
    assert route("ESLT.TA") is None


def test_routing_ignores_case_and_extra_spacing():
    assert route("  /PORTFOLIO  ") == "portfolio"
    assert route("💼   התיק   שלי") == "portfolio"


# ── The keyboard itself ───────────────────────────────────────────────────────

def test_start_opens_the_keyboard():
    recorder = Recorder()
    run(make_bot(recorder)._handle({"chat": {"id": 1}, "text": "/start"}))

    assert recorder.last["reply_markup"] == MENU_KEYBOARD


def test_the_keyboard_covers_the_six_things_you_actually_do():
    labels = [b["text"] for row in MENU_KEYBOARD["keyboard"] for b in row]
    assert len(labels) == 6
    assert any("תיק" in l for l in labels)
    assert any("חדשות" in l for l in labels)
    assert any("התראות" in l for l in labels)


def test_the_keyboard_stays_open_between_messages():
    """Without is_persistent a tap closes it and the next action is typing again."""
    assert MENU_KEYBOARD["is_persistent"] is True
    assert MENU_KEYBOARD["resize_keyboard"] is True


def test_an_unknown_message_offers_the_keyboard_rather_than_a_dead_end():
    recorder = Recorder()
    run(make_bot(recorder)._handle({"chat": {"id": 1}, "text": "מה קורה בשוק"}))

    assert recorder.last["reply_markup"] == MENU_KEYBOARD
    assert "לא הבנתי" in recorder.text()


def test_markdown_breaking_characters_are_stripped_from_the_echo():
    """The echoed text is the user's; a stray asterisk would reject the reply."""
    recorder = Recorder()
    run(make_bot(recorder)._handle({"chat": {"id": 1}, "text": "what *is* this_"}))

    assert "*is*" not in recorder.text()


# ── Tickers, including Tel Aviv ───────────────────────────────────────────────

AVGO_QUOTE = {"price": 352.10, "prev_close": 360.0, "volume": 2_500_000,
              "session": "regular", "change_pct": -2.19, "day_high": 361.0,
              "day_low": 350.0, "currency": "USD"}
ESLT_QUOTE = {"price": 16240.0, "prev_close": 16000.0, "volume": 120_000,
              "session": "regular", "change_pct": 1.5, "day_high": 16300.0,
              "day_low": 15950.0, "currency": "ILA"}


def test_a_us_ticker_is_priced_in_dollars():
    recorder = Recorder()
    bot = make_bot(recorder, quotes={"AVGO": AVGO_QUOTE})
    run(bot._handle({"chat": {"id": 1}, "text": "AVGO"}))

    assert "$352.10" in recorder.text()


def test_a_tel_aviv_ticker_is_accepted_and_priced_in_agorot():
    """ESLT.TA used to be rejected as "not a ticker", then shown as $16,240."""
    recorder = Recorder()
    bot = make_bot(recorder, quotes={"ESLT.TA": ESLT_QUOTE})
    run(bot._handle({"chat": {"id": 1}, "text": "ESLT.TA"}))

    text = recorder.text()
    assert "16,240.00 אג׳" in text
    assert "$" not in text


def test_an_unknown_ticker_says_what_to_check():
    recorder = Recorder()
    run(make_bot(recorder)._handle({"chat": {"id": 1}, "text": "NOPE"}))

    assert ".TA" in recorder.text(), "the suffix hint is the common mistake"


def test_a_quote_offers_news_even_without_ai():
    recorder = Recorder()
    bot = make_bot(recorder, quotes={"AVGO": AVGO_QUOTE})
    run(bot._handle({"chat": {"id": 1}, "text": "AVGO"}))

    data = [b["callback_data"] for row in recorder.last["reply_markup"]["inline_keyboard"]
            for b in row]
    assert data == ["news:AVGO"]


def test_an_analysis_request_accepts_a_suffixed_ticker():
    recorder = Recorder()
    analyst = type("A", (), {"analyze": staticmethod(lambda *a, **k: "ניתוח")})()
    bot = make_bot(recorder, analyst=analyst, quotes={"ESLT.TA": ESLT_QUOTE})
    run(bot._handle({"chat": {"id": 1}, "text": "ניתוח ESLT.TA"}))

    assert "ESLT.TA" in recorder.text()


# ── News ──────────────────────────────────────────────────────────────────────

def _item(title, ticker="AVGO", age=2.0):
    return {"ticker": ticker, "title": title, "publisher": "Reuters",
            "link": "https://example.com/a", "age_hours": age,
            "published_at": datetime.now(timezone.utc).isoformat()}


def test_news_lists_the_headlines_with_their_age():
    recorder = Recorder()
    monitor = _Monitor({"items": [_item("Broadcom lifts guidance")],
                        "scanned_at": None, "sources": {"AVGO": "ticker"},
                        "unavailable": [], "fresh_window_hours": 24})
    run(make_bot(recorder, news_monitor=monitor)._send_news(1))

    text = recorder.text()
    assert "Broadcom lifts guidance" in text
    assert "לפני 2 ש׳" in text
    assert "Reuters" in text


def test_news_distinguishes_a_quiet_day_from_a_broken_feed():
    """The bug that started this: an outage read as "no news" for a month."""
    recorder = Recorder()
    quiet = _Monitor({"items": [], "scanned_at": None, "sources": {"AVGO": "rss"},
                      "unavailable": [], "fresh_window_hours": 24})
    run(make_bot(recorder, news_monitor=quiet)._send_news(1))
    assert "אין כותרות חדשות" in recorder.text()

    recorder = Recorder()
    broken = _Monitor({"items": [], "scanned_at": None, "sources": {},
                       "unavailable": ["AVGO"], "fresh_window_hours": 24})
    run(make_bot(recorder, news_monitor=broken)._send_news(1))
    assert "מקור החדשות אינו זמין" in recorder.text()
    assert "לא יום שקט" in recorder.text()


def test_one_broken_ticker_among_several_is_not_called_an_outage():
    recorder = Recorder()
    monitor = _Monitor({"items": [_item("still working", "CF")], "scanned_at": None,
                        "sources": {"CF": "ticker"}, "unavailable": ["AVGO"],
                        "fresh_window_hours": 24})
    run(make_bot(recorder, news_monitor=monitor)._send_news(1))

    assert "אינו זמין" not in recorder.text()


def test_news_for_a_single_symbol_fetches_that_symbol(monkeypatch):
    recorder = Recorder()
    asked = {}

    def fake_fetch(ticker, limit=6, **kwargs):
        asked["ticker"] = ticker
        return news.NewsResult(items=[_item("one stock", ticker)], source="ticker")

    monkeypatch.setattr(news, "fetch", fake_fetch)
    run(make_bot(recorder)._send_news(1, "NVDA"))

    assert asked["ticker"] == "NVDA"
    assert "one stock" in recorder.text()


def test_headlines_cannot_break_the_message_formatting():
    recorder = Recorder()
    monitor = _Monitor({"items": [_item("Earnings *beat* [big]_")], "scanned_at": None,
                        "sources": {"AVGO": "ticker"}, "unavailable": [],
                        "fresh_window_hours": 24})
    run(make_bot(recorder, news_monitor=monitor)._send_news(1))

    text = recorder.text()
    assert "*beat*" not in text and "[big]" not in text
    assert "Earnings beat big" in text


# ── Summary ───────────────────────────────────────────────────────────────────

def test_the_summary_carries_the_portfolio():
    recorder = Recorder()
    run(make_bot(recorder)._send_summary(1))

    text = recorder.text()
    assert "סיכום" in text
    assert "התיק שלי" in text
    assert "ESLT.TA" in text and "AVGO" in text


def test_a_short_portfolio_is_not_listed_twice():
    """With two holdings the "biggest moves" section is the same two rows."""
    recorder = Recorder()
    run(make_bot(recorder)._send_summary(1))

    assert "התנועות הגדולות" not in recorder.text()


def test_a_longer_portfolio_gets_a_movers_section():
    recorder = Recorder()
    extra = [
        {**REPORT["positions"][0], "ticker": name, "pnl_pct": pnl, "currency": "USD"}
        for name, pnl in [("CF", 26.7), ("MU", -11.2), ("JPM", 1.1)]
    ]
    report = {**REPORT, "positions": REPORT["positions"] + extra}
    run(make_bot(recorder, report=report)._send_summary(1))

    text = recorder.text()
    assert "התנועות הגדולות בתיק" in text
    # The three largest absolute moves, not the first three rows.
    movers = text.split("התנועות הגדולות בתיק")[1]
    assert "CF" in movers and "MU" in movers and "JPM" not in movers


def test_the_summary_prices_each_position_in_its_own_currency():
    recorder = Recorder()
    run(make_bot(recorder)._send_summary(1))

    text = recorder.text()
    assert "16,240.00 אג׳" in text, "the Israeli position's price"
    assert "$352.10" in text, "and the American one's"


def test_the_summary_reports_an_empty_portfolio_without_failing():
    recorder = Recorder()
    empty = {**REPORT, "positions": [], "total_value": 0.0, "total_pnl_value": 0.0}
    run(make_bot(recorder, report=empty)._send_summary(1))

    assert "התיק ריק" in recorder.text()


def test_the_summary_mentions_active_mutes():
    recorder = Recorder()
    mute.mute("AVGO", 60)
    run(make_bot(recorder)._send_summary(1))

    assert "השתקות פעילות" in recorder.text()


def test_the_summary_includes_fresh_headlines_when_there_are_any():
    recorder = Recorder()
    monitor = _Monitor({"items": [_item("Broadcom lifts guidance")], "scanned_at": None,
                        "sources": {"AVGO": "ticker"}, "unavailable": [],
                        "fresh_window_hours": 24})
    run(make_bot(recorder, news_monitor=monitor)._send_summary(1))

    # One headline takes the singular — "1 כותרות" is not Hebrew.
    assert "כותרת חדשה אחת" in recorder.text()
    assert "Broadcom lifts guidance" in recorder.text()


def test_the_daily_digest_needs_a_chat_to_send_to():
    recorder = Recorder()
    bot = make_bot(recorder)
    run(bot.daily_digest())
    assert recorder.messages == [], "no configured chat means nothing to push"

    bot._authorized_chat_id = "123"
    run(bot.daily_digest())
    assert recorder.messages, "with a chat configured the digest goes out unprompted"


# ── Startup wiring ────────────────────────────────────────────────────────────

def _app(**telegram):
    from stock_monitor.config import (AppConfig, NotificationConfig, StockConfig,
                                      TelegramConfig)
    from stock_monitor.main import StockMonitorApp

    config = AppConfig(
        stocks=[StockConfig(symbol="AVGO", alerts=[])],
        notifications=NotificationConfig(telegram=TelegramConfig(**telegram)),
    )
    return StockMonitorApp(config)


def test_no_token_means_no_bot():
    assert _app(enabled=True, bot_token="").build_telegram_bot() is None
    assert _app(enabled=False, bot_token="x").build_telegram_bot() is None


def test_the_digest_is_scheduled_at_the_configured_hour():
    app = _app(enabled=True, bot_token="t", chat_id="123", digest_time="16:45")
    bot = app.build_telegram_bot()

    jobs = app.scheduler._daily_jobs
    assert bot is not None
    assert any(fn.__self__ is bot and (hour, minute) == (16, 45)
               for fn, hour, minute in jobs)


def test_the_bot_shares_the_dashboards_news_cache():
    """Two caches would mean the bot re-fetching what the tab already has."""
    app = _app(enabled=True, bot_token="t", chat_id="123")
    assert app.build_telegram_bot()._news is app._news


def test_an_empty_digest_time_switches_the_digest_off():
    app = _app(enabled=True, bot_token="t", chat_id="123", digest_time="")
    app.build_telegram_bot()

    assert not any("digest" in fn.__name__ for fn, _, _ in app.scheduler._daily_jobs)


def test_a_malformed_digest_time_is_ignored_rather_than_fatal():
    app = _app(enabled=True, bot_token="t", chat_id="1", digest_time="half past four")
    assert app.build_telegram_bot() is not None, "the bot still runs"
    assert not any("digest" in fn.__name__ for fn, _, _ in app.scheduler._daily_jobs)


def test_without_a_chat_id_there_is_nothing_to_push_to():
    app = _app(enabled=True, bot_token="t", chat_id="", digest_time="16:45")
    app.build_telegram_bot()

    assert not any("digest" in fn.__name__ for fn, _, _ in app.scheduler._daily_jobs)


# ── Alerts screen and muting ──────────────────────────────────────────────────

def test_the_alerts_screen_says_everything_is_active_when_it_is(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(tb.alert_store, "recent", lambda n=5: [])
    run(make_bot(recorder)._send_alerts(1))

    assert "כל ההתראות פעילות" in recorder.text()
    assert "עדיין לא נשלחו התראות" in recorder.text()


def test_the_alerts_screen_lists_what_is_muted_and_until_when(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(tb.alert_store, "recent", lambda n=5: [])
    mute.mute("AVGO", 60)
    run(make_bot(recorder)._send_alerts(1))

    assert "מושתק" in recorder.text() and "AVGO" in recorder.text()


def test_the_alerts_screen_shows_what_fired_recently(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(tb.alert_store, "recent", lambda n=5: [
        AlertRecord(timestamp="14:32:00", symbol="AVGO", alert_type="price_change_pct",
                    message="$AVGO moved DOWN 3.21%", severity="WARNING"),
    ])
    run(make_bot(recorder)._send_alerts(1))

    assert "נשלחו לאחרונה" in recorder.text()
    assert "AVGO moved DOWN 3.21%" in recorder.text()


def test_the_alerts_screen_offers_muting(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(tb.alert_store, "recent", lambda n=5: [])
    run(make_bot(recorder)._send_alerts(1))

    data = [b["callback_data"] for row in recorder.last["reply_markup"]["inline_keyboard"]
            for b in row]
    assert f"mute:{mute.ALL}:60" in data
    assert "mute_today" in data


def test_unmuting_is_offered_only_when_something_is_muted(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(tb.alert_store, "recent", lambda n=5: [])
    run(make_bot(recorder)._send_alerts(1))
    flat = [b["callback_data"] for row in recorder.last["reply_markup"]["inline_keyboard"]
            for b in row]
    assert "unmute_all" not in flat

    mute.mute(mute.ALL, 60)
    run(make_bot(recorder)._send_alerts(1))
    flat = [b["callback_data"] for row in recorder.last["reply_markup"]["inline_keyboard"]
            for b in row]
    assert "unmute_all" in flat


def test_the_mute_button_on_an_alert_mutes_that_symbol():
    recorder = Recorder()
    bot = make_bot(recorder)
    run(bot._handle_callback({"id": "1", "message": {"chat": {"id": 1}},
                              "data": "mute:AVGO:60"}))

    assert mute.is_muted("AVGO") is True
    assert "AVGO" in recorder.text() and "השתקה" in recorder.text()


def test_the_mute_confirmation_names_the_hour_it_lifts():
    recorder = Recorder()
    bot = make_bot(recorder)
    run(bot._handle_callback({"id": "1", "message": {"chat": {"id": 1}},
                              "data": "mute:AVGO:60"}))

    assert "ET" in recorder.text(), "a deadline you can read, not 'muted'"


def test_mute_until_tomorrow_covers_the_rest_of_the_day():
    recorder = Recorder()
    bot = make_bot(recorder)
    run(bot._handle_callback({"id": "1", "message": {"chat": {"id": 1}},
                              "data": "mute_today"}))

    deadline = mute.muted_until(mute.ALL)
    assert deadline is not None
    assert deadline.astimezone(tb.NYSE_TZ).hour == 9, "lifts before the next open"
    assert "עד מחר" in recorder.text()


def test_unmuting_everything_from_the_button_clears_the_mutes(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(tb.alert_store, "recent", lambda n=5: [])
    mute.mute(mute.ALL, 60)
    bot = make_bot(recorder)
    run(bot._handle_callback({"id": "1", "message": {"chat": {"id": 1}},
                              "data": "unmute_all"}))

    assert mute.active() == {}
    assert "ההתראות חזרו לפעול" in recorder.text()


def test_a_stranger_cannot_mute_your_alerts():
    recorder = Recorder()
    bot = make_bot(recorder)
    bot._authorized_chat_id = "999"
    run(bot._handle_callback({"id": "1", "message": {"chat": {"id": 1}},
                              "data": "mute:AVGO:60"}))

    assert mute.is_muted("AVGO") is False
    assert recorder.messages == []
