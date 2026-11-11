"""Every alert, in Hebrew.

Until now one alert type — the earnings reminder — was Hebrew and the rest were
English, because the engine wrote its own sentences as it went. The engine now
records the facts and :mod:`stock_monitor.messages` does the wording, so these
tests cover both halves: that the engine supplies what a renderer needs, and
that the renderer produces Hebrew a person can read on a phone.
"""

from datetime import datetime

import pytest

from stock_monitor import messages, mute
from stock_monitor.alert_engine import AlertEngine, AlertEvent, PricePoint
from stock_monitor.config import AlertConfig, AppConfig, NotificationConfig, StockConfig
from stock_monitor.notifier import NotificationDispatcher, _alert_buttons


def event(alert_type="price_change_pct", symbol="AVGO", severity="WARNING",
          details=None, message="english one-liner", price=100.0):
    return AlertEvent(
        symbol=symbol, alert_type=alert_type, message=message, price=price,
        timestamp=datetime(2026, 11, 11, 14, 32), severity=severity,
        details=details if details is not None else {
            "change_pct": -3.21, "window_minutes": 15,
            "from_price": 412.5, "to_price": 399.26, "currency": "USD",
        },
    )


def strip_bidi(text):
    """The rendered text without the invisible direction marks."""
    for mark in (messages.RLM, "⁦", "⁩"):
        text = text.replace(mark, "")
    return text


@pytest.fixture(autouse=True)
def unmuted():
    mute.unmute_everything()
    yield
    mute.unmute_everything()


# ── Hebrew, for every type ────────────────────────────────────────────────────

def test_a_price_move_is_described_in_hebrew():
    text = strip_bidi(messages.alert(event()))

    assert "צניחה של 3.21%" in text
    assert "מ-$412.50 ל-$399.26" in text
    assert "AVGO" in text, "the ticker itself stays Latin"


def test_a_gentle_move_is_not_called_a_crash():
    """INFO and WARNING are different events and deserve different words."""
    calm = strip_bidi(messages.alert(event(severity="INFO")))
    assert "ירידה של 3.21%" in calm and "צניחה" not in calm


def test_a_volume_spike_is_described_in_hebrew():
    text = strip_bidi(messages.alert(event(
        alert_type="volume_spike",
        details={"volume": 52_000_000, "ratio": 2.4, "avg_volume": 10_000_000,
                 "session_elapsed_pct": 43.0, "time_adjusted": True},
    )))

    assert "זינוק במחזור המסחר" in text
    assert "52,000,000 מניות" in text
    assert "פי 2.4 מהקצב הצפוי לשעה זו" in text
    assert "43% מיום המסחר חלפו" in text


def test_a_full_day_benchmark_says_so_instead_of_naming_a_pace():
    text = strip_bidi(messages.alert(event(
        alert_type="volume_spike",
        details={"volume": 52_000_000, "ratio": 2.4, "avg_volume": 10_000_000,
                 "session_elapsed_pct": None, "time_adjusted": False},
    )))

    assert "מהמחזור היומי הרגיל" in text
    assert "מיום המסחר חלפו" not in text


def test_a_threshold_crossing_names_the_direction():
    up = strip_bidi(messages.alert(event(
        alert_type="price_threshold", severity="INFO",
        details={"direction": "above", "level": 400.0, "price": 401.2,
                 "currency": "USD"},
    )))
    down = strip_bidi(messages.alert(event(
        alert_type="price_threshold",
        details={"direction": "below", "level": 400.0, "price": 398.0,
                 "currency": "USD"},
    )))

    assert "כלפי מעלה" in up and "רף: $400.00" in up
    assert "כלפי מטה" in down


def test_an_unknown_alert_type_still_sends_something():
    """A renderer nobody wrote yet must not cost the alert."""
    text = strip_bidi(messages.alert(event(
        alert_type="something_new", details={}, message="$NVDA did something odd",
    )))

    assert "NVDA" in text and "did something odd" in text


def test_the_session_and_time_are_named_in_hebrew():
    for session, label in [("pre", "טרום מסחר"), ("regular", "מסחר רגיל"),
                           ("after", "אחרי שעות המסחר"), ("closed", "שוק סגור")]:
        text = strip_bidi(messages.alert(event(), session))
        assert label in text and "14:32 ET" in text


