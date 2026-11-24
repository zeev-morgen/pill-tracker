"""Alerts asked for in free text: the rules, their evaluation, and the parse.

Two properties carry this feature, and most of this file is about them.

A *crossing* needs two observations. A rule written while the price is already
above its level has not witnessed a crossing, and reporting one would be a
false account of an event that never happened.

A *reference point* is part of the alert. The user's own example asks for 3%
from the open rather than from the session's low, so "from the open" and "from
yesterday's close" are separate kinds and each one says which it is in the
message it sends.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from stock_monitor import custom_alerts, messages
from stock_monitor.custom_alerts import CustomAlert, RuleError
from stock_monitor.store import custom_alert_store


def strip_bidi(text):
    for mark in (messages.RLM, "⁦", "⁩"):
        text = text.replace(mark, "")
    return text


def rule(kind="pct_from_open", symbol="AVGO", **params):
    defaults = {
        "pct_from_open": {"direction": "up", "threshold_pct": 3.0},
        "pct_from_close": {"direction": "up", "threshold_pct": 3.0},
        "price_level": {"direction": "up", "price": 400.0},
        "ma_cross": {"direction": "up", "days": 150},
        "volume_multiple": {"multiple": 2.5},
    }[kind]
    return CustomAlert(symbol=symbol, kind=kind, params={**defaults, **params},
                       id=1)


def quote(price, **extra):
    return {"price": price, "volume": 1_000_000, "currency": "USD", **extra}


class _Feed:
    def __init__(self, average=None, volume_average=None):
        self._average, self._volume_average = average, volume_average
        self.ma_calls = 0

    def get_moving_average(self, symbol, days):
        self.ma_calls += 1
        return self._average

    def get_average_daily_volume(self, symbol, days=10):
        return self._volume_average


@pytest.fixture(autouse=True)
def clean():
    custom_alerts.reset_state()
    for existing in list(custom_alert_store.all()):
        custom_alert_store.remove(existing.id)
    yield
    custom_alerts.reset_state()
    for existing in list(custom_alert_store.all()):
        custom_alert_store.remove(existing.id)


# ── Validation ────────────────────────────────────────────────────────────────

def test_a_well_formed_rule_validates():
    assert rule().validate() is not None


def test_an_unknown_kind_is_refused():
    with pytest.raises(RuleError):
        CustomAlert(symbol="AVGO", kind="moon_phase").validate()


def test_a_missing_direction_is_refused():
    with pytest.raises(RuleError):
        CustomAlert(symbol="AVGO", kind="pct_from_open",
                    params={"threshold_pct": 3}).validate()


def test_a_nonsense_percentage_is_refused():
    """0.001% is noise and 900% is not an alert; both mean a misread request."""
    for bad in (0.0, 0.01, 900.0):
        with pytest.raises(RuleError):
            rule(threshold_pct=bad).validate()


def test_a_missing_number_is_refused_rather_than_defaulted():
    with pytest.raises(RuleError):
        CustomAlert(symbol="AVGO", kind="price_level",
                    params={"direction": "up"}).validate()


def test_a_negative_price_level_is_refused():
    with pytest.raises(RuleError):
        rule("price_level", price=-5).validate()


def test_an_impossible_moving_average_length_is_refused():
    for bad in (1, 0, 900):
        with pytest.raises(RuleError):
            rule("ma_cross", days=bad).validate()


def test_a_fractional_moving_average_length_is_refused():
    with pytest.raises(RuleError):
        rule("ma_cross", days=150.5).validate()


def test_a_whole_float_length_is_accepted_as_an_integer():
    """JSON has no ints; 150.0 from the model is 150 days."""
    validated = rule("ma_cross", days=150.0).validate()
    assert validated.params["days"] == 150
    assert isinstance(validated.params["days"], int)


def test_a_volume_multiple_below_one_is_refused():
    """"More than 0.5x the average" is every minute of every day."""
    with pytest.raises(RuleError):
        rule("volume_multiple", multiple=0.5).validate()


def test_an_unknown_channel_is_refused():
    with pytest.raises(RuleError):
        CustomAlert(symbol="AVGO", kind="pct_from_open", channel="fax",
                    params={"direction": "up", "threshold_pct": 3}).validate()


def test_a_missing_symbol_is_refused():
    with pytest.raises(RuleError):
        CustomAlert(symbol="", kind="pct_from_open",
                    params={"direction": "up", "threshold_pct": 3}).validate()


def test_an_absurd_cooldown_is_replaced_with_the_default():
    assert rule().validate().cooldown_minutes == custom_alerts.DEFAULT_COOLDOWN_MINUTES
    broken = rule()
    broken.cooldown_minutes = 0
    assert broken.validate().cooldown_minutes == custom_alerts.DEFAULT_COOLDOWN_MINUTES


# ── Wording ───────────────────────────────────────────────────────────────────

def test_the_description_names_the_reference_point():
    """The distinction the user asked for, and the point of confirming at all."""
    assert "מנקודת הפתיחה" in strip_bidi(rule("pct_from_open").describe())
    assert "מהסגירה של אתמול" in strip_bidi(rule("pct_from_close").describe())


def test_the_description_names_the_direction():
    assert "עולה" in strip_bidi(rule(direction="up").describe())
    assert "יורדת" in strip_bidi(rule(direction="down").describe())
    assert "כלפי מטה" in strip_bidi(rule("price_level", direction="down").describe())


def test_the_description_prices_a_level_in_its_own_currency():
    """An agorot level shown with a dollar sign is a hundredfold mistake."""
    assert "אג׳" in strip_bidi(rule("price_level", symbol="TEVA.TA",
                                    price=1800.0).describe())
    assert "$400.00" in strip_bidi(rule("price_level").describe())


def test_the_description_of_a_moving_average_gives_its_length():
    assert "ממוצע 150 הימים" in strip_bidi(rule("ma_cross").describe())


def test_the_summary_line_carries_the_channel_and_the_repetition():
    alert = rule()
    alert.channel, alert.once = "email", True
    text = strip_bidi(alert.summary())

    assert "AVGO" in text and "אימייל" in text and "פעם אחת" in text


# ── Percentage from a baseline ────────────────────────────────────────────────

def test_a_rise_from_the_open_fires_at_the_threshold():
    fired = custom_alerts.evaluate(rule(threshold_pct=3.0),
                                   quote(103.0, open_price=100.0))
    assert fired and "3.00%" in strip_bidi(fired)
    assert "הפתיחה" in strip_bidi(fired)


def test_a_rise_short_of_the_threshold_does_not_fire():
    assert custom_alerts.evaluate(rule(threshold_pct=3.0),
                                  quote(102.9, open_price=100.0)) is None


def test_the_baseline_is_the_open_and_not_the_session_low():
    """The user's explicit requirement: from the open, not from the low.

    Up 3% from the day's low while still down from the open is not what was
    asked for, and the built-in rolling-window rule would have fired on it.
    """
    assert custom_alerts.evaluate(
        rule(threshold_pct=3.0),
        quote(99.0, open_price=100.0, day_low=96.0),
    ) is None


def test_a_fall_fires_a_downward_rule_and_a_rise_does_not():
    down = rule(direction="down", threshold_pct=3.0)
    assert custom_alerts.evaluate(down, quote(96.5, open_price=100.0))
    assert custom_alerts.evaluate(down, quote(104.0, open_price=100.0)) is None


def test_a_rise_does_not_fire_a_downward_rule_at_the_same_size():
    assert custom_alerts.evaluate(rule(direction="down", threshold_pct=3.0),
                                  quote(103.0, open_price=100.0)) is None


def test_the_close_baseline_uses_yesterdays_close():
    fired = custom_alerts.evaluate(rule("pct_from_close", threshold_pct=2.0),
                                   quote(103.0, prev_close=100.0, open_price=102.9))
    assert fired and "הסגירה של אתמול" in strip_bidi(fired)


def test_a_missing_baseline_does_not_fire_on_a_guess():
    """Pre-market, there is no open yet; inventing one would invent an alert."""
    assert custom_alerts.evaluate(rule(), quote(103.0)) is None
    assert custom_alerts.evaluate(rule("pct_from_close"), quote(103.0)) is None


def test_a_quote_without_a_price_never_fires():
    assert custom_alerts.evaluate(rule(), {"open_price": 100.0}) is None


def test_the_message_has_no_arrow_glyphs():
    """← and → are Bidi_Mirrored and render backwards inside Hebrew."""
    fired = custom_alerts.evaluate(rule(), quote(103.0, open_price=100.0))
    assert "←" not in fired and "→" not in fired
    assert "מ-" in strip_bidi(fired) and "ל-" in strip_bidi(fired)


def test_the_message_quotes_an_israeli_move_in_agorot():
    fired = custom_alerts.evaluate(
        rule(symbol="TEVA.TA", threshold_pct=2.0),
        {"price": 1836.0, "open_price": 1800.0, "currency": "ILA"},
    )
    assert "אג׳" in strip_bidi(fired) and "$" not in strip_bidi(fired)


# ── Crossings ─────────────────────────────────────────────────────────────────

def test_a_price_level_needs_a_crossing_not_a_condition():
    """The property that keeps a new rule from firing on arrival."""
    level = rule("price_level", price=400.0)

    assert custom_alerts.evaluate(level, quote(410.0)) is None, "first poll records only"
    assert custom_alerts.evaluate(level, quote(411.0)) is None, "still above, no crossing"


def test_a_price_level_fires_when_the_line_is_crossed():
    level = rule("price_level", price=400.0)
    custom_alerts.evaluate(level, quote(395.0))          # below
    fired = custom_alerts.evaluate(level, quote(401.0))  # crosses up

    assert fired and "חצתה" in strip_bidi(fired)
    assert "$400.00" in strip_bidi(fired)


def test_a_downward_level_fires_only_downwards():
    level = rule("price_level", direction="down", price=400.0)
    custom_alerts.evaluate(level, quote(410.0))
    assert custom_alerts.evaluate(level, quote(399.0))

    custom_alerts.reset_state()
    rising = rule("price_level", direction="down", price=400.0)
    custom_alerts.evaluate(rising, quote(390.0))
    assert custom_alerts.evaluate(rising, quote(401.0)) is None


def test_two_rules_do_not_share_a_side():
    """State is keyed per rule; sharing it would have one read the other's line."""
    first = rule("price_level", price=400.0)
    second = rule("price_level", price=500.0)
    second.id = 2

    custom_alerts.evaluate(first, quote(450.0))     # above 400, below 500
    custom_alerts.evaluate(second, quote(450.0))
    assert custom_alerts.evaluate(second, quote(505.0)), "500 was crossed"


