"""Dashboard authentication — protects a public deployment.

/health and /webhook/* must stay reachable without a password: the first is
the uptime-pinger target that keeps a free-tier host awake, the second is
called by TradingView and is guarded by its own HMAC signature instead.
"""

import base64

import pytest
from fastapi.testclient import TestClient

from stock_monitor.config import NotificationConfig
from stock_monitor.notifier import NotificationDispatcher
from stock_monitor.webhook_server import create_webhook_app

USER, PASSWORD = "zeev", "s3cret-pass"


def auth_header(user: str = USER, password: str = PASSWORD) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client():
    return TestClient(create_webhook_app(NotificationDispatcher(NotificationConfig()), ""))


@pytest.fixture
def secured(monkeypatch, client):
    monkeypatch.setenv("DASHBOARD_USER", USER)
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    return client


# ── Auth disabled (local development) ─────────────────────────────────────────

def test_no_password_required_when_unconfigured(monkeypatch, client):
    monkeypatch.delenv("DASHBOARD_USER", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    assert client.get("/").status_code == 200


def test_partial_config_does_not_enable_auth(monkeypatch, client):
    # A username with no password must not half-enable auth and lock the user out.
    monkeypatch.setenv("DASHBOARD_USER", USER)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    assert client.get("/").status_code == 200


# ── Auth enabled (public deployment) ──────────────────────────────────────────

@pytest.mark.parametrize("path", ["/", "/api/status", "/api/holdings", "/api/portfolio", "/docs"])
def test_protected_paths_reject_anonymous(secured, path):
    assert secured.get(path).status_code == 401


def test_correct_credentials_allowed(secured):
    assert secured.get("/", headers=auth_header()).status_code == 200


@pytest.mark.parametrize(
    "user,password",
    [(USER, "wrong"), ("wrong", PASSWORD), ("wrong", "wrong"), ("", "")],
)
def test_wrong_credentials_rejected(secured, user, password):
    assert secured.get("/", headers=auth_header(user, password)).status_code == 401


def test_challenge_header_present(secured):
    # Without this browsers show a blank 401 instead of a login prompt.
    assert secured.get("/").headers["www-authenticate"].startswith("Basic")


@pytest.mark.parametrize(
    "header",
    ["Basic !!not-base64!!", "Basic", "Bearer sometoken", "Basic " + base64.b64encode(b"nocolon").decode()],
)
def test_malformed_authorization_header_is_rejected_not_crashed(secured, header):
    assert secured.get("/", headers={"Authorization": header}).status_code == 401


# ── Paths that must remain public ─────────────────────────────────────────────

def test_health_stays_public_for_keepalive(secured):
    assert secured.get("/health").status_code == 200


def test_webhooks_stay_public(secured):
    response = secured.post("/webhook/custom", json={"title": "t", "message": "m"})
    assert response.status_code == 200


# ── Keep-alive probe compatibility ────────────────────────────────────────────

@pytest.mark.parametrize("method", ["get", "head"])
def test_health_answers_both_probe_methods(secured, method):
    """Uptime monitors probe with HEAD by default; GET is the manual check.

    A 405 here silently breaks the keep-alive, letting a free-tier instance
    sleep and stopping all monitoring.
    """
    assert getattr(secured, method)("/health").status_code == 200


@pytest.mark.parametrize("method", ["get", "head"])
def test_root_rejects_unauthenticated_probes(secured, method):
    # Pointing a monitor at "/" instead of "/health" must fail loudly (401),
    # never silently succeed against the protected dashboard.
    assert getattr(secured, method)("/").status_code == 401


# ── Caching ───────────────────────────────────────────────────────────────────

def test_live_responses_are_not_cacheable():
    """None of these carried a Cache-Control header.

    A browser or an intermediate proxy was free to serve an old copy of the
    price API indefinitely, which looks exactly like a portfolio that has
    stopped updating — and like a deploy that never landed.
    """
    from fastapi.testclient import TestClient

    from stock_monitor.config import NotificationConfig
    from stock_monitor.notifier import NotificationDispatcher
    from stock_monitor.webhook_server import create_webhook_app

    client = TestClient(create_webhook_app(NotificationDispatcher(NotificationConfig()), ""))
    for path in ("/", "/api/watchlist", "/api/journal"):
        response = client.get(path)
        assert "no-store" in response.headers.get("cache-control", ""), path
