"""The free-text alert feature end to end: chat, delivery, and the poll loop.

The chat part is about one rule: nothing is saved until the parse has been read
back and confirmed. The delivery part is about another: an alert the user asked
for must not disappear because the channel they chose is not configured.
"""

import asyncio
import json
import smtplib
from datetime import datetime, timezone

import pandas as pd
import pytest
import requests

from stock_monitor import custom_alerts, email_notifier, messages
from stock_monitor import telegram_bot as tb
from stock_monitor.config import NotificationConfig
from stock_monitor.custom_alerts import CustomAlert
from stock_monitor.notifier import NotificationDispatcher
from stock_monitor.store import custom_alert_store, watchlist_store


def strip_bidi(text):
    for mark in (messages.RLM, "⁦", "⁩"):
        text = text.replace(mark, "")
    return text


def run(coro):
    return asyncio.run(coro)


def rule(symbol="AVGO", kind="pct_from_open", channel="telegram", **params):
    base = {"direction": "up", "threshold_pct": 3.0}
    return CustomAlert(symbol=symbol, kind=kind, params={**base, **params},
                       channel=channel, request=f"תתריע לי על {symbol}", id=7)


class Recorder:
    def __init__(self):
        self.messages = []

    def send(self, chat_id, text, reply_markup=None, disable_preview=False,
             parse_mode="Markdown"):
        self.messages.append({"text": text, "reply_markup": reply_markup})

    @property
    def last(self):
        return self.messages[-1]

    def text(self, index=-1):
        return strip_bidi(self.messages[index]["text"])

    def all_text(self):
        return strip_bidi("\n".join(m["text"] for m in self.messages))


class Analyst:
    """Answers the alert-parsing prompt with a canned payload."""

    def __init__(self, **payload):
        self.payload = {"ok": True, **payload}
        self.calls = 0

    def parse_alert_request(self, system_prompt, request):
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)


def make_bot(recorder, analyst=None, watchlist=()):
    bot = tb.TelegramCommandBot.__new__(tb.TelegramCommandBot)
    bot._analyst = analyst
    bot._authorized_chat_id = ""
    bot._send = recorder.send
    bot._answer_callback = lambda *args, **kwargs: None
    bot._pending = {}
    bot._parsed = {}
    bot._rules = {}
    bot._news = None
    bot._feed = None
    bot._rc_ref = {}
    bot._config_syms = list(watchlist)
    return bot


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    custom_alerts.reset_state()
    for existing in list(custom_alert_store.all()):
        custom_alert_store.remove(existing.id)
    original = list(watchlist_store.all())
    for symbol in original:
        watchlist_store.remove(symbol)
    monkeypatch.setattr(tb, "symbol_is_known", lambda symbol: True)
    # No SMTP in the test environment unless a test says otherwise.
    for name in (email_notifier.ENV_HOST, email_notifier.ENV_USER,
                 email_notifier.ENV_PASSWORD, email_notifier.ENV_TO,
                 email_notifier.ENV_RESEND_KEY, email_notifier.ENV_RESEND_FROM,
                 email_notifier.ENV_PORT):
        monkeypatch.delenv(name, raising=False)
    yield
    for existing in list(custom_alert_store.all()):
        custom_alert_store.remove(existing.id)
    for symbol in list(watchlist_store.all()):
        watchlist_store.remove(symbol)
    for symbol in original:
        watchlist_store.add(symbol)
    custom_alerts.reset_state()


PCT_PAYLOAD = {
    "symbol": "ANT", "kind": "pct_from_open",
    "params": {"direction": "up", "threshold_pct": 3},
    "channel": "telegram",
    "explain": "התראה כשANT עולה 3% מנקודת הפתיחה",
}


# ── Asking in your own words ──────────────────────────────────────────────────

def test_a_sentence_is_read_as_a_request_without_a_button():
    recorder = Recorder()
    analyst = Analyst(**PCT_PAYLOAD)
    bot = make_bot(recorder, analyst)

    run(bot._handle({"chat": {"id": 1},
                     "text": "תתריע לי כשמניית ANT עולה ב-3% מנקודת הפתיחה"}))

    assert analyst.calls == 1
    assert "זה מה שהבנתי" in recorder.text()


def test_a_ticker_is_still_a_quote_request_not_an_alert():
    recorder = Recorder()
    analyst = Analyst(**PCT_PAYLOAD)
    bot = make_bot(recorder, analyst)
    bot._feed = type("F", (), {"get_current_data": staticmethod(lambda s: None)})()

    run(bot._handle({"chat": {"id": 1}, "text": "AVGO"}))

    assert analyst.calls == 0


def test_the_button_asks_for_the_request_and_waits():
    recorder = Recorder()
    bot = make_bot(recorder, Analyst(**PCT_PAYLOAD))
    run(bot._handle_callback({"id": "1", "message": {"chat": {"id": 1}},
                              "data": "ca_new"}))

    assert "מה להתריע לך" in recorder.text()
    assert bot._pending[1][0] == "custom_alert"


def test_the_message_after_the_button_is_read_as_the_request():
    recorder = Recorder()
    analyst = Analyst(**PCT_PAYLOAD)
    bot = make_bot(recorder, analyst)
    run(bot._handle_callback({"id": "1", "message": {"chat": {"id": 1}},
                              "data": "ca_new"}))
    run(bot._handle({"chat": {"id": 1}, "text": "כשANT עולה 3 אחוז מהפתיחה"}))

    assert analyst.calls == 1


