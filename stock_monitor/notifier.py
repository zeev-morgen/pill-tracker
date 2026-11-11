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

from . import messages, mute
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

_DISCORD_COLOR = {"INFO": 0x3498DB, "WARNING": 0xE67E22, "CRITICAL": 0xE74C3C}


def _format_message(event: AlertEvent, session: str) -> str:
    """The text a person reads — Hebrew, for every alert type.

    All of it comes from :mod:`stock_monitor.messages`; nothing is worded here.
    """
    return messages.alert(event, session)


def _alert_buttons(event: AlertEvent) -> dict:
    """What to offer on the alert itself.

    An alert is the moment you want to act, and the actions are always the
    same two: understand it, or stop it repeating. Both were a dashboard trip
    away until now. The callback payloads are the ones
    ``TelegramCommandBot._handle_callback`` answers.
    """
    return {
        "inline_keyboard": [[
            {"text": "🧠 ניתוח AI", "callback_data": f"analyze:{event.symbol}"},
            {"text": "🔕 השתקה לשעה", "callback_data": f"mute:{event.symbol}:60"},
        ]]
    }


# ── Individual notifiers ──────────────────────────────────────────────────────

class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._url     = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id

    def send(self, text: str, reply_markup: Optional[dict] = None) -> bool:
        payload = {"chat_id": self._chat_id, "text": text, "parse_mode": "Markdown"}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            resp = _post_with_ssl_fallback(self._url, json=payload)
            if resp.status_code == 400 and "parse" in resp.text.lower():
                # Telegram's legacy Markdown has no escape syntax, so one stray
                # asterisk or underscore — in a headline, or in text the AI
                # wrote — rejects the whole message. Better an unformatted
                # alert than no alert.
                logger.warning(
                    "Telegram rejected the Markdown in a message — resending as plain text"
                )
                payload.pop("parse_mode")
                resp = _post_with_ssl_fallback(self._url, json=payload)
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
        # The log line and the stored alert history are kept whatever the mute
        # says — a muted hour should cost you the buzz, not the record of what
        # happened during it.
        logger.info("ALERT ▶ %s", event.message)
        if mute.is_muted(event.symbol):
            until = mute.muted_until(event.symbol) or mute.muted_until(mute.ALL)
            logger.info(
                "Alert for %s not sent: muted until %s",
                event.symbol, until.strftime("%H:%M UTC") if until else "?",
            )
            return

        self._fanout(
            _format_message(event, session),
            event.severity,
            f"Stock Alert: {event.symbol}",
            event.message,
            reply_markup=_alert_buttons(event),
        )

    def dispatch_raw(self, title: str, body: str, framed: bool = True) -> None:
        """Send a free-form message (e.g. from a TradingView webhook).

        ``framed`` adds the "external alert" header, which is right for a
        webhook from somewhere else and wrong for a message this system has
        already worded for itself — the earnings reminder, for one.
        """
        logger.info("RAW ALERT ▶ [%s] %s", title, body)
        # Only a global mute silences these: a per-symbol mute is about one
        # noisy stock, and these carry no symbol to match it against.
        if mute.is_muted():
            logger.info("Raw alert '%s' not sent: alerts are muted", title)
            return
        text = messages.external_alert(title, body) if framed else body
        self._fanout(text, "INFO", title, body)

    def _fanout(
        self, formatted: str, severity: str, desktop_title: str, desktop_body: str,
        reply_markup: Optional[dict] = None,
    ) -> None:
        if self._telegram:
            self._telegram.send(formatted, reply_markup=reply_markup)
        if self._discord:
            self._discord.send(formatted, severity)
        if self._desktop:
            self._desktop.send(desktop_title, desktop_body)
