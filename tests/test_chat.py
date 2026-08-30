"""The portfolio conversation.

Two things carry the weight here. The snapshot has to describe the portfolio
that actually exists — including the parts that are missing or stale, because
confident advice about a total that quietly excludes two holdings is worse than
no advice. And the history has to stay a valid conversation across trimming and
failures: a dangling question with no answer gets carried into the next turn
and answered late, out of context.
"""

import json

import pytest
from fastapi.testclient import TestClient

from stock_monitor import chat, dashboard
from stock_monitor.chat import PortfolioChat, build_snapshot
from stock_monitor.config import NotificationConfig
from stock_monitor.notifier import NotificationDispatcher
from stock_monitor.webhook_server import create_webhook_app

REPORT = {
    "positions": [
        {"ticker": "AVGO", "quantity": 9.76, "entry_price": 380.0,
         "current_price": 418.28, "market_value": 4082.41, "pnl_pct": 10.07,
         "pnl_value": 373.61, "atr_pct": 4.09, "sector": "Technology",
         "currency": "", "holding_days": 45, "price_is_stale": False},
        {"ticker": "POLI.TA", "quantity": 10, "entry_price": 3000.0,
         "current_price": 3450.0, "market_value": 95.83, "pnl_pct": 15.0,
         "pnl_value": 12.5, "atr_pct": 2.1, "sector": "Financial Services",
         "currency": "ILA", "holding_days": None, "price_is_stale": True},
    ],
    "total_value": 4178.24,
    "total_pnl_value": 386.11,
    "skipped_tickers": ["ARYT"],
    "skip_reason": "no price data",
    "feed_lag_days": 0,
    "latest_bar_date": "2026-08-30",
    "has_foreign": True,
    "fx_rate": 3.6,
    "volatility": {"exposure_pct": 42.7, "threshold_pct": 30.0},
    "sector": {"threshold_pct": 40.0, "alert": True,
               "sectors": [{"sector": "Technology", "weight_pct": 97.7},
                           {"sector": "Financial Services", "weight_pct": 2.3}],
               "concentrated_sectors": [{"sector": "Technology"}]},
}

JOURNAL = {
    "entries": [{"ticker": "CF", "pnl_pct": 22.67, "holding_days": 90,
                 "rating": "green"}],
    "summary": {"count": 1, "total_pnl": 136.0, "win_rate_pct": 100.0},
}


# ── The snapshot ──────────────────────────────────────────────────────────────

def test_the_snapshot_names_every_position():
    text = build_snapshot(REPORT)
    assert "AVGO" in text and "POLI.TA" in text


def test_an_empty_portfolio_says_so_rather_than_producing_a_blank():
    assert "ריק" in build_snapshot({"positions": []})


def test_positions_carry_their_weight_in_the_portfolio():
    """Without it the model treats a 2% position like a 40% one."""
    text = build_snapshot(REPORT)
    assert "97.7% מהתיק" in text or "97.7%" in text


def test_a_tel_aviv_price_is_labelled_agorot_not_dollars():
    """3,450 agorot next to a $ would read as a position worth 360x its value."""
    line = [l for l in build_snapshot(REPORT).splitlines() if "POLI.TA" in l][0]
    assert "אג׳" in line
    assert "$3,450" not in line


def test_a_us_price_keeps_its_dollar_sign():
    line = [l for l in build_snapshot(REPORT).splitlines() if "AVGO" in l][0]
    assert "$418.28" in line


def test_unpriceable_holdings_are_disclosed():
    """The totals exclude them, so advice based on the totals must know."""
    text = build_snapshot(REPORT)
    assert "ARYT" in text
    assert "no price data" in text


def test_a_stale_price_is_flagged_on_the_position():
    line = [l for l in build_snapshot(REPORT).splitlines() if "POLI.TA" in l][0]
    assert "מסשן קודם" in line