def test_the_examples_offered_are_ones_the_engine_can_actually_evaluate():
    recorder = Recorder()
    make_bot(recorder)._ask_for_alert_request(1)
    text = recorder.text()

    assert "מנקודת הפתיחה" in text and "ממוצע 150 יום" in text


# ── Nothing is saved before it is confirmed ───────────────────────────────────

def test_the_parse_is_read_back_before_anything_is_stored():
    """A misread rule either never fires or fires wrongly; both are worse than a question."""
    recorder = Recorder()
    bot = make_bot(recorder, Analyst(**PCT_PAYLOAD))
    run(bot._handle({"chat": {"id": 1}, "text": "תתריע לי כשANT עולה ב-3% מהפתיחה"}))

    assert custom_alert_store.all() == []
    text = recorder.text()
    assert "ANT" in text and "מנקודת הפתיחה" in text and "לאשר?" in text
    data = [b["callback_data"] for row in recorder.last["reply_markup"]["inline_keyboard"]
            for b in row]
    assert data == ["ca_ok", "ca_new", "ca_cancel"]


def test_confirming_saves_the_rule():
    recorder = Recorder()
    bot = make_bot(recorder, Analyst(**PCT_PAYLOAD))
    run(bot._handle({"chat": {"id": 1}, "text": "תתריע לי כשANT עולה ב-3% מהפתיחה"}))
    run(bot._handle_callback({"id": "1", "message": {"chat": {"id": 1}},
                              "data": "ca_ok"}))

    saved = custom_alert_store.all()
    assert len(saved) == 1
    assert saved[0].symbol == "ANT" and saved[0].kind == "pct_from_open"
    assert "נשמרה" in recorder.text()


def test_cancelling_saves_nothing():
    recorder = Recorder()
    bot = make_bot(recorder, Analyst(**PCT_PAYLOAD))
    run(bot._handle({"chat": {"id": 1}, "text": "תתריע לי כשANT עולה ב-3% מהפתיחה"}))
    run(bot._handle_callback({"id": "1", "message": {"chat": {"id": 1}},
                              "data": "ca_cancel"}))

    assert custom_alert_store.all() == []
    assert "בוטלה" in recorder.text()


def test_confirming_twice_does_not_save_twice():
    recorder = Recorder()
    bot = make_bot(recorder, Analyst(**PCT_PAYLOAD))
    run(bot._handle({"chat": {"id": 1}, "text": "תתריע לי כשANT עולה ב-3% מהפתיחה"}))
    run(bot._handle_callback({"id": "1", "message": {"chat": {"id": 1}}, "data": "ca_ok"}))
    run(bot._handle_callback({"id": "2", "message": {"chat": {"id": 1}}, "data": "ca_ok"}))

    assert len(custom_alert_store.all()) == 1


def test_a_stale_confirmation_is_refused():
    recorder = Recorder()
    bot = make_bot(recorder, Analyst(**PCT_PAYLOAD))
    run(bot._handle({"chat": {"id": 1}, "text": "תתריע לי כשANT עולה ב-3% מהפתיחה"}))
    alert, _ = bot._parsed[1]
    bot._parsed[1] = (alert, -10_000.0)       # parsed long ago

    run(bot._handle_callback({"id": "1", "message": {"chat": {"id": 1}}, "data": "ca_ok"}))

    assert custom_alert_store.all() == []
    assert "יותר מדי זמן" in recorder.text()


def test_confirming_without_a_pending_parse_says_so():
    recorder = Recorder()
    run(make_bot(recorder)._handle_callback(
        {"id": "1", "message": {"chat": {"id": 1}}, "data": "ca_ok"}))

    assert "לא בתוקף" in recorder.text()


def test_a_stranger_cannot_create_alerts():
    recorder = Recorder()
    analyst = Analyst(**PCT_PAYLOAD)
    bot = make_bot(recorder, analyst)
    bot._authorized_chat_id = "999"

    run(bot._handle({"chat": {"id": 1}, "text": "תתריע לי כשANT עולה ב-3%"}))

    assert analyst.calls == 0 and recorder.messages == []


# ── What the confirmation has to say ──────────────────────────────────────────

def test_the_confirmation_states_the_channel():
    recorder = Recorder()
    bot = make_bot(recorder, Analyst(**{**PCT_PAYLOAD, "channel": "email"}))
    run(bot._handle({"chat": {"id": 1}, "text": "תתריע לי במייל כשANT עולה 3%"}))

    assert "אימייל" in recorder.text()


def test_an_email_rule_warns_when_mail_is_not_configured():
    """Promising mail that cannot be sent is the one thing not to do silently."""
    recorder = Recorder()
    bot = make_bot(recorder, Analyst(**{**PCT_PAYLOAD, "channel": "email"}))
    run(bot._handle({"chat": {"id": 1}, "text": "תתריע לי במייל כשANT עולה 3%"}))

    text = recorder.text()
    assert "שליחה במייל עוד לא מוגדרת" in text
    assert "RESEND_API_KEY" in text, "and it names what is missing"


def test_no_such_warning_once_mail_is_configured(monkeypatch):
    monkeypatch.setenv(email_notifier.ENV_HOST, "smtp.example.com")
    monkeypatch.setenv(email_notifier.ENV_USER, "me@example.com")
    monkeypatch.setenv(email_notifier.ENV_PASSWORD, "app-password")

    recorder = Recorder()
    bot = make_bot(recorder, Analyst(**{**PCT_PAYLOAD, "channel": "email"}))
    run(bot._handle({"chat": {"id": 1}, "text": "תתריע לי במייל כשANT עולה 3%"}))

    assert "לא מוגדרת" not in recorder.text()


