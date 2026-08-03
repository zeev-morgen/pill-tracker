"""
FastAPI webhook server — receives and dispatches TradingView alerts.

Endpoints
─────────
POST /webhook/tradingview   — TradingView alert webhook (JSON or plain text)
POST /webhook/custom        — Generic JSON webhook for custom integrations
GET  /health                — Liveness probe

TradingView JSON payload example
─────────────────────────────────
{
  "ticker":  "{{ticker}}",
  "close":   {{close}},
  "message": "{{strategy.order.comment}}"
}

Signature verification (optional)
───────────────────────────────────
Set TRADINGVIEW_WEBHOOK_SECRET in .env.  The server will then require the
request to carry an X-Signature header with value:
    sha256=HMAC_SHA256(secret, raw_body)
"""

import hashlib
import base64
import binascii
import hmac
import json
import logging
import os
import secrets as pysecrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .notifier import NotificationDispatcher
from .dashboard import router as dashboard_router
from .version import build_info

logger = logging.getLogger(__name__)

# Paths that must stay reachable without a login:
#   /health        — uptime pingers and the host's own health check
#   /webhook/*     — TradingView and custom integrations, guarded by the
#                    separate HMAC signature instead of a password
_PUBLIC_PATHS = ("/health", "/webhook/")


def _auth_configured() -> tuple[str, str]:
    return os.environ.get("DASHBOARD_USER", ""), os.environ.get("DASHBOARD_PASSWORD", "")


def _credentials_ok(header: Optional[str], user: str, password: str) -> bool:
    """Constant-time check of an HTTP Basic Authorization header."""
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, IndexError):
        return False
    got_user, _, got_password = decoded.partition(":")
    # Compare both halves regardless of the first result to avoid leaking
    # which of the two was wrong through response timing.
    user_ok = pysecrets.compare_digest(got_user, user)
    password_ok = pysecrets.compare_digest(got_password, password)
    return user_ok and password_ok


def create_webhook_app(
    dispatcher: NotificationDispatcher, secret: str = ""
) -> FastAPI:
    app = FastAPI(title="Stock Monitor", version="1.0.0", docs_url="/docs")
    app.include_router(dashboard_router)

    # ── Dashboard authentication ──────────────────────────────────────────────
    # Enabled only when DASHBOARD_USER and DASHBOARD_PASSWORD are both set, so
    # local runs are unaffected. Required for any public deployment.
    @app.middleware("http")
    async def require_basic_auth(request: Request, call_next):
        user, password = _auth_configured()
        if not user or not password:
            return await call_next(request)
        if request.url.path.startswith(_PUBLIC_PATHS):
            return await call_next(request)
        if not _credentials_ok(request.headers.get("authorization"), user, password):
            return Response(
                content="Authentication required",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Stock Monitor"'},
            )
        return await call_next(request)

    # ── Signature helper ──────────────────────────────────────────────────────

    def _valid_signature(body: bytes, header: Optional[str]) -> bool:
        if not secret:
            return True
        if not header:
            return False
        expected = hmac.new(
            secret.encode(), body, digestmod=hashlib.sha256
        ).hexdigest()
        received = header.lower().removeprefix("sha256=")
        return hmac.compare_digest(expected, received)

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.post("/webhook/tradingview")
    async def tradingview(
        request: Request,
        x_signature: Optional[str] = Header(None),
    ):
        body = await request.body()

        if not _valid_signature(body, x_signature):
            raise HTTPException(status_code=401, detail="Invalid or missing signature")

        try:
            ct = request.headers.get("content-type", "")
            if "application/json" in ct or body.lstrip().startswith(b"{"):
                data     = json.loads(body)
                symbol   = data.get("ticker", data.get("symbol", "UNKNOWN"))
                price    = data.get("close", data.get("price", "N/A"))
                msg_text = data.get("message", data.get("alert_message", ""))
                title    = f"TradingView: {symbol}"
                body_txt = f"Symbol: {symbol} | Price: {price}\n{msg_text}".strip()
            else:
                title    = "TradingView Alert"
                body_txt = body.decode("utf-8", errors="replace")

            dispatcher.dispatch_raw(title, body_txt)
            logger.info("TradingView webhook processed: %s", title)
            return JSONResponse({"status": "ok", "dispatched": True})

        except Exception as exc:
            logger.error("Webhook processing error: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/webhook/custom")
    async def custom(request: Request):
        """Generic JSON webhook: {"title": "...", "message": "..."}"""
        try:
            data  = await request.json()
            title = str(data.get("title", "Custom Alert"))
            msg   = str(data.get("message", json.dumps(data)))
            dispatcher.dispatch_raw(title, msg)
            return JSONResponse({"status": "ok"})
        except Exception as exc:
            logger.error("Custom webhook error: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/health")
    async def health():
        # Also the uptime-ping target, so it stays cheap and public. The build
        # fields make it possible to confirm which revision is deployed.
        return {
            "status": "running",
            "utc": datetime.now(timezone.utc).isoformat(),
            **build_info(),
        }

    return app
