"""Muting alerts: the deadlines, and what the dispatcher does about them.

The design under test is that a mute is a deadline rather than a switch, so no
mute can be forgotten in the on position, and that a muted alert still reaches
the log and the alert history — you lose the buzz, not the record.
"""

from datetime import datetime, timedelta, timezone

import pytest

from stock_monitor import mute
from stock_monitor.alert_engine import AlertEvent
from stock_monitor.config import NotificationConfig
from stock_monitor.notifier import NotificationDispatcher


@pytest.fixture(autouse=True)
def clean():
    mute.unmute_everything()
    yield
    mute.unmute_everything()


def event(symbol="AVGO"):
    return AlertEvent(symbol=symbol, alert_type="price_change_pct", message="moved",
                      price=100.0, timestamp=datetime.now(), severity="WARNING",
                      details={"change_pct": 3.0, "window_minutes": 5,
                               "from_price": 97.0, "to_price": 100.0})


# ── The registry ──────────────────────────────────────────────────────────────

def test_nothing_is_muted_to_begin_with():
    assert mute.is_muted() is False
    assert mute.is_muted("AVGO") is False
    assert mute.active() == {}


def test_muting_one_symbol_silences_only_that_symbol():
    mute.mute("AVGO", 60)

    assert mute.is_muted("AVGO") is True
    assert mute.is_muted("NVDA") is False
    assert mute.is_muted() is False, "one stock is not the whole channel"


def test_muting_everything_silences_every_symbol():
    mute.mute(mute.ALL, 60)

    assert mute.is_muted() is True
    assert mute.is_muted("ANYTHING") is True


def test_a_mute_expires_on_its_own():
    """The property that matters: no mute outlives the time it was given."""
    mute.mute_until(datetime.now(timezone.utc) - timedelta(seconds=1), "AVGO")
    assert mute.is_muted("AVGO") is False


def test_an_expired_mute_is_pruned_rather_than_kept_forever():
    mute.mute_until(datetime.now(timezone.utc) - timedelta(seconds=1), "AVGO")
    mute.is_muted("AVGO")
    assert "AVGO" not in mute.active()


def test_the_deadline_is_returned_so_the_reply_can_name_it():
    deadline = mute.mute("AVGO", 30)
    expected = datetime.now(timezone.utc) + timedelta(minutes=30)

    assert abs((deadline - expected).total_seconds()) < 5


def test_a_naive_deadline_is_read_as_utc_rather_than_crashing():
    mute.mute_until(datetime.now() + timedelta(hours=5), "AVGO")
    assert mute.muted_until("AVGO") is not None


def test_symbols_are_matched_case_insensitively():
    mute.mute("avgo", 60)
    assert mute.is_muted("AVGO") is True


def test_a_zero_or_negative_duration_still_mutes_briefly():
    """A button that sends 0 should not silently do nothing at all."""
    assert mute.mute("AVGO", 0) > datetime.now(timezone.utc)


def test_unmuting_a_symbol_leaves_the_global_mute_alone():
    mute.mute(mute.ALL, 60)
    mute.mute("AVGO", 60)
    mute.unmute("AVGO")

    assert mute.is_muted("AVGO") is True, "the global mute still covers it"
    assert mute.is_muted() is True


def test_unmuting_everything_clears_every_mute():
    mute.mute(mute.ALL, 60)
    mute.mute("AVGO", 60)
    mute.unmute_everything()

    assert mute.active() == {}
    assert mute.is_muted("AVGO") is False


def test_active_lists_each_mute_with_its_deadline():
    mute.mute("AVGO", 120)
    mute.mute("NVDA", 10)
    active = mute.active()

    assert set(active) == {"AVGO", "NVDA"}
    assert list(active) == ["NVDA", "AVGO"], "soonest to expire first"


def test_muting_the_same_symbol_again_extends_it():
    first = mute.mute("AVGO", 10)
    second = mute.mute("AVGO", 120)

    assert second > first
    assert mute.muted_until("AVGO") == second


# ── What the dispatcher does with it ──────────────────────────────────────────

class _Telegram:
    def __init__(self):
        self.sent = []

    def send(self, text, reply_markup=None):
        self.sent.append(text)
        return True


@pytest.fixture
def dispatcher():
    inst = NotificationDispatcher(NotificationConfig())
    telegram = _Telegram()
    inst._telegram, inst._discord, inst._desktop = telegram, None, None
    return inst, telegram


def test_a_muted_symbol_sends_nothing(dispatcher):
    inst, telegram = dispatcher
    mute.mute("AVGO", 60)
    inst.dispatch(event("AVGO"))

    assert telegram.sent == []


def test_other_symbols_still_come_through(dispatcher):
    inst, telegram = dispatcher
    mute.mute("AVGO", 60)
    inst.dispatch(event("NVDA"))

    assert len(telegram.sent) == 1


def test_a_global_mute_stops_everything(dispatcher):
    inst, telegram = dispatcher
    mute.mute(mute.ALL, 60)
    inst.dispatch(event("NVDA"))
    inst.dispatch_raw("TradingView", "something happened")

    assert telegram.sent == []


def test_a_per_symbol_mute_does_not_stop_webhook_alerts(dispatcher):
    """Those carry no symbol to match, so muting one stock must not eat them."""
    inst, telegram = dispatcher
    mute.mute("AVGO", 60)
    inst.dispatch_raw("TradingView", "AAPL broke out")

    assert len(telegram.sent) == 1


def test_the_alert_is_still_logged_while_muted(dispatcher, caplog):
    """A quiet hour must not become an hour with no record of what happened."""
    inst, _ = dispatcher
    mute.mute("AVGO", 60)
    with caplog.at_level("INFO"):
        inst.dispatch(event("AVGO"))

    assert any("ALERT ▶" in record.getMessage() for record in caplog.records)
    assert any("muted until" in record.getMessage() for record in caplog.records), \
        "and the log says why nothing was sent"


def test_alerts_resume_once_the_mute_expires(dispatcher):
    inst, telegram = dispatcher
    mute.mute_until(datetime.now(timezone.utc) + timedelta(milliseconds=1), "AVGO")
    import time
    time.sleep(0.01)
    inst.dispatch(event("AVGO"))

    assert len(telegram.sent) == 1