def test_a_moving_average_cross_fires_on_the_crossing():
    feed = _Feed(average=100.0)
    ma = rule("ma_cross", days=150)
    custom_alerts.evaluate(ma, quote(98.0), feed)
    fired = custom_alerts.evaluate(ma, quote(101.0), feed)

    assert fired
    text = strip_bidi(fired)
    assert "ממוצע 150 הימים" in text and "$100.00" in text


def test_a_moving_average_cross_does_not_fire_on_the_first_poll():
    feed = _Feed(average=100.0)
    assert custom_alerts.evaluate(rule("ma_cross"), quote(120.0), feed) is None


def test_no_average_means_no_alert_rather_than_a_guess():
    """A stock with less history than the window cannot have a 150-day average."""
    assert custom_alerts.evaluate(rule("ma_cross"), quote(120.0), _Feed(None)) is None


def test_a_moving_average_rule_without_a_feed_is_skipped():
    assert custom_alerts.evaluate(rule("ma_cross"), quote(120.0), None) is None


# ── Volume ────────────────────────────────────────────────────────────────────

def test_a_volume_multiple_fires_above_the_multiple():
    feed = _Feed(volume_average=1_000_000)
    fired = custom_alerts.evaluate(rule("volume_multiple", multiple=2.0),
                                   quote(100.0, volume=2_500_000), feed)
    assert fired and "2,500,000" in strip_bidi(fired)


