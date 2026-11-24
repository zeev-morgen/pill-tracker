"""Email delivery for alerts, over SMTP.

Kept to the standard library on purpose. A transactional-mail API would mean
another account, another key and another dependency to carry for what is, at
this volume, a handful of messages a week; ``smtplib`` with an app password is
three environment variables and no new code to trust.

Configuration (all from the environment, like the rest of the secrets)::

    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587            # 587 = STARTTLS (default), 465 = implicit TLS
    SMTP_USER=you@gmail.com
    SMTP_PASSWORD=...        # Gmail: an App Password, not the account password
    ALERT_EMAIL_TO=you@gmail.com   # optional; defaults to SMTP_USER

Two things worth knowing before relying on it. Gmail and most providers refuse
a plain account password over SMTP — the password here must be an app-specific
one, generated with two-factor authentication enabled. And hosts commonly
block outbound port 25, so 587 or 465 are the ports to use; this module
chooses the right handshake from the port rather than asking.

The subject and body arrive already written, in Hebrew, with the Unicode
direction marks stripped: a mail client applies its own bidi handling to a
plain-text part, and the marks would show up as stray characters in some of
them. ``EmailMessage`` handles the UTF-8 encoding and the headers.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

logger = logging.getLogger(__name__)

ENV_HOST = "SMTP_HOST"
ENV_PORT = "SMTP_PORT"
ENV_USER = "SMTP_USER"
ENV_PASSWORD = "SMTP_PASSWORD"
ENV_TO = "ALERT_EMAIL_TO"

DEFAULT_PORT = 587
#: Implicit TLS from the first byte, rather than STARTTLS after a greeting.
IMPLICIT_TLS_PORTS = (465,)
TIMEOUT = 20


def settings() -> dict:
    """What the environment currently says, with the password only as a flag."""
    host = (os.getenv(ENV_HOST) or "").strip()
    user = (os.getenv(ENV_USER) or "").strip()
    password = os.getenv(ENV_PASSWORD) or ""
    try:
        port = int(os.getenv(ENV_PORT) or DEFAULT_PORT)
    except ValueError:
        logger.warning("%s is not a number — using %d", ENV_PORT, DEFAULT_PORT)
        port = DEFAULT_PORT
    return {
        "host": host,
        "port": port,
        "user": user,
        "has_password": bool(password),
        "to": (os.getenv(ENV_TO) or user).strip(),
    }


def is_configured() -> bool:
    """True when a mail could actually be sent, so callers can say why not."""
    current = settings()
    return bool(current["host"] and current["user"]
                and current["has_password"] and current["to"])


def missing_settings() -> list:
    """The names of the environment variables still needed. For the user."""
    current = settings()
    missing = []
    if not current["host"]:
        missing.append(ENV_HOST)
    if not current["user"]:
        missing.append(ENV_USER)
    if not current["has_password"]:
        missing.append(ENV_PASSWORD)
    if not current["to"]:
        missing.append(ENV_TO)
    return missing


class EmailNotifier:
    """Sends one alert by mail. Reads the environment on every send.

    Reading each time rather than at construction means adding the variables
    on the host and restarting is enough — no code path has to remember
    whether mail was configured when the process started.
    """

    def send(self, subject: str, body: str, to: Optional[str] = None) -> bool:
        current = settings()
        recipient = (to or current["to"]).strip()
        if not (current["host"] and current["user"] and recipient):
            logger.error(
                "Email not sent: missing %s", ", ".join(missing_settings()) or "settings"
            )
            return False

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = current["user"]
        message["To"] = recipient
        message.set_content(body)

        try:
            self._deliver(message, current)
            logger.info("Alert emailed to %s", recipient)
            return True
        except Exception as exc:                       # noqa: BLE001
            # Never fatal: a failed mail must not cost the Telegram alert that
            # the same event also sends, nor stop the monitor's cycle.
            logger.error("Email send failed (%s): %s", exc.__class__.__name__, exc)
            return False

    @staticmethod
    def _deliver(message: EmailMessage, current: dict) -> None:
        context = ssl.create_default_context()
        password = os.getenv(ENV_PASSWORD) or ""
        if current["port"] in IMPLICIT_TLS_PORTS:
            with smtplib.SMTP_SSL(current["host"], current["port"],
                                  timeout=TIMEOUT, context=context) as server:
                server.login(current["user"], password)
                server.send_message(message)
            return
        with smtplib.SMTP(current["host"], current["port"], timeout=TIMEOUT) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(current["user"], password)
            server.send_message(message)


def status() -> dict:
    """For diagnostics: configured or not, and what is missing. No secrets."""
    current = settings()
    return {
        "configured": is_configured(),
        "host": current["host"],
        "port": current["port"],
        "user": current["user"],
        "to": current["to"],
        "missing": missing_settings(),
    }