def test_a_one_time_rule_says_it_will_switch_itself_off():
    recorder = Recorder()
    bot = make_bot(recorder, Analyst(**{**PCT_PAYLOAD, "once": True}))
    run(bot._handle({"chat": {"id": 1}, "text": "תתריע לי פעם אחת כשANT עולה 3%"}))

    assert "חד-פעמי" in recorder.text()


def test_an_unwatched_symbol_is_announced_and_then_added():
    recorder = Recorder()
    bot = make_bot(recorder, Analyst(**PCT_PAYLOAD))
    run(bot._handle({"chat": {"id": 1}, "text": "תתריע לי כשANT עולה ב-3%"}))
    assert "אינה ברשימת המעקב" in recorder.text()

    run(bot._handle_callback({"id": "1", "message": {"chat": {"id": 1}}, "data": "ca_ok"}))

    assert "ANT" in watchlist_store.all(), "or the rule would never be evaluated"


def test_an_already_watched_symbol_is_not_announced():
    watchlist_store.add("ANT")
    recorder = Recorder()
    bot = make_bot(recorder, Analyst(**PCT_PAYLOAD), watchlist=["ANT"])
    run(bot._handle({"chat": {"id": 1}, "text": "תתריע לי כשANT עולה ב-3%"}))

    assert "אינה ברשימת המעקב" not in recorder.text()


# ── When the request cannot be met ────────────────────────────────────────────

def test_an_unsupported_request_explains_what_can_be_asked():
    recorder = Recorder()
    analyst = Analyst(ok=False, reason="אין לי אינדיקטור RSI")
    bot = make_bot(recorder, analyst)
    run(bot._handle({"chat": {"id": 1}, "text": "תתריע לי כשRSI של AVGO עובר 70"}))

    text = recorder.text()
    assert "אין לי אינדיקטור RSI" in text
    assert "חציית ממוצע נע" in text, "and what is available instead"
    assert custom_alert_store.all() == []


def test_a_symbol_the_feed_does_not_know_is_refused(monkeypatch):
    monkeypatch.setattr(tb, "symbol_is_known", lambda symbol: False)
    recorder = Recorder()
    bot = make_bot(recorder, Analyst(**PCT_PAYLOAD))
    run(bot._handle({"chat": {"id": 1}, "text": "תתריע לי כשANT עולה ב-3%"}))

    assert "לא נמצאו נתונים" in recorder.text()
    assert custom_alert_store.all() == []


def test_an_unverifiable_symbol_is_still_offered(monkeypatch):
    """A throttled Yahoo must not be able to refuse a valid request."""
    monkeypatch.setattr(tb, "symbol_is_known", lambda symbol: None)
    recorder = Recorder()
    bot = make_bot(recorder, Analyst(**PCT_PAYLOAD))
    run(bot._handle({"chat": {"id": 1}, "text": "תתריע לי כשANT עולה ב-3%"}))

    assert "לאשר?" in recorder.text()


def test_without_ai_the_feature_says_what_to_configure():
    recorder = Recorder()
    run(make_bot(recorder, analyst=None)._handle(
        {"chat": {"id": 1}, "text": "תתריע לי כשANT עולה ב-3%"}))

    assert "ANTHROPIC_API_KEY" in recorder.text()


# ── Listing and removing ──────────────────────────────────────────────────────

def test_the_alerts_screen_lists_the_rules_with_a_delete_button(monkeypatch):
    monkeypatch.setattr(tb.alert_store, "recent", lambda n=5: [])
    saved = custom_alert_store.add(rule(symbol="ORCL", kind="ma_cross",
                                        direction="up", days=150))
    recorder = Recorder()
    run(make_bot(recorder)._send_alerts(1))

    text = recorder.text()
    assert "ההתראות שהגדרת" in text
    assert "ORCL" in text and "ממוצע 150 הימים" in text
    data = [b["callback_data"] for row in recorder.last["reply_markup"]["inline_keyboard"]
            for b in row]
    assert f"ca_rm:{saved.id}" in data
    assert "ca_new" in data, "and a way to add another"


def test_a_rule_can_be_deleted_from_the_screen(monkeypatch):
    monkeypatch.setattr(tb.alert_store, "recent", lambda n=5: [])
    saved = custom_alert_store.add(rule())
    recorder = Recorder()
    run(make_bot(recorder)._handle_callback(
        {"id": "1", "message": {"chat": {"id": 1}}, "data": f"ca_rm:{saved.id}"}))

    assert custom_alert_store.all() == []
    assert "נמחקה" in recorder.all_text()


def test_deleting_something_already_gone_is_not_an_error(monkeypatch):
    monkeypatch.setattr(tb.alert_store, "recent", lambda n=5: [])
    recorder = Recorder()
    run(make_bot(recorder)._handle_callback(
        {"id": "1", "message": {"chat": {"id": 1}}, "data": "ca_rm:-999"}))

    assert "לא נמצאה" in recorder.all_text()


def test_a_switched_off_rule_is_shown_with_a_way_to_re_arm_it(monkeypatch):
    monkeypatch.setattr(tb.alert_store, "recent", lambda n=5: [])
    saved = custom_alert_store.add(rule())
    custom_alert_store.set_enabled(saved.id, False)
    recorder = Recorder()
    run(make_bot(recorder)._send_alerts(1))

    assert "כבויה" in recorder.text()
    data = [b["callback_data"] for row in recorder.last["reply_markup"]["inline_keyboard"]
            for b in row]
    assert f"ca_on:{saved.id}" in data