def test_a_normal_volume_does_not_fire():
    feed = _Feed(volume_average=1_000_000)
    assert custom_alerts.evaluate(rule("volume_multiple", multiple=2.0),
                                  quote(100.0, volume=1_500_000), feed) is None


def test_no_volume_average_means_no_alert():
    assert custom_alerts.evaluate(rule("volume_multiple"),
                                  quote(100.0, volume=9_000_000),
                                  _Feed(volume_average=None)) is None


# ── Cooldown ──────────────────────────────────────────────────────────────────

def test_a_rule_is_on_cooldown_right_after_firing():
    alert = rule()
    alert.last_fired_at = datetime.now(timezone.utc)
    assert alert.on_cooldown() is True


def test_the_cooldown_expires():
    alert = rule()
    alert.cooldown_minutes = 60
    alert.last_fired_at = datetime.now(timezone.utc) - timedelta(hours=2)
    assert alert.on_cooldown() is False


def test_a_naive_timestamp_does_not_crash_the_cooldown():
    """Postgres can hand back a naive datetime depending on the column type."""
    alert = rule()
    alert.last_fired_at = datetime.now(timezone.utc).replace(tzinfo=None)
    assert alert.on_cooldown() is True


def test_a_rule_that_never_fired_is_not_on_cooldown():
    assert rule().on_cooldown() is False


