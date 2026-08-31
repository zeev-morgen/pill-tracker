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


# ── IPv6-only DNS on an IPv4-only host ────────────────────────────────────────

# The error verbatim from the deployed instance. Neon published an AAAA record;
# the container has no IPv6 egress, so the connection died at the network layer
# before any credential was exchanged — and the dashboard rendered it as an
# empty portfolio.
IPV6_FAILURE = (
    '(psycopg.OperationalError) connection is bad: connection to server at '
    '"2a05:d014:c19:402c:535e:9fff:269d:87fa", port 5432 failed: '
    'Network is unreachable'
)


def test_the_reported_failure_is_recognised_as_a_routing_problem():
    assert db._is_unroutable(Exception(IPV6_FAILURE))


@pytest.mark.parametrize("message", [
    "no route to host",
    "Cannot assign requested address",
    "NETWORK IS UNREACHABLE",
])
def test_other_routing_failures_are_recognised(message):
    assert db._is_unroutable(Exception(message))


@pytest.mark.parametrize("message", [
    'password authentication failed for user "owner"',
    "database does not exist",
    "SSL connection has been closed unexpectedly",
    "timeout expired",
])
def test_a_non_routing_failure_is_not_retried(message):
    """Retrying a rejected login over IPv4 just fails twice as slowly."""
    assert not db._is_unroutable(Exception(message))


def test_the_ipv4_address_is_resolved_from_the_url(monkeypatch):
    monkeypatch.setattr(db.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("52.1.2.3", 5432))])

    assert db._ipv4_address("postgresql://u:p@db.example.com/x") == "52.1.2.3"


def test_a_host_with_no_ipv4_record_resolves_to_nothing(monkeypatch):
    def no_a_record(*args, **kwargs):
        raise db.socket.gaierror("no address associated with hostname")

    monkeypatch.setattr(db.socket, "getaddrinfo", no_a_record)
    assert db._ipv4_address("postgresql://u:p@v6only.example.com/x") is None


def test_a_malformed_url_resolves_to_nothing_rather_than_raising():
    assert db._ipv4_address("not a url at all") is None


def test_the_connection_is_pinned_by_hostaddr_not_by_swapping_the_host(monkeypatch):
    """TLS still needs the name: managed providers route on SNI, and a bare IP
    would reach the wrong project or fail certificate verification."""
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured["url"] = str(url)
        captured["connect_args"] = kwargs.get("connect_args")
        return object()

    monkeypatch.setattr(db, "create_engine", fake_create_engine)
    db._build_engine("postgresql://u:p@db.example.com/x", "52.1.2.3")

    assert captured["connect_args"] == {"hostaddr": "52.1.2.3"}
    assert "db.example.com" in captured["url"], "the hostname must survive for SNI"


def test_a_routing_failure_is_retried_over_ipv4(monkeypatch):
    """The whole point: persistence comes back without the user touching Render."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example.com/x")
    monkeypatch.setattr(db, "_ipv4_address", lambda url: "52.1.2.3")
    monkeypatch.setattr(db, "_add_missing_columns", lambda: None)
    monkeypatch.setattr(db, "_SessionFactory", None)
    attempts = []

    def fake_build(url, ipv4=None):
        attempts.append(ipv4)
        if ipv4 is None:
            raise OSError(IPV6_FAILURE)
        return object()

    monkeypatch.setattr(db, "_build_engine", fake_build)
    monkeypatch.setattr(db.Base.metadata, "create_all", lambda engine: None)
    monkeypatch.setattr(db, "sessionmaker", lambda **kwargs: object())

    assert db.init_db() is True
    assert attempts == [None, "52.1.2.3"], "one direct attempt, then one pinned"
    assert db.status()["error"] is None


def test_an_auth_failure_is_not_retried(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example.com/x")
    monkeypatch.setattr(
        db, "_ipv4_address", lambda url: pytest.fail("must not resolve"))
    monkeypatch.setattr(db, "_SessionFactory", None)
    attempts = []

    def fake_build(url, ipv4=None):
        attempts.append(ipv4)
        raise OSError("password authentication failed")

    monkeypatch.setattr(db, "_build_engine", fake_build)

    assert db.init_db() is False
    assert attempts == [None], "exactly one attempt"


def test_a_routing_failure_with_no_ipv4_available_gives_up_cleanly(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@v6only.example.com/x")
    monkeypatch.setattr(db, "_ipv4_address", lambda url: None)
    monkeypatch.setattr(db, "_SessionFactory", None)

    def fake_build(url, ipv4=None):
        raise OSError(IPV6_FAILURE)

    monkeypatch.setattr(db, "_build_engine", fake_build)

    assert db.init_db() is False
    assert "unreachable" in db.status()["error"].lower()


# ── Read caching, and why it exists ───────────────────────────────────────────

def test_the_cache_starts_cold():
    """Nothing may be served before a successful read has actually happened."""
    from stock_monitor.store import _cache_is_fresh

    assert _cache_is_fresh(0.0) is False


def test_a_fresh_stamp_is_reused(monkeypatch):
    from stock_monitor import store

    monkeypatch.setattr(store.time, "monotonic", lambda: 1000.0)
    assert store._cache_is_fresh(999.0) is True


def test_an_expired_stamp_is_not_reused(monkeypatch):
    from stock_monitor import store

    monkeypatch.setattr(store.time, "monotonic", lambda: 1000.0 + store.READ_CACHE_TTL + 1)
    assert store._cache_is_fresh(1000.0) is False


def test_the_polling_loop_does_not_query_on_every_cycle(monkeypatch):
    """The reported outage: a query every 60s kept the database awake around
    the clock and burned a month of compute quota in about a week."""
    from stock_monitor import store
    from stock_monitor.store import WatchlistStore

    queries = []
    monkeypatch.setattr(db, "is_enabled", lambda: True)

    watch = WatchlistStore()
    watch._symbols = ["NVDA"]
    watch._loaded_at = store.time.monotonic()   # already loaded once

    def explode():
        queries.append(1)
        raise AssertionError("must not query while the cache is fresh")

    monkeypatch.setattr(db, "session_scope", lambda: explode())

    for _ in range(60):          # an hour of polling at the configured interval
        assert watch.all() == ["NVDA"]
    assert queries == []


def test_a_failed_read_is_not_cached_as_empty(monkeypatch):
    """Otherwise a blip would serve an empty portfolio for the whole window."""
    from stock_monitor.store import PortfolioStore

    monkeypatch.setattr(db, "is_enabled", lambda: True)
    monkeypatch.setattr(
        db, "session_scope",
        lambda: (_ for _ in ()).throw(RuntimeError("database is not initialized")),
    )
    store_ = PortfolioStore()

    assert store_.all() == []
    assert store_._loaded_at == 0.0, "a failure must leave the cache cold"


def test_a_write_is_visible_without_waiting_for_the_window(monkeypatch):
    """Writes update memory as well as the row, so no invalidation is needed."""
    from stock_monitor import store
    from stock_monitor.store import WatchlistStore

    monkeypatch.setattr(db, "is_enabled", lambda: False)
    watch = WatchlistStore()
    watch.add("NVDA")
    watch._loaded_at = store.time.monotonic()

    watch.add("AVGO")
    assert "AVGO" in watch.all()
    watch.remove("NVDA")
    assert "NVDA" not in watch.all()


def test_the_window_is_long_enough_to_let_the_database_sleep():
    """Under five minutes and a managed instance never suspends, which is the
    entire failure this constant exists to prevent."""
    from stock_monitor.store import READ_CACHE_TTL

    assert READ_CACHE_TTL >= 600