def test_a_lagging_feed_is_stated_before_anything_else():
    text = build_snapshot(dict(REPORT, feed_lag_days=2))
    assert "אינם מעודכנים" in text
    assert "2 ימי מסחר" in text


def test_a_current_feed_raises_no_warning():
    assert "אינם מעודכנים" not in build_snapshot(REPORT)


def test_the_exchange_rate_is_stated_when_it_is_applied():
    assert "3.600" in build_snapshot(REPORT)


def test_sector_concentration_is_included():
    text = build_snapshot(REPORT)
    assert "ריכוזיות" in text and "Technology" in text


def test_the_journal_is_included_when_given():
    text = build_snapshot(REPORT, JOURNAL)
    assert "CF" in text and "עסקאות שנסגרו" in text


def test_the_snapshot_works_without_a_journal():
    assert "עסקאות שנסגרו" not in build_snapshot(REPORT)


def test_a_report_missing_optional_sections_does_not_raise():
    """full_report can come back thin when the data source is failing."""
    minimal = {"positions": [dict(REPORT["positions"][0])], "total_value": 4082.41}
    assert "AVGO" in build_snapshot(minimal)


# ── Conversation mechanics ────────────────────────────────────────────────────

class FakeStream:
    def __init__(self, fragments):
        self._fragments = fragments

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @property
    def text_stream(self):
        for fragment in self._fragments:
            if isinstance(fragment, Exception):
                raise fragment
            yield fragment


@pytest.fixture
def conversation(monkeypatch):
    """A PortfolioChat whose model is replaced by a recorder."""
    calls = []
    state = {"fragments": ["שלום", " עולם"]}

    class FakeMessages:
        def stream(self, **kwargs):
            calls.append(kwargs)
            return FakeStream(state["fragments"])

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(chat.anthropic, "Anthropic", lambda **kw: FakeClient())
    instance = PortfolioChat(api_key="test-key")
    instance.calls = calls
    instance.state = state
    return instance


def _drain(instance, message, snapshot="מצב התיק"):
    return "".join(instance.stream_reply(message, snapshot))


def test_a_reply_streams_back_in_fragments(conversation):
    fragments = list(conversation.stream_reply("מה מצב התיק?", "מצב"))
    assert fragments == ["שלום", " עולם"]


def test_both_turns_are_kept_in_the_history(conversation):
    _drain(conversation, "מה מצב התיק?")
    assert conversation.history() == [
        {"role": "user", "content": "מה מצב התיק?"},
        {"role": "assistant", "content": "שלום עולם"},
    ]


def test_the_conversation_carries_forward(conversation):
    _drain(conversation, "שאלה ראשונה")
    _drain(conversation, "ושאלה שנייה")

    sent = conversation.calls[-1]["messages"]
    assert [m["content"] for m in sent if m["role"] == "user"] == [
        "שאלה ראשונה", "ושאלה שנייה",
    ]


def test_an_empty_message_is_rejected(conversation):
    with pytest.raises(ValueError):
        list(conversation.stream_reply("   ", "מצב"))
    assert conversation.history() == []


# ── The snapshot channel ──────────────────────────────────────────────────────

def test_the_snapshot_is_sent_as_a_system_message(conversation):
    """It is operator data, not something the user typed."""
    _drain(conversation, "שאלה", "תמונת מצב כאן")

    sent = conversation.calls[-1]["messages"]
    assert sent[-1] == {"role": "system", "content": "תמונת מצב כאן"}


def test_the_snapshot_follows_the_users_turn(conversation):
    """The API rejects a system message that is not preceded by a user turn."""
    _drain(conversation, "שאלה", "מצב")

    sent = conversation.calls[-1]["messages"]
    assert sent[-2]["role"] == "user"