# ── Recognising a request ─────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "תתריע לי כשמניית ANT עולה ב-3%",
    "תעדכן אותי כשORCL חוצה את ממוצע 150 יום",
    "הודע לי אם TEVA.TA יורדת מתחת ל-1800",
    "notify me when AVGO crosses 400",
])
def test_a_request_is_recognised_as_one(text):
    assert custom_alerts.looks_like_request(text) is True


@pytest.mark.parametrize("text", [
    "AVGO", "תיק", "/status", "מה קורה בשוק", "ניתוח AVGO", "",
])
def test_everything_else_is_not(text):
    assert custom_alerts.looks_like_request(text) is False


# ── Parsing ───────────────────────────────────────────────────────────────────

class Analyst:
    def __init__(self, answer, error=None):
        self.answer, self.error = answer, error
        self.calls = []

    def parse_alert_request(self, system_prompt, request):
        self.calls.append({"system": system_prompt, "request": request})
        if self.error:
            raise self.error
        return self.answer


def _reply(**payload):
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def test_a_parsed_request_becomes_a_validated_rule():
    analyst = Analyst(_reply(symbol="ORCL", kind="ma_cross",
                             params={"direction": "up", "days": 150},
                             channel="telegram",
                             explain="חציית ממוצע 150 יום כלפי מעלה"))
    result = custom_alerts.parse_request(
        "תעדכן אותי כשמניית orcl חוצה כלפי מעלה את ממוצע 150 יום", analyst)

    assert result.ok
    assert result.alert.symbol == "ORCL"
    assert result.alert.kind == "ma_cross"
    assert result.alert.params["days"] == 150
    assert result.explain


def test_the_request_is_stored_with_the_rule():
    """The only record of what the user meant, as opposed to what was parsed."""
    request = "תתריע לי כשANT עולה 3% מהפתיחה"
    analyst = Analyst(_reply(symbol="ANT", kind="pct_from_open",
                             params={"direction": "up", "threshold_pct": 3}))

    assert custom_alerts.parse_request(request, analyst).alert.request == request