def test_the_earnings_alert_keeps_its_hebrew_and_declines_correctly():
    assert "בעוד 3 ימים" in strip_bidi(messages.earnings_alert("AVGO", 3, "14/11/2026")[1])
    assert "בעוד יומיים" in strip_bidi(messages.earnings_alert("AVGO", 2, "13/11/2026")[1])
    # "בעוד מחר" is not Hebrew; the adverb takes no preposition.
    tomorrow = strip_bidi(messages.earnings_alert("AVGO", 1, "12/11/2026")[1])
    assert "דיווח תוצאות מחר" in tomorrow and "בעוד" not in tomorrow
    assert "דיווח תוצאות היום" in strip_bidi(messages.earnings_alert("AVGO", 0, "11/11/2026")[1])


def test_an_external_webhook_keeps_its_own_body_under_a_hebrew_frame():
    """Whoever wrote the TradingView alert wrote it in their words, not ours."""
    text = strip_bidi(messages.external_alert("TradingView", "AAPL broke 200-day MA"))

    assert "TradingView" in text and "AAPL broke 200-day MA" in text


# ── Tel Aviv quotes ───────────────────────────────────────────────────────────

def test_an_israeli_alert_is_quoted_in_agorot():
    """Printing 15,600 agorot as $15,600 overstates the position a hundredfold."""
    text = strip_bidi(messages.alert(event(
        symbol="ESLT.TA",
        details={"change_pct": 4.1, "window_minutes": 30, "from_price": 15600.0,
                 "to_price": 16240.0, "currency": "ILA"},
    )))

    assert "15,600.00 אג׳" in text and "16,240.00 אג׳" in text
    assert "$" not in text


def test_the_ta_suffix_alone_is_enough_to_pick_the_currency():
    """The monitor does not always carry a currency; the suffix always does."""
    text = strip_bidi(messages.alert(event(
        symbol="TEVA.TA",
        details={"change_pct": -2.0, "window_minutes": 5, "from_price": 1900.0,
                 "to_price": 1862.0, "currency": ""},
    )))

    assert "אג׳" in text and "$" not in text


def test_a_shekel_quote_uses_the_shekel_sign():
    assert "₪12.30" in strip_bidi(messages.money(12.3, "ILS"))


# ── Bidirectional text ────────────────────────────────────────────────────────

def test_every_line_declares_itself_right_to_left():
    """A line opening with a ticker or a digit is still a Hebrew line."""
    rendered = messages.alert(event())
    assert all(line.startswith(messages.RLM) for line in rendered.split("\n"))


def test_prices_are_isolated_so_they_cannot_swap_places():
    """The failure this prevents: two prices reordering, in an alert about them."""
    rendered = messages.alert(event())
    assert rendered.count("⁦") == rendered.count("⁩")
    assert "⁦$412.50⁩" in rendered


def test_no_arrow_glyphs_in_the_text():
    """← and → carry Bidi_Mirrored, so they render backwards inside Hebrew."""
    rendered = messages.alert(event())
    assert "←" not in rendered and "→" not in rendered


# ── What the engine hands over ────────────────────────────────────────────────

def _engine(alert):
    config = AppConfig(stocks=[StockConfig(symbol="MU", alerts=[alert])])

    class Feed:
        def get_average_daily_volume(self, symbol):
            return 10_000_000

    return AlertEngine(config, Feed())


def test_the_engine_records_the_facts_of_a_price_move():
    alert = AlertConfig(type="price_change_pct", threshold_pct=1.0,
                        window_minutes=15, cooldown_minutes=0)
    engine = _engine(alert)
    for _ in range(2):
        engine._history_for("MU").append(
            PricePoint(timestamp=datetime.now().astimezone(), price=100.0, volume=1)
        )

    fired = engine._check_price_change_pct("MU", alert, 105.0, "USD")
    assert fired is not None
    assert fired.details["from_price"] == 100.0
    assert fired.details["to_price"] == 105.0
    assert fired.details["window_minutes"] == 15
    assert fired.details["change_pct"] == pytest.approx(5.0)
    assert fired.details["currency"] == "USD"


def test_the_engine_records_the_facts_of_a_volume_spike():
    alert = AlertConfig(type="volume_spike", multiplier=2.0, time_adjusted=False)
    fired = _engine(alert)._check_volume_spike("MU", alert, 32_000_000, "regular", "USD")

    assert fired is not None
    assert fired.details["volume"] == 32_000_000
    assert fired.details["avg_volume"] == 10_000_000
    assert fired.details["ratio"] == pytest.approx(3.2)
    assert fired.details["time_adjusted"] is False