def test_re_arming_works(monkeypatch):
    monkeypatch.setattr(tb.alert_store, "recent", lambda n=5: [])
    saved = custom_alert_store.add(rule())
    custom_alert_store.set_enabled(saved.id, False)
    recorder = Recorder()
    run(make_bot(recorder)._handle_callback(
        {"id": "1", "message": {"chat": {"id": 1}}, "data": f"ca_on:{saved.id}"}))

    assert custom_alert_store.get(saved.id).enabled is True


# ── Delivery ──────────────────────────────────────────────────────────────────

class _Telegram:
    def __init__(self):
        self.sent = []

    def send(self, text, reply_markup=None):
        self.sent.append(text)
        return True


class _Email:
    def __init__(self, ok=True, last_error=""):
        self.ok, self.sent = ok, []
        self.last_error = last_error

    def send(self, subject, body, to=None):
        self.sent.append({"subject": subject, "body": body})
        return self.ok


@pytest.fixture
def dispatcher():
    inst = NotificationDispatcher(NotificationConfig())
    telegram, email = _Telegram(), _Email()
    inst._telegram, inst._discord, inst._desktop = telegram, None, None
    inst._email = email
    return inst, telegram, email


def test_a_telegram_rule_goes_to_telegram_only(dispatcher):
    inst, telegram, email = dispatcher
    inst.dispatch_custom(rule(), "עלתה 3.2% מהפתיחה")

    assert len(telegram.sent) == 1 and email.sent == []
    assert "עלתה 3.2%" in strip_bidi(telegram.sent[0])


def test_an_email_rule_goes_to_mail_and_leaves_a_receipt_in_the_chat(dispatcher):
    """A silent Telegram would otherwise read as a rule that never fired."""
    inst, telegram, email = dispatcher
    inst.dispatch_custom(rule(channel="email"), "עלתה 3.2% מהפתיחה")

    assert len(email.sent) == 1
    assert "AVGO" in email.sent[0]["subject"]
    assert len(telegram.sent) == 1
    assert "במייל" in strip_bidi(telegram.sent[0])


def test_both_channels_send_the_whole_alert(dispatcher):
    inst, telegram, email = dispatcher
    inst.dispatch_custom(rule(channel="both"), "עלתה 3.2% מהפתיחה")

    assert len(email.sent) == 1
    assert "עלתה 3.2%" in strip_bidi(telegram.sent[0])


def test_a_failed_email_falls_back_to_telegram_with_the_reason(dispatcher):
    """The alert was asked for; losing it because mail is unset is not an option.

    And the reason travels with it: the exception knew why, so sending the user
    to read the host's logs would be the avoidable half of the problem.
    """
    inst, telegram, email = dispatcher
    inst._email = _Email(ok=False, last_error="מפתח ה-API של Resend נדחה.")
    inst.dispatch_custom(rule(channel="email"), "עלתה 3.2% מהפתיחה")

    text = strip_bidi(telegram.sent[0])
    assert "עלתה 3.2%" in text
    assert "מפתח ה-API של Resend נדחה" in text


def test_a_failure_with_no_recorded_reason_still_says_something(dispatcher):
    inst, telegram, email = dispatcher
    inst._email = _Email(ok=False)
    inst.dispatch_custom(rule(channel="email"), "עלתה 3.2%")

    assert "נכשלה" in strip_bidi(telegram.sent[0])


def test_a_muted_symbol_silences_its_custom_alerts_too(dispatcher):
    from stock_monitor import mute

    inst, telegram, email = dispatcher
    mute.mute("AVGO", 60)
    try:
        inst.dispatch_custom(rule(), "עלתה 3.2%")
    finally:
        mute.unmute_everything()

    assert telegram.sent == [] and email.sent == []


# ── The poll loop ─────────────────────────────────────────────────────────────

def _app(monkeypatch):
    from stock_monitor.config import AppConfig, StockConfig
    from stock_monitor.main import StockMonitorApp

    config = AppConfig(stocks=[StockConfig(symbol="AVGO", alerts=[])])
    app = StockMonitorApp(config)
    sent = []
    app.dispatcher.dispatch_custom = lambda alert, reason, session="regular": sent.append(
        {"symbol": alert.symbol, "reason": reason}
    )
    return app, sent


def test_a_rule_fires_through_the_cycle(monkeypatch):
    app, sent = _app(monkeypatch)
    saved = custom_alert_store.add(rule(symbol="ANT"))
    quotes = {"ANT": {"price": 103.0, "open_price": 100.0, "volume": 1, "currency": "USD"}}

    app._run_custom_rules([saved], quotes, "regular")

    assert [s["symbol"] for s in sent] == ["ANT"]
    assert custom_alert_store.get(saved.id).on_cooldown() is True


def test_a_rule_on_cooldown_is_skipped(monkeypatch):
    app, sent = _app(monkeypatch)
    saved = custom_alert_store.add(rule(symbol="ANT"))
    custom_alert_store.mark_fired(saved.id)
    quotes = {"ANT": {"price": 110.0, "open_price": 100.0, "volume": 1}}

    app._run_custom_rules([custom_alert_store.get(saved.id)], quotes, "regular")

    assert sent == []


def test_a_one_time_rule_switches_itself_off(monkeypatch):
    app, sent = _app(monkeypatch)
    alert = rule(symbol="ANT")
    alert.once = True
    saved = custom_alert_store.add(alert)
    quotes = {"ANT": {"price": 103.0, "open_price": 100.0, "volume": 1}}

    app._run_custom_rules([saved], quotes, "regular")

    assert len(sent) == 1
    assert custom_alert_store.get(saved.id).enabled is False