def test_a_fenced_json_reply_is_read():
    analyst = Analyst("```json\n" + _reply(
        symbol="AVGO", kind="pct_from_close",
        params={"direction": "down", "threshold_pct": 5}) + "\n```")
    assert custom_alerts.parse_request("תתריע כשAVGO יורדת 5%", analyst).ok


def test_prose_around_the_json_is_tolerated():
    analyst = Analyst("בטח! " + _reply(
        symbol="AVGO", kind="price_level",
        params={"direction": "up", "price": 400}) + " בהצלחה")
    assert custom_alerts.parse_request("תתריע כשAVGO עוברת 400", analyst).ok


def test_a_refusal_from_the_model_is_passed_on_as_the_reason():
    analyst = Analyst(json.dumps(
        {"ok": False, "reason": "לא ציינת אחוז"}, ensure_ascii=False))
    result = custom_alerts.parse_request("תתריע לי כשAVGO עולה", analyst)

    assert not result.ok
    assert result.error == "לא ציינת אחוז"


def test_an_unparseable_reply_does_not_raise():
    result = custom_alerts.parse_request("תתריע לי", Analyst("I'm not sure"))
    assert not result.ok and result.error


def test_a_rule_the_model_got_wrong_is_refused_not_saved():
    """A 900% threshold is a misread request, and a rule that cannot fire."""
    analyst = Analyst(_reply(symbol="AVGO", kind="pct_from_open",
                             params={"direction": "up", "threshold_pct": 900}))
    result = custom_alerts.parse_request("תתריע לי כשAVGO עולה", analyst)

    assert not result.ok
    assert "תקפה" in result.error or "אחוז" in result.error


def test_an_invented_kind_is_refused():
    analyst = Analyst(_reply(symbol="AVGO", kind="rsi_cross",
                             params={"direction": "up", "level": 70}))
    assert not custom_alerts.parse_request("תתריע על RSI", analyst).ok


def test_an_api_failure_becomes_an_actionable_message():
    analyst = Analyst("", error=RuntimeError("credit balance is too low"))
    result = custom_alerts.parse_request("תתריע לי כשAVGO עולה 3%", analyst)

    assert not result.ok
    assert "קרדיט" in result.error


def test_no_analyst_says_what_to_configure():
    result = custom_alerts.parse_request("תתריע לי כשAVGO עולה 3%", None)
    assert "ANTHROPIC_API_KEY" in result.error


def test_an_empty_request_is_refused_without_a_call():
    analyst = Analyst(_reply(symbol="AVGO", kind="pct_from_open"))
    assert not custom_alerts.parse_request("   ", analyst).ok
    assert analyst.calls == []


def test_the_symbol_is_upper_cased():
    analyst = Analyst(_reply(symbol="orcl", kind="pct_from_open",
                             params={"direction": "up", "threshold_pct": 3}))
    assert custom_alerts.parse_request("תתריע כשorcl עולה 3%", analyst).alert.symbol == "ORCL"


def test_the_channel_defaults_to_telegram():
    analyst = Analyst(_reply(symbol="AVGO", kind="pct_from_open",
                             params={"direction": "up", "threshold_pct": 3}))
    assert custom_alerts.parse_request("תתריע כשAVGO עולה 3%", analyst
                                       ).alert.channel == "telegram"


def test_an_email_request_is_carried_through():
    analyst = Analyst(_reply(symbol="AVGO", kind="pct_from_open", channel="email",
                             params={"direction": "up", "threshold_pct": 3}))
    assert custom_alerts.parse_request("תתריע לי במייל כשAVGO עולה 3%", analyst
                                       ).alert.channel == "email"


