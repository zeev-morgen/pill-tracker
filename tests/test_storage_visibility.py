"""What the app shows when it cannot read the database.

Losing persistence is deliberately silent: the monitor keeps polling and
alerting without it. The cost is that the dashboard then renders an empty
portfolio and a watchlist rebuilt from config.yaml — which is indistinguishable
from having lost everything, and invites the user to re-enter positions that
are sitting safely in a database the process simply cannot reach.

These tests are about making the two states tell themselves apart.
"""

import pytest
from fastapi.testclient import TestClient

from stock_monitor import dashboard, db
from stock_monitor.config import NotificationConfig
from stock_monitor.notifier import NotificationDispatcher
from stock_monitor.store import WatchlistStore
from stock_monitor.webhook_server import create_webhook_app


@pytest.fixture
def client():
    return TestClient(
        create_webhook_app(NotificationDispatcher(NotificationConfig()), "")
    )


@pytest.fixture(autouse=True)
def clean_db_state(monkeypatch):
    monkeypatch.setattr(db, "_last_error", None)
    yield


# ── db.status() ───────────────────────────────────────────────────────────────

def test_an_unset_url_is_reported_as_such(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "_SessionFactory", None)

    status = db.status()
    assert status["enabled"] is False
    assert status["url_configured"] is False