def test_one_broken_rule_does_not_stop_the_others(monkeypatch):
    app, sent = _app(monkeypatch)
    broken = rule(symbol="BAD")
    broken.params = {}                      # a rule no evaluation can read
    good = custom_alert_store.add(rule(symbol="ANT"))
    quotes = {
        "BAD": {"price": 10.0, "open_price": 9.0, "volume": 1},
        "ANT": {"price": 103.0, "open_price": 100.0, "volume": 1},
    }

    app._run_custom_rules([broken, good], quotes, "regular")

    assert [s["symbol"] for s in sent] == ["ANT"]


def test_a_symbol_with_no_quote_is_skipped_not_fatal(monkeypatch):
    app, sent = _app(monkeypatch)
    monkeypatch.setattr(app.data_feed, "get_current_data", lambda symbol: None)
    saved = custom_alert_store.add(rule(symbol="ANT"))

    app._run_custom_rules([saved], {}, "regular")

    assert sent == []


def test_the_firing_is_recorded_in_the_alert_history(monkeypatch):
    app, sent = _app(monkeypatch)
    recorded = []
    monkeypatch.setattr(tb.alert_store, "add",
                        lambda **kwargs: recorded.append(kwargs))
    from stock_monitor import main
    monkeypatch.setattr(main.alert_store, "add",
                        lambda **kwargs: recorded.append(kwargs))

    saved = custom_alert_store.add(rule(symbol="ANT"))
    app._run_custom_rules(
        [saved], {"ANT": {"price": 103.0, "open_price": 100.0, "volume": 1}}, "regular")

    assert recorded and recorded[0]["alert_type"] == "custom:pct_from_open"
    assert messages.RLM not in recorded[0]["message"], "the history stores plain text"


def test_rule_symbols_are_quoted_even_when_not_on_the_watchlist(monkeypatch):
    """Otherwise the rule is stored, listed, and never evaluated."""
    app, _ = _app(monkeypatch)
    custom_alert_store.add(rule(symbol="ORCL"))
    asked = {}

    def fake_batch(symbols):
        asked["symbols"] = list(symbols)
        return {}

    monkeypatch.setattr(app.data_feed, "get_current_data_batch", fake_batch)
    monkeypatch.setattr(app.data_feed, "get_current_data", lambda symbol: None)
    app._run_cycle()

    assert "ORCL" in asked["symbols"]


# ── Email, without a mail server ──────────────────────────────────────────────

def test_email_is_not_configured_without_the_variables():
    assert email_notifier.is_configured() is False
    assert email_notifier.active_provider() == ""
    # The one-line path is asked for first, not both providers at once.
    assert email_notifier.ENV_RESEND_KEY in email_notifier.missing_settings()


def test_email_is_configured_once_the_variables_are_set(monkeypatch):
    monkeypatch.setenv(email_notifier.ENV_HOST, "smtp.example.com")
    monkeypatch.setenv(email_notifier.ENV_USER, "me@example.com")
    monkeypatch.setenv(email_notifier.ENV_PASSWORD, "app-password")

    assert email_notifier.is_configured() is True
    assert email_notifier.missing_settings() == []
    assert email_notifier.settings()["to"] == "me@example.com", "defaults to the sender"


def test_the_recipient_can_differ_from_the_sender(monkeypatch):
    monkeypatch.setenv(email_notifier.ENV_USER, "bot@example.com")
    monkeypatch.setenv(email_notifier.ENV_TO, "me@example.com")

    assert email_notifier.settings()["to"] == "me@example.com"


def test_a_bad_port_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv(email_notifier.ENV_PORT, "not a port")
    assert email_notifier.settings()["port"] == email_notifier.DEFAULT_PORT


def test_the_password_is_never_returned(monkeypatch):
    monkeypatch.setenv(email_notifier.ENV_PASSWORD, "secret-app-password")
    serialized = json.dumps(email_notifier.status(), ensure_ascii=False)

    assert "secret-app-password" not in serialized
    assert email_notifier.settings()["has_password"] is True


def test_sending_without_configuration_fails_quietly():
    assert email_notifier.EmailNotifier().send("נושא", "גוף") is False


# ── Choosing a provider ───────────────────────────────────────────────────────

def test_resend_is_used_when_its_key_is_set(monkeypatch):
    monkeypatch.setenv(email_notifier.ENV_RESEND_KEY, "re_test")
    monkeypatch.setenv(email_notifier.ENV_TO, "me@example.com")

    assert email_notifier.active_provider() == "resend"
    assert email_notifier.is_configured() is True


def test_smtp_is_used_when_only_it_is_configured(monkeypatch):
    monkeypatch.setenv(email_notifier.ENV_HOST, "smtp.example.com")
    monkeypatch.setenv(email_notifier.ENV_USER, "me@example.com")
    monkeypatch.setenv(email_notifier.ENV_PASSWORD, "app-password")

    assert email_notifier.active_provider() == "smtp"


def test_resend_wins_when_both_are_configured(monkeypatch):
    """On a host that blocks SMTP, it is the one that can actually deliver."""
    monkeypatch.setenv(email_notifier.ENV_RESEND_KEY, "re_test")
    monkeypatch.setenv(email_notifier.ENV_HOST, "smtp.example.com")
    monkeypatch.setenv(email_notifier.ENV_USER, "me@example.com")
    monkeypatch.setenv(email_notifier.ENV_PASSWORD, "app-password")

    assert email_notifier.active_provider() == "resend"