def test_the_snapshot_is_not_stored_in_the_history(conversation):
    """Keeping past snapshots would leave several contradictory portfolios in
    the context, all of them paid for, most of them wrong."""
    _drain(conversation, "שאלה ראשונה", "תיק בשווי 100")
    _drain(conversation, "שאלה שנייה", "תיק בשווי 200")

    assert not any(m["role"] == "system" for m in conversation.history())
    sent = conversation.calls[-1]["messages"]
    snapshots = [m for m in sent if m["role"] == "system"]
    assert len(snapshots) == 1, "only the current portfolio may be in the request"
    assert snapshots[0]["content"] == "תיק בשווי 200"


def test_an_older_model_folds_the_snapshot_into_the_user_turn(conversation):
    """Mid-conversation system messages are model-gated; a model without them
    would reject the request outright rather than degrade."""
    conversation._model = "claude-opus-4-7"
    _drain(conversation, "שאלה", "תמונת מצב")

    sent = conversation.calls[-1]["messages"]
    assert not any(m["role"] == "system" for m in sent)
    assert "שאלה" in sent[-1]["content"] and "תמונת מצב" in sent[-1]["content"]


def test_the_default_model_supports_the_system_channel():
    assert chat.DEFAULT_MODEL in chat.SNAPSHOT_AS_SYSTEM_MODELS


def test_no_snapshot_sends_no_system_message(conversation):
    _drain(conversation, "שאלה", "")
    assert not any(m["role"] == "system" for m in conversation.calls[-1]["messages"])


# ── Failure handling ──────────────────────────────────────────────────────────

def test_a_failed_turn_does_not_leave_the_question_in_the_history(conversation):
    """Carried forward, it gets answered on the next message, out of context."""
    conversation.state["fragments"] = [RuntimeError("boom")]

    with pytest.raises(RuntimeError):
        _drain(conversation, "שאלה שנכשלת")
    assert conversation.history() == []


def test_an_empty_reply_is_not_recorded_as_an_answer(conversation):
    conversation.state["fragments"] = []
    _drain(conversation, "שאלה")

    assert conversation.history() == []


def test_a_partial_reply_before_a_failure_is_not_silently_kept(conversation):
    conversation.state["fragments"] = ["חלק ראשון", RuntimeError("cut")]

    with pytest.raises(RuntimeError):
        _drain(conversation, "שאלה")
    assert conversation.history() == []


# ── Trimming ──────────────────────────────────────────────────────────────────

def test_the_history_is_capped(conversation, monkeypatch):
    monkeypatch.setattr(chat, "MAX_TURNS", 4)
    for i in range(6):
        _drain(conversation, f"שאלה {i}")

    assert len(conversation.history()) <= 4


def test_trimming_leaves_the_history_starting_on_a_user_turn(conversation, monkeypatch):
    """The API rejects a conversation whose first message is from the assistant."""
    monkeypatch.setattr(chat, "MAX_TURNS", 3)
    for i in range(6):
        _drain(conversation, f"שאלה {i}")

    history = conversation.history()
    assert history[0]["role"] == "user", history


def test_clearing_empties_the_conversation(conversation):
    _drain(conversation, "שאלה")
    conversation.clear()
    assert conversation.history() == []


# ── The request itself ────────────────────────────────────────────────────────

def test_the_system_prompt_is_cached(conversation):
    """It is fixed for the life of the process and resent on every message."""
    _drain(conversation, "שאלה")
    system = conversation.calls[-1]["system"]

    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_thinking_is_adaptive(conversation):
    _drain(conversation, "שאלה")
    assert conversation.calls[-1]["thinking"] == {"type": "adaptive"}


def test_the_reply_is_streamed_not_awaited_whole(conversation):
    """A blank box for half a minute reads as a broken feature."""
    import inspect

    assert inspect.isgeneratorfunction(PortfolioChat.stream_reply)


# ── The HTTP surface ──────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard, "_chat", None)
    return TestClient(
        create_webhook_app(NotificationDispatcher(NotificationConfig()), "")
    )


def test_the_chat_reports_itself_disabled_without_a_key(client):
    body = client.get("/api/chat").json()
    assert body["enabled"] is False
    assert body["messages"] == []