def test_the_prompt_lists_only_the_kinds_the_engine_can_evaluate():
    """A kind the model invents cannot be evaluated, so none are left implied."""
    analyst = Analyst(_reply(symbol="AVGO", kind="pct_from_open",
                             params={"direction": "up", "threshold_pct": 3}))
    custom_alerts.parse_request("תתריע כשAVGO עולה 3%", analyst)
    system = analyst.calls[0]["system"]

    for kind in custom_alerts.KINDS:
        assert kind in system
    assert "אל תנחש מספרים" in system


# ── The store ─────────────────────────────────────────────────────────────────

def test_a_saved_rule_comes_back():
    saved = custom_alert_store.add(rule())
    assert custom_alert_store.get(saved.id).symbol == "AVGO"


def test_saving_validates():
    with pytest.raises(RuleError):
        custom_alert_store.add(CustomAlert(symbol="AVGO", kind="nonsense"))


def test_in_memory_ids_are_negative_and_distinct():
    """So they cannot collide with database ids, which key the crossing state."""
    first = custom_alert_store.add(rule())
    second = custom_alert_store.add(rule(symbol="NVDA"))

    assert first.id < 0 and second.id < 0 and first.id != second.id


def test_the_symbols_to_poll_are_listed():
    custom_alert_store.add(rule(symbol="ANT"))
    custom_alert_store.add(rule(symbol="ORCL"))
    assert custom_alert_store.symbols() == ["ANT", "ORCL"]


def test_a_disabled_rule_is_not_polled_for():
    saved = custom_alert_store.add(rule(symbol="ANT"))
    custom_alert_store.set_enabled(saved.id, False)

    assert custom_alert_store.symbols() == []
    assert len(custom_alert_store.all()) == 1, "but it is still listed"


def test_removing_a_rule_removes_it():
    saved = custom_alert_store.add(rule())
    assert custom_alert_store.remove(saved.id) is True
    assert custom_alert_store.get(saved.id) is None


def test_removing_something_absent_says_so():
    assert custom_alert_store.remove(-999) is False


def test_firing_is_recorded_so_the_cooldown_can_start():
    saved = custom_alert_store.add(rule())
    custom_alert_store.mark_fired(saved.id)

    assert custom_alert_store.get(saved.id).on_cooldown() is True


def test_a_one_time_rule_can_be_disabled_as_it_fires():
    saved = custom_alert_store.add(rule())
    custom_alert_store.mark_fired(saved.id, disable=True)

    assert custom_alert_store.get(saved.id).enabled is False


def test_a_rule_can_be_re_armed():
    saved = custom_alert_store.add(rule())
    custom_alert_store.set_enabled(saved.id, False)
    custom_alert_store.set_enabled(saved.id, True)

    assert custom_alert_store.get(saved.id).enabled is True


def test_params_survive_the_round_trip():
    saved = custom_alert_store.add(rule("ma_cross", days=200))
    assert custom_alert_store.get(saved.id).params == {"direction": "up", "days": 200}


# ── The message that goes out ─────────────────────────────────────────────────

def test_the_telegram_message_names_the_stock_and_the_request():
    alert = rule()
    alert.request = "תתריע לי כשAVGO עולה 3% מהפתיחה"
    text = strip_bidi(custom_alerts.alert_text(alert, "עלתה 3.2% מהפתיחה"))

    assert "AVGO" in text and "עלתה 3.2%" in text
    assert alert.request in text


def test_the_telegram_message_says_when_a_one_time_rule_switched_off():
    alert = rule()
    alert.once = True
    assert "חד-פעמית" in strip_bidi(custom_alerts.alert_text(alert, "עלתה 3.2%"))


def test_the_email_has_a_subject_and_a_plain_body():
    alert = rule()
    alert.request = "תתריע לי כשAVGO עולה 3%"
    subject, body = custom_alerts.email_text(alert, messages.rtl("עלתה 3.2%"))

    assert "AVGO" in subject
    assert "עלתה 3.2%" in body
    assert messages.RLM not in body, "no direction marks in a mail client"
    assert "⁦" not in body