def test_a_resend_key_without_a_recipient_is_not_configured(monkeypatch):
    monkeypatch.setenv(email_notifier.ENV_RESEND_KEY, "re_test")
    assert email_notifier.active_provider() == ""
    assert email_notifier.ENV_TO in email_notifier.missing_settings()


def test_a_half_configured_smtp_is_asked_to_be_finished(monkeypatch):
    """Having started down one path, the user is not sent down the other."""
    monkeypatch.setenv(email_notifier.ENV_HOST, "smtp.example.com")
    missing = email_notifier.missing_settings()

    assert email_notifier.ENV_PASSWORD in missing
    assert email_notifier.ENV_RESEND_KEY not in missing


def test_the_smtp_status_carries_the_render_warning(monkeypatch):
    """A correct SMTP setup on Render's free plan looks fine and cannot deliver."""
    monkeypatch.setenv(email_notifier.ENV_HOST, "smtp.gmail.com")
    monkeypatch.setenv(email_notifier.ENV_USER, "me@example.com")
    monkeypatch.setenv(email_notifier.ENV_PASSWORD, "app-password")

    assert "Render" in email_notifier.status()["warning"]


def test_the_resend_status_has_no_such_warning(monkeypatch):
    monkeypatch.setenv(email_notifier.ENV_RESEND_KEY, "re_test")
    monkeypatch.setenv(email_notifier.ENV_TO, "me@example.com")

    assert "warning" not in email_notifier.status()


# ── Sending through Resend ────────────────────────────────────────────────────

class _Response:
    def __init__(self, status_code=200, text='{"id": "abc"}'):
        self.status_code, self.text = status_code, text


@pytest.fixture
def resend(monkeypatch):
    monkeypatch.setenv(email_notifier.ENV_RESEND_KEY, "re_test_key")
    monkeypatch.setenv(email_notifier.ENV_TO, "me@example.com")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update({"url": url, "headers": headers, "body": json})
        return captured.get("response") or _Response()

    monkeypatch.setattr(email_notifier.requests, "post", fake_post)
    return captured


def test_a_mail_goes_out_over_https(resend):
    notifier = email_notifier.EmailNotifier()
    assert notifier.send("התראה: ZS", "חצתה את $233.30") is True

    assert resend["url"] == email_notifier.RESEND_URL
    assert resend["url"].startswith("https://"), "port 443, which Render allows"
    assert resend["body"]["to"] == ["me@example.com"]
    assert resend["body"]["subject"] == "התראה: ZS"
    assert "233.30" in resend["body"]["text"]
    assert notifier.last_error == ""


def test_the_key_travels_as_a_bearer_token(resend):
    email_notifier.EmailNotifier().send("נושא", "גוף")
    assert resend["headers"]["Authorization"] == "Bearer re_test_key"


def test_the_default_sender_is_resends_shared_address(resend):
    email_notifier.EmailNotifier().send("נושא", "גוף")
    assert resend["body"]["from"] == email_notifier.DEFAULT_FROM


def test_a_custom_sender_is_used_when_set(resend, monkeypatch):
    monkeypatch.setenv(email_notifier.ENV_RESEND_FROM, "alerts@mydomain.com")
    email_notifier.EmailNotifier().send("נושא", "גוף")

    assert resend["body"]["from"] == "alerts@mydomain.com"


def test_the_own_address_only_rule_is_explained(resend):
    """Resend's free sender delivers only to the account's own address."""
    resend["response"] = _Response(403, json.dumps(
        {"message": "You can only send testing emails to your own email address"}))
    notifier = email_notifier.EmailNotifier()

    assert notifier.send("נושא", "גוף") is False
    assert "אותה כתובת של חשבון ה-Resend" in notifier.last_error


def test_a_rejected_key_is_named_as_such(resend):
    resend["response"] = _Response(401, json.dumps({"message": "Invalid API key"}))
    notifier = email_notifier.EmailNotifier()

    assert notifier.send("נושא", "גוף") is False
    assert email_notifier.ENV_RESEND_KEY in notifier.last_error


def test_an_unverified_domain_is_explained(resend):
    resend["response"] = _Response(422, json.dumps(
        {"message": "The example.com domain is not verified"}))
    notifier = email_notifier.EmailNotifier()
    notifier.send("נושא", "גוף")

    assert "מאומת" in notifier.last_error


def test_a_rate_limit_says_to_wait(resend):
    resend["response"] = _Response(429, "{}")
    notifier = email_notifier.EmailNotifier()
    notifier.send("נושא", "גוף")

    assert "מגבלת השליחה" in notifier.last_error


def test_a_network_failure_does_not_raise(resend, monkeypatch):
    def boom(*args, **kwargs):
        raise requests.ConnectionError("dns failure")

    monkeypatch.setattr(email_notifier.requests, "post", boom)
    notifier = email_notifier.EmailNotifier()

    assert notifier.send("נושא", "גוף") is False
    assert notifier.last_error


def test_the_reason_is_readable_from_the_diagnostics(resend):
    resend["response"] = _Response(401, json.dumps({"message": "Invalid API key"}))
    email_notifier.notifier.send("נושא", "גוף")

    assert email_notifier.ENV_RESEND_KEY in email_notifier.status()["last_error"]


def test_a_success_clears_the_previous_reason(resend):
    notifier = email_notifier.EmailNotifier()
    notifier.last_error = "משהו קודם"
    notifier.send("נושא", "גוף")

    assert notifier.last_error == ""


# ── Sending a test on demand ──────────────────────────────────────────────────