def test_sending_without_a_key_is_refused(client):
    assert client.post("/api/chat", json={"message": "שלום"}).status_code == 503


def test_an_empty_message_is_refused(client, conversation, monkeypatch):
    monkeypatch.setattr(dashboard, "_chat", conversation)
    assert client.post("/api/chat", json={"message": "  "}).status_code == 422


def test_an_overlong_message_is_refused(client, conversation, monkeypatch):
    monkeypatch.setattr(dashboard, "_chat", conversation)
    response = client.post("/api/chat", json={"message": "א" * 4001})
    assert response.status_code == 422


def test_the_reply_arrives_as_server_sent_events(client, conversation, monkeypatch):
    monkeypatch.setattr(dashboard, "_chat", conversation)
    monkeypatch.setattr(dashboard, "_chat_snapshot", lambda: "מצב")

    response = client.post("/api/chat", json={"message": "שאלה"})
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]

    frames = [
        json.loads(line[6:]) for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert "".join(f.get("text", "") for f in frames) == "שלום עולם"
    assert frames[-1] == {"done": True}


def test_a_mid_stream_failure_is_reported_inside_the_stream(client, conversation, monkeypatch):
    """The status code is already sent by then; the error has to ride the body."""
    monkeypatch.setattr(dashboard, "_chat", conversation)
    monkeypatch.setattr(dashboard, "_chat_snapshot", lambda: "מצב")
    conversation.state["fragments"] = [RuntimeError("boom")]

    response = client.post("/api/chat", json={"message": "שאלה"})
    frames = [
        json.loads(line[6:]) for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert any("error" in f for f in frames)
    assert frames[-1] == {"done": True}, "the client must not be left waiting"


def test_hebrew_survives_the_wire(client, conversation, monkeypatch):
    """ensure_ascii would ship \\u05e9 escapes and render as mojibake."""
    monkeypatch.setattr(dashboard, "_chat", conversation)
    monkeypatch.setattr(dashboard, "_chat_snapshot", lambda: "מצב")

    response = client.post("/api/chat", json={"message": "שאלה"})
    assert "שלום" in response.text


def test_the_history_is_served_back(client, conversation, monkeypatch):
    monkeypatch.setattr(dashboard, "_chat", conversation)
    _drain(conversation, "שאלה")

    body = client.get("/api/chat").json()
    assert body["enabled"] is True
    assert body["messages"][0] == {"role": "user", "content": "שאלה"}


def test_the_conversation_can_be_cleared_over_http(client, conversation, monkeypatch):
    monkeypatch.setattr(dashboard, "_chat", conversation)
    _drain(conversation, "שאלה")

    assert client.delete("/api/chat").json() == {"messages": []}
    assert conversation.history() == []


def test_a_broken_snapshot_does_not_block_the_conversation(monkeypatch):
    """A failed price read is a reason to say so, not to refuse to talk."""
    monkeypatch.setattr(
        dashboard._risk_analyzer, "full_report",
        lambda: (_ for _ in ()).throw(RuntimeError("Yahoo down")),
    )
    text = dashboard._chat_snapshot()
    assert "RuntimeError" in text
    assert "אל תסיק מסקנות" in text


def test_the_chat_is_not_public():
    """It reads the portfolio and spends tokens."""
    from stock_monitor.webhook_server import _PUBLIC_PATHS

    assert not any("/api/chat".startswith(p) for p in _PUBLIC_PATHS)


def test_a_single_lagging_day_reads_as_singular_hebrew():
    """"ב-1 ימי מסחר" is not a sentence."""
    text = build_snapshot(dict(REPORT, feed_lag_days=1))
    assert "ביום מסחר אחד" in text
    assert "ב-1 ימי" not in text


def test_agorot_is_written_after_the_number():
    """A unit follows the figure; a currency symbol precedes it."""
    line = [l for l in build_snapshot(REPORT).splitlines() if "POLI.TA" in l][0]
    assert "3,450.00 אג׳" in line
    assert "אג׳3,450" not in line
