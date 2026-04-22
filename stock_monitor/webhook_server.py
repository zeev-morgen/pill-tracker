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
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .notifier import NotificationDispatcher
from .dashboard import router as dashboard_router

logger = logging.getLogger(__name__)


def create_webhook_app(
    dispatcher: NotificationDispatcher, secret: str = ""
) -> FastAPI:
    app = FastAPI(title="Stock Monitor", version="1.0.0", docs_url="/docs")
    app.include_router(dashboard_router)

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
        return {"status": "running", "utc": datetime.now(timezone.utc).isoformat()}

    return app