def test_a_test_mail_goes_through_the_real_path(resend):
    """The point: learn whether mail works without waiting for a real alert."""
    result = email_notifier.send_test()

    assert result["sent"] is True
    assert result["provider"] == "resend"
    assert result["to"] == "me@example.com"
    assert result["error"] == ""
    assert resend["body"]["subject"] == email_notifier.TEST_SUBJECT


def test_the_test_mail_says_it_is_a_test_and_asks_for_nothing(resend):
    email_notifier.send_test()
    body = resend["body"]["text"]

    assert "בדיקה" in body
    assert "לא נדרשת שום פעולה" in body


def test_the_test_mail_shows_what_a_real_alert_looks_like(resend):
    email_notifier.send_test()
    assert "ZS" in resend["body"]["text"]


def test_a_test_can_be_sent_to_another_address(resend):
    email_notifier.send_test("someone@example.com")
    assert resend["body"]["to"] == ["someone@example.com"]


def test_a_failed_test_reports_the_provider_s_own_reason(resend):
    resend["response"] = _Response(401, json.dumps({"message": "Invalid API key"}))
    result = email_notifier.send_test()

    assert result["sent"] is False
    assert email_notifier.ENV_RESEND_KEY in result["error"]


def test_a_test_without_any_configuration_says_what_is_missing():
    result = email_notifier.send_test()

    assert result["sent"] is False
    assert result["provider"] == "none"
    assert email_notifier.ENV_RESEND_KEY in result["error"]


def test_the_bot_reports_a_successful_test(resend):
    recorder = Recorder()
    run(make_bot(recorder)._send_email_test(1))

    text = recorder.text()
    assert "נשלח" in text and "me@example.com" in text


def test_the_bot_reports_a_failed_test_with_the_reason(resend):
    resend["response"] = _Response(401, json.dumps({"message": "Invalid API key"}))
    recorder = Recorder()
    run(make_bot(recorder)._send_email_test(1))

    text = recorder.text()
    assert "לא נשלח" in text
    assert "RESEND_API_KEY" in text


def test_an_unconfigured_test_points_at_the_short_path():
    recorder = Recorder()
    run(make_bot(recorder)._send_email_test(1))

    assert "RESEND_API_KEY" in recorder.text()
    assert "DEPLOY.md" in recorder.text()


def test_the_alerts_screen_offers_the_test_and_states_the_mail_setup(monkeypatch,
                                                                    resend):
    monkeypatch.setattr(tb.alert_store, "recent", lambda n=5: [])
    recorder = Recorder()
    run(make_bot(recorder)._send_alerts(1))

    text = recorder.text()
    assert "מייל: resend" in text and "me@example.com" in text
    data = [b["callback_data"] for row in recorder.last["reply_markup"]["inline_keyboard"]
            for b in row]
    assert "email_test" in data


def test_the_alerts_screen_says_when_mail_is_unconfigured(monkeypatch):
    monkeypatch.setattr(tb.alert_store, "recent", lambda n=5: [])
    recorder = Recorder()
    run(make_bot(recorder)._send_alerts(1))

    assert "מייל: לא מוגדר" in recorder.text()
    assert "RESEND_API_KEY" in recorder.text()


def test_the_alerts_screen_surfaces_the_last_failure(monkeypatch, resend):
    monkeypatch.setattr(tb.alert_store, "recent", lambda n=5: [])
    resend["response"] = _Response(401, json.dumps({"message": "Invalid API key"}))
    email_notifier.notifier.send("נושא", "גוף")

    recorder = Recorder()
    run(make_bot(recorder)._send_alerts(1))

    assert "השליחה האחרונה נכשלה" in recorder.text()


@pytest.mark.parametrize("text", ["בדיקת מייל", "/testmail", "מייל בדיקה"])
def test_the_test_can_be_asked_for_by_typing(text):
    from stock_monitor.telegram_bot import route

    assert route(text) == "email_test"


def test_the_route_sends_a_test(resend):
    from fastapi.testclient import TestClient

    from stock_monitor.webhook_server import create_webhook_app

    client = TestClient(create_webhook_app(
        NotificationDispatcher(NotificationConfig()), ""))
    body = client.post("/api/diagnostics/email/test").json()

    assert body["sent"] is True


def test_sending_a_test_is_not_a_get(resend):
    """A GET with an effect fires on every link preview and crawler."""
    from fastapi.testclient import TestClient

    from stock_monitor.webhook_server import create_webhook_app

    client = TestClient(create_webhook_app(
        NotificationDispatcher(NotificationConfig()), ""))

    assert client.get("/api/diagnostics/email/test").status_code == 405


# ── What a failure says ───────────────────────────────────────────────────────

def test_a_blocked_port_is_named_as_the_likely_cause():
    """The failure this whole provider split exists for."""
    message = email_notifier.friendly_error(TimeoutError("timed out"))

    assert "Render" in message and "SMTP" in message


