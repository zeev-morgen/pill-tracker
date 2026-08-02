"""
Notification dispatcher: Telegram Bot API, Discord Webhooks, Desktop (plyer).

Usage
─────
    dispatcher = NotificationDispatcher(config.notifications)
    dispatcher.dispatch(alert_event, session="regular")
    dispatcher.dispatch_raw("TradingView", "AAPL broke above 200-day MA")
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import requests
import urllib3

from .alert_engine import AlertEvent
from .config import NotificationConfig

logger = logging.getLogger(__name__)


def _post_with_ssl_fallback(url: str, **kwargs) -> requests.Response:
    """POST with normal SSL verification; retry verify=False if an SSLError is raised.

    Useful behind corporate firewalls / antivirus tools (Kaspersky, Bitdefender,
    ESET, Symantec, etc.) that perform TLS inspection with a self-signed root CA.
    """
    try:
        return requests.post(url, timeout=10, **kwargs)
    except requests.exceptions.SSLError as exc:
        logger.warning(
            "SSL verification failed for %s (%s) — retrying without verification. "
            "A proxy/antivirus is likely intercepting HTTPS.",
            url.split("?")[0],
            exc.__class__.__name__,
        )
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return requests.post(url, timeout=10, verify=False, **kwargs)

# ── Formatting helpers ────────────────────────────────────────────────────────

_SEVERITY_ICON = {"INFO": "ℹ️", "WARNING": "⚠️", "CRITICAL": "🚨"}
_SESSION_ICON  = {"pre": "🌅", "regular": "📈", "after": "🌆", "closed": "🔒"}
_DISCORD_COLOR = {"INFO": 0x3498DB, "WARNING": 0xE67E22, "CRITICAL": 0xE74C3C}


def _format_message(event: AlertEvent, session: str) -> str:
    sev  = _SEVERITY_ICON.get(event.severity, "ℹ️")
    sess = _SESSION_ICON.get(session, "📊")
    ts   = event.timestamp.strftime("%H:%M:%S ET")
    return f"{sev} {sess} *STOCK ALERT*\n{event.message}\n⏰ {ts}"


# ── Individual notifiers ──────────────────────────────────────────────────────

class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._url     = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id

    def send(self, text: str) -> bool:
        try:
            resp = _post_with_ssl_fallback(
                self._url,
                json={"chat_id": self._chat_id, "text": text, "parse_mode": "Markdown"},
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Telegram send failed: %s", exc)
            return False


class DiscordNotifier:
    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url

    def send(self, text: str, severity: str = "INFO") -> bool:
        try:
            color = _DISCORD_COLOR.get(severity, 0x3498DB)
            payload = {
                "embeds": [{
                    "description": text,
                    "color": color,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }]
            }
            resp = _post_with_ssl_fallback(self._url, json=payload)
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Discord send failed: %s", exc)
            return False


class DesktopNotifier:
    def __init__(self) -> None:
        self._available = False
        try:
            from plyer import notification as _n  # type: ignore
            self._plyer = _n
            self._available = True
        except ImportError:
            logger.warning("plyer not installed — desktop notifications disabled")

    def send(self, title: str, body: str) -> bool:
        if not self._available:
            return False
        try:
            self._plyer.notify(
                title=title, message=body, app_name="Stock Monitor", timeout=10
            )
            return True
        except Exception as exc:
            logger.error("Desktop notification failed: %s", exc)
            return False


# ── Dispatcher ────────────────────────────────────────────────────────────────

class NotificationDispatcher:
    """Fanout hub: sends every alert through all enabled channels."""

    def __init__(self, config: NotificationConfig) -> None:
        self._telegram: Optional[TelegramNotifier] = None
        self._discord:  Optional[DiscordNotifier]  = None
        self._desktop:  Optional[DesktopNotifier]  = None

        if config.telegram.enabled and config.telegram.bot_token:
            self._telegram = TelegramNotifier(
                config.telegram.bot_token, config.telegram.chat_id
            )
            logger.info("Telegram notifications enabled (chat_id=%s)", config.telegram.chat_id)

        if config.discord.enabled and config.discord.webhook_url:
            self._discord = DiscordNotifier(config.discord.webhook_url)
            logger.info("Discord notifications enabled")

        if config.desktop.enabled:
            self._desktop = DesktopNotifier()

    def dispatch(self, event: AlertEvent, session: str = "regular") -> None:
        """Send a structured AlertEvent through all channels."""
        msg = _format_message(event, session)
        logger.info("ALERT ▶ %s", event.message)
        self._fanout(msg, event.severity, f"Stock Alert: {event.symbol}", event.message)

    def dispatch_raw(self, title: str, body: str) -> None:
        """Send a free-form message (e.g. from a TradingView webhook)."""
        logger.info("RAW ALERT ▶ [%s] %s", title, body)
        msg = f"📡 *{title}*\n{body}"
        self._fanout(msg, "INFO", title, body)

    def _fanout(
        self, formatted: str, severity: str, desktop_title: str, desktop_body: str
    ) -> None:
        if self._telegram:
            self._telegram.send(formatted)
        if self._discord:
            self._discord.send(formatted, severity)
        if self._desktop:
            self._desktop.send(desktop_title, desktop_body)