def test_a_configured_but_failing_database_is_distinguishable(monkeypatch):
    """"Not set up" and "set up but broken" need completely different fixes."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setattr(db, "_SessionFactory", None)
    monkeypatch.setattr(db, "_last_error", "OperationalError: password auth failed")

    status = db.status()
    assert status["enabled"] is False
    assert status["url_configured"] is True
    assert "OperationalError" in status["error"]


def test_the_failure_reason_is_recorded_by_init(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@nonexistent.invalid/db")
    monkeypatch.setattr(
        db, "create_engine",
        lambda *a, **k: (_ for _ in ()).throw(OSError("could not connect")),
    )
    monkeypatch.setattr(db, "_SessionFactory", None)

    assert db.init_db() is False
    assert "OSError" in db.status()["error"]


def test_the_status_never_carries_the_connection_string(monkeypatch):
    """The credentials in it are a common reason it fails in the first place."""
    secret = "postgresql://admin:hunter2@db.example.com/prod"
    monkeypatch.setenv("DATABASE_URL", secret)
    monkeypatch.setattr(
        db, "create_engine",
        lambda *a, **k: (_ for _ in ()).throw(OSError(f"cannot reach {secret}")),
    )
    monkeypatch.setattr(db, "_SessionFactory", None)
    db.init_db()

    serialized = str(db.status())
    assert "hunter2" not in serialized
    assert "admin" not in serialized


def test_an_unset_url_records_that_as_the_reason(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert db.init_db() is False
    assert "DATABASE_URL" in db.status()["error"]


# ── The seed guard ────────────────────────────────────────────────────────────

def test_the_watchlist_is_not_reseeded_when_the_database_is_unreachable(monkeypatch):
    """The reported symptom: a curated watchlist replaced by config.yaml.

    An unreadable store returns empty, seed() reads that as "first run", and
    tickers removed months ago come back.
    """
    monkeypatch.setattr(db, "status", lambda: {"url_configured": True, "enabled": False})
    monkeypatch.setattr(db, "is_enabled", lambda: False)
    store = WatchlistStore()

    store.seed(["OLD1", "OLD2"])
    assert store.all() == [], "config symbols must not resurface"


def test_a_genuine_first_run_still_seeds(monkeypatch):
    """No database configured at all — memory is the source of truth."""
    monkeypatch.setattr(db, "status", lambda: {"url_configured": False, "enabled": False})
    monkeypatch.setattr(db, "is_enabled", lambda: False)
    store = WatchlistStore()

    store.seed(["AAPL", "MSFT"])
    assert store.all() == ["AAPL", "MSFT"]


def test_a_working_database_still_seeds_on_first_run(monkeypatch):
    monkeypatch.setattr(db, "status", lambda: {"url_configured": True, "enabled": True})
    monkeypatch.setattr(db, "is_enabled", lambda: False)   # no writes in this test
    store = WatchlistStore()

    store.seed(["AAPL"])
    assert store.all() == ["AAPL"]


def test_seeding_never_overwrites_an_existing_watchlist(monkeypatch):
    monkeypatch.setattr(db, "status", lambda: {"url_configured": False, "enabled": False})
    monkeypatch.setattr(db, "is_enabled", lambda: False)
    store = WatchlistStore()
    store.add("NVDA")

    store.seed(["AAPL", "MSFT"])
    assert store.all() == ["NVDA"]


# ── The HTTP surface ──────────────────────────────────────────────────────────

def test_the_storage_endpoint_reports_the_counts(client, monkeypatch):
    monkeypatch.setattr(db, "status", lambda: {
        "enabled": True, "url_configured": True, "error": None})

    body = client.get("/api/storage").json()
    assert body["enabled"] is True
    assert "holdings" in body and "watchlist" in body


def test_the_storage_endpoint_says_the_data_is_not_lost(client, monkeypatch):
    """The panic this is built to prevent."""
    monkeypatch.setattr(db, "status", lambda: {
        "enabled": False, "url_configured": True, "error": "OperationalError: nope"})

    body = client.get("/api/storage").json()
    assert body["enabled"] is False
    assert "עדיין קיימים" in body["note"]


def test_the_portfolio_report_carries_the_storage_state(client, monkeypatch):
    """An empty table has to be able to explain which kind of empty it is."""
    monkeypatch.setattr(dashboard, "get_data_feed", lambda: None)
    monkeypatch.setattr(
        dashboard._risk_analyzer, "full_report",
        lambda: {"positions": [], "total_value": 0, "total_pnl_value": 0},
    )
    monkeypatch.setattr(db, "is_enabled", lambda: False)

    assert client.get("/api/portfolio").json()["storage_ok"] is False


def test_a_healthy_database_reports_storage_ok(client, monkeypatch):
    monkeypatch.setattr(dashboard, "get_data_feed", lambda: None)
    monkeypatch.setattr(
        dashboard._risk_analyzer, "full_report",
        lambda: {"positions": [], "total_value": 0, "total_pnl_value": 0},
    )
    monkeypatch.setattr(db, "is_enabled", lambda: True)

    assert client.get("/api/portfolio").json()["storage_ok"] is True


def test_the_storage_endpoint_is_not_public():
    from stock_monitor.webhook_server import _PUBLIC_PATHS

    assert not any("/api/storage".startswith(p) for p in _PUBLIC_PATHS)


# ── The banner ────────────────────────────────────────────────────────────────

def test_an_unreadable_portfolio_does_not_render_as_an_empty_one():
    from stock_monitor.dashboard import _HTML

    body = _HTML.split("function renderHoldings(", 1)[1].split("\n}", 1)[0]
    assert "storage_ok === false" in body
    assert "לא אבדו" in body, "the user must be told the data still exists"
    assert "אל תזינו מחדש" in body, "re-entering would overwrite the saved rows"


# ── Redaction ─────────────────────────────────────────────────────────────────

def test_a_password_never_survives_redaction(monkeypatch):
    monkeypatch.setenv("DATABASE_URL",
                       "postgresql://owner:npg_SECRET123@ep-x.neon.tech/neondb")
    for message in (
        "could not connect to postgresql://owner:npg_SECRET123@ep-x.neon.tech/neondb",
        "OSError: cannot reach postgresql+psycopg://admin:hunter2@db.example.com/prod",
    ):
        assert "SECRET123" not in db._redact(message)
        assert "hunter2" not in db._redact(message)


def test_the_useful_part_of_an_auth_error_is_kept(monkeypatch):
    """"password authentication failed" is the diagnosis and holds no secret.

    Redacting it to nothing would leave the user with a broken database and no
    idea which of a dozen causes it was.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner:pw@ep-x.neon.tech/neondb")
    message = (
        'OperationalError: connection to server at "ep-x.neon.tech" failed: '
        'password authentication failed for user "owner"'
    )
    assert "password authentication failed" in db._redact(message)


def test_redaction_leaves_an_ordinary_message_alone(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert db._redact("TimeoutError: timed out") == "TimeoutError: timed out"