def test_a_rejected_password_points_at_the_app_password():
    message = email_notifier.friendly_error(
        smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted"))

    assert "App Password" in message


def test_a_wrong_port_for_the_handshake_is_named():
    assert "465" in email_notifier.friendly_error(
        smtplib.SMTPNotSupportedError("STARTTLS extension not supported"))


def test_a_refused_recipient_points_at_the_recipient_variable():
    message = email_notifier.friendly_error(
        smtplib.SMTPRecipientsRefused({"x@y.com": (550, b"no such user")}))

    assert email_notifier.ENV_TO in message


def test_an_unrecognised_failure_still_names_its_class():
    assert "ValueError" in email_notifier.friendly_error(ValueError("odd"))


def test_variable_names_survive_the_trip_to_a_markdown_chat():
    """They are instructions to copy; an underscore eaten by Markdown is a typo.

    A code span carries them through legacy Markdown intact, which stripping
    the underscores — as the chat helper does for text from the open web —
    did not: RESEND_API_KEY arrived as RESENDAPIKEY.
    """
    message = email_notifier._http_error(
        401, json.dumps({"message": "Invalid API key"}))

    assert "RESEND_API_KEY" in message
    assert "`" in message, "wrapped as code, so the underscores are literal"


class _SMTP:
    """Captures an SMTP conversation instead of having one."""

    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.context = host, port, context
        self.logged_in = None
        self.started_tls = False
        self.message = None
        _SMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, message):
        self.message = message


@pytest.fixture
def smtp(monkeypatch):
    _SMTP.instances = []
    monkeypatch.setattr(email_notifier.smtplib, "SMTP", _SMTP)
    monkeypatch.setattr(email_notifier.smtplib, "SMTP_SSL", _SMTP)
    monkeypatch.setenv(email_notifier.ENV_HOST, "smtp.example.com")
    monkeypatch.setenv(email_notifier.ENV_USER, "me@example.com")
    monkeypatch.setenv(email_notifier.ENV_PASSWORD, "app-password")
    return _SMTP


def test_a_mail_is_sent_with_the_subject_and_body(smtp):
    assert email_notifier.EmailNotifier().send("התראה: AVGO", "עלתה 3.2%") is True

    server = smtp.instances[0]
    assert server.logged_in == ("me@example.com", "app-password")
    assert server.message["Subject"] == "התראה: AVGO"
    assert "עלתה 3.2%" in server.message.get_content()


def test_port_587_upgrades_with_starttls(smtp, monkeypatch):
    monkeypatch.setenv(email_notifier.ENV_PORT, "587")
    email_notifier.EmailNotifier().send("נושא", "גוף")

    assert smtp.instances[0].started_tls is True


def test_port_465_connects_with_implicit_tls(smtp, monkeypatch):
    monkeypatch.setenv(email_notifier.ENV_PORT, "465")
    email_notifier.EmailNotifier().send("נושא", "גוף")

    server = smtp.instances[0]
    assert server.started_tls is False, "465 is already encrypted"
    assert server.context is not None


def test_a_refused_login_is_reported_as_failure_not_raised(smtp, monkeypatch):
    def boom(self, user, password):
        raise email_notifier.smtplib.SMTPAuthenticationError(535, b"nope")

    monkeypatch.setattr(_SMTP, "login", boom)

    assert email_notifier.EmailNotifier().send("נושא", "גוף") is False


def test_the_body_carries_hebrew_as_utf8(smtp):
    email_notifier.EmailNotifier().send("נושא", "שורה בעברית")
    assert "שורה בעברית" in smtp.instances[0].message.get_content()


# ── The moving average the cross rule needs ───────────────────────────────────

def _history(closes):
    index = pd.bdate_range(end=datetime.now(timezone.utc).date(), periods=len(closes))
    return pd.DataFrame({"Close": closes, "Volume": [1] * len(closes)}, index=index)


def test_the_moving_average_is_the_mean_of_the_last_n_closes(monkeypatch):
    from stock_monitor.data_feed import StockDataFeed

    feed = StockDataFeed()
    monkeypatch.setattr(feed, "_ticker", lambda symbol: type(
        "T", (), {"history": staticmethod(lambda **kw: _history([10.0] * 160))})())

    assert feed.get_moving_average("AVGO", 150) == pytest.approx(10.0)


def test_the_average_is_fetched_once_a_day_not_once_a_minute(monkeypatch):
    """A 150-day average moves daily; the monitor asks every minute."""
    from stock_monitor.data_feed import StockDataFeed

    feed = StockDataFeed()
    calls = []

    def ticker(symbol):
        calls.append(symbol)
        return type("T", (), {"history": staticmethod(lambda **kw: _history([10.0] * 160))})()

    monkeypatch.setattr(feed, "_ticker", ticker)
    feed.get_moving_average("AVGO", 150)
    feed.get_moving_average("AVGO", 150)

    assert len(calls) == 1


def test_different_lengths_are_cached_separately(monkeypatch):
    from stock_monitor.data_feed import StockDataFeed

    feed = StockDataFeed()
    monkeypatch.setattr(feed, "_ticker", lambda symbol: type(
        "T", (), {"history": staticmethod(lambda **kw: _history(list(range(1, 201))))})())

    short = feed.get_moving_average("AVGO", 50)
    long = feed.get_moving_average("AVGO", 200)

    assert short != long


def test_too_little_history_yields_no_average(monkeypatch):
    """A listing younger than the window has no 150-day average to cross."""
    from stock_monitor.data_feed import StockDataFeed

    feed = StockDataFeed()
    monkeypatch.setattr(feed, "_ticker", lambda symbol: type(
        "T", (), {"history": staticmethod(lambda **kw: _history([10.0] * 20))})())

    assert feed.get_moving_average("AVGO", 150) is None


def test_a_failing_history_request_yields_no_average(monkeypatch):
    from stock_monitor.data_feed import StockDataFeed

    def boom(**kwargs):
        raise RuntimeError("429 Too Many Requests")

    feed = StockDataFeed()
    monkeypatch.setattr(feed, "_ticker", lambda symbol: type(
        "T", (), {"history": staticmethod(boom)})())

    assert feed.get_moving_average("AVGO", 150) is None