def test_the_engine_records_the_facts_of_a_threshold_crossing():
    alert = AlertConfig(type="price_threshold", above=400.0, cooldown_minutes=0)
    engine = _engine(alert)
    engine._check_price_threshold("MU", alert, 395.0, "USD")   # first observation
    fired = engine._check_price_threshold("MU", alert, 401.0, "USD")

    assert fired is not None
    assert fired.details == {"direction": "above", "level": 400.0,
                             "price": 401.0, "currency": "USD"}


def test_the_english_one_liner_survives_for_the_log_and_the_history():
    """The alert table and the log lines stay greppable in English."""
    alert = AlertConfig(type="volume_spike", multiplier=2.0, time_adjusted=False)
    fired = _engine(alert)._check_volume_spike("MU", alert, 32_000_000, "regular")

    assert "$MU volume spike" in fired.message


def test_the_quote_currency_reaches_the_event_through_evaluate():
    alert = AlertConfig(type="price_threshold", below=2000.0, cooldown_minutes=0)
    config = AppConfig(stocks=[StockConfig(symbol="TEVA.TA", alerts=[alert])])
    engine = AlertEngine(config, None)
    stock_cfg = config.stocks[0]
    base = {"timestamp": datetime.now().astimezone(), "volume": 1, "currency": "ILA"}

    engine.evaluate("TEVA.TA", stock_cfg, {**base, "price": 2100.0})
    events = engine.evaluate("TEVA.TA", stock_cfg, {**base, "price": 1900.0})

    assert events and events[0].details["currency"] == "ILA"


# ── What gets stored ──────────────────────────────────────────────────────────

def test_the_stored_summary_is_hebrew_and_plain():
    """It lands in a table cell and a database column, not a chat bubble."""
    summary = messages.alert_summary(event())

    assert "צניחה של 3.21%" in summary
    assert "מ-$412.50 ל-$399.26" in summary
    assert "*" not in summary, "no Markdown in a table cell"
    assert messages.RLM not in summary and "⁦" not in summary


def test_the_stored_summary_is_one_line():
    assert "\n" not in messages.alert_summary(event(alert_type="volume_spike", details={
        "volume": 52_000_000, "ratio": 2.4, "avg_volume": 10_000_000,
        "session_elapsed_pct": 43.0, "time_adjusted": True,
    }))


def test_the_stored_summary_drops_the_timestamp_line():
    """The store stamps its own time; repeating it in the text wastes the cell."""
    assert "ET" not in messages.alert_summary(event())


def test_the_stored_summary_starts_with_the_symbol():
    assert messages.alert_summary(event(symbol="ESLT.TA")).startswith("ESLT.TA")


# ── What actually goes out ────────────────────────────────────────────────────

class _Telegram:
    def __init__(self):
        self.sent = []

    def send(self, text, reply_markup=None):
        self.sent.append({"text": text, "reply_markup": reply_markup})
        return True


@pytest.fixture
def dispatcher():
    inst = NotificationDispatcher(NotificationConfig())
    telegram = _Telegram()
    inst._telegram = telegram
    inst._desktop = None
    inst._discord = None
    return inst, telegram


def test_the_dispatcher_sends_the_hebrew_text(dispatcher):
    inst, telegram = dispatcher
    inst.dispatch(event(), "regular")

    assert "צניחה של" in strip_bidi(telegram.sent[0]["text"])
    assert "STOCK ALERT" not in telegram.sent[0]["text"], "the English header is gone"


def test_every_alert_carries_the_two_actions_worth_taking(dispatcher):
    inst, telegram = dispatcher
    inst.dispatch(event(symbol="NVDA"), "regular")

    buttons = telegram.sent[0]["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["analyze:NVDA", "mute:NVDA:60"]


def test_the_buttons_name_the_symbol_that_fired():
    assert _alert_buttons(event(symbol="ESLT.TA"))["inline_keyboard"][0][1][
        "callback_data"] == "mute:ESLT.TA:60"


def test_the_earnings_reminder_is_not_framed_as_an_external_webhook(dispatcher):
    inst, telegram = dispatcher
    title, body = messages.earnings_alert("AVGO", 3, "14/11/2026")
    inst.dispatch_raw(title, body, framed=False)

    assert "📡" not in telegram.sent[0]["text"], "one header is enough"
    assert "דיווח תוצאות" in strip_bidi(telegram.sent[0]["text"])


def test_a_webhook_alert_is_framed(dispatcher):
    inst, telegram = dispatcher
    inst.dispatch_raw("TradingView", "AAPL broke out")

    assert "📡" in telegram.sent[0]["text"]
