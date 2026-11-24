"""Email delivery for alerts, by HTTPS API or by SMTP.

Why there are two providers
───────────────────────────
SMTP was the obvious choice — three environment variables and the standard
library — and it is the wrong one on the host this runs on. Render's free web
services block outbound traffic to **all** the SMTP ports, 25, 465 and 587
alike, so a correctly configured Gmail app password still ends in a connection
that never opens. That is not a configuration problem and no amount of
re-reading the settings reveals it.

An HTTPS mail API goes out over port 443 like every other request this process
makes, so it is unaffected. Hence the order of preference:

* ``resend`` — one API key, an ordinary HTTPS POST. Works on Render's free
  plan. Without a verified domain, Resend sends from ``onboarding@resend.dev``
  and will deliver **only to the address that owns the Resend account** — which
  is exactly the shape of this feature: alerts to yourself.
* ``smtp`` — kept because it is right everywhere else: a local run, a paid
  Render instance, any other host. No account with anyone required.

Whichever is configured is used; Resend wins if both are, because if both are
set up on this host it is the one that can actually deliver.

Saying why, not just that
─────────────────────────
A failed send used to report "it did not go out", which sent the user to the
host's logs to learn anything more — while the exception that said exactly
what was wrong was right there. Every failure now maps to a Hebrew line naming
the cause and the remedy, kept on ``last_error`` for the alert itself and for
``/api/diagnostics/email``.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

# ── Resend (HTTPS) ────────────────────────────────────────────────────────────
ENV_RESEND_KEY = "RESEND_API_KEY"
ENV_RESEND_FROM = "RESEND_FROM"
RESEND_URL = "https://api.resend.com/emails"
#: Resend's shared sender, usable before any domain is verified. Mail from it
#: reaches only the Resend account's own address.
DEFAULT_FROM = "Stock Monitor <onboarding@resend.dev>"

# ── SMTP ──────────────────────────────────────────────────────────────────────
ENV_HOST = "SMTP_HOST"
ENV_PORT = "SMTP_PORT"
ENV_USER = "SMTP_USER"
ENV_PASSWORD = "SMTP_PASSWORD"

ENV_TO = "ALERT_EMAIL_TO"

DEFAULT_PORT = 587
#: Implicit TLS from the first byte, rather than STARTTLS after a greeting.
IMPLICIT_TLS_PORTS = (465,)
TIMEOUT = 20

#: Hosts known to block outbound SMTP, so the diagnostics can say so instead of
#: leaving a working configuration looking broken.
SMTP_BLOCKED_HINT = (
    "שירותים חינמיים ב-Render חוסמים את כל פורטי ה-SMTP (25, 465, 587), "
    "ולכן SMTP לא יעבוד שם גם כשההגדרות נכונות. השתמש ב-RESEND_API_KEY "
    "שעובד מעל HTTPS."
)


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def settings() -> dict:
    """What the environment says. Secrets appear only as booleans."""
    try:
        port = int(_env(ENV_PORT) or DEFAULT_PORT)
    except ValueError:
        logger.warning("%s is not a number — using %d", ENV_PORT, DEFAULT_PORT)
        port = DEFAULT_PORT
    user = _env(ENV_USER)
    return {
        "resend_key": bool(_env(ENV_RESEND_KEY)),
        "resend_from": _env(ENV_RESEND_FROM) or DEFAULT_FROM,
        "host": _env(ENV_HOST),
        "port": port,
        "user": user,
        "has_password": bool(os.getenv(ENV_PASSWORD)),
        "to": _env(ENV_TO) or user,
    }


def active_provider() -> str:
    """"resend", "smtp", or "" when neither is configured.

    Resend first: where both are set up, it is the one that can deliver from a
    host that blocks SMTP, and a host that allows SMTP allows HTTPS too.
    """
    current = settings()
    if current["resend_key"] and current["to"]:
        return "resend"
    if current["host"] and current["user"] and current["has_password"] and current["to"]:
        return "smtp"
    return ""


def is_configured() -> bool:
    return bool(active_provider())


def missing_settings() -> List[str]:
    """What is still needed — for the provider closest to being configured.

    Naming every variable of both providers would be a wall of text telling
    the user to do twice the work. So: if either has been started, finish that
    one; if neither, ask for the one line that works everywhere.
    """
    current = settings()
    if active_provider():
        return []

    smtp_started = bool(current["host"] or current["user"] or current["has_password"])
    if current["resend_key"] or not smtp_started:
        missing = [] if current["resend_key"] else [ENV_RESEND_KEY]
        if not current["to"]:
            missing.append(ENV_TO)
        return missing

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


# ── What went wrong, in words that say what to do ─────────────────────────────

def friendly_error(exc: Exception) -> str:
    """The cause of a failed send, and its remedy, in one Hebrew line."""
    text = str(exc).lower()
    name = exc.__class__.__name__

    if isinstance(exc, smtplib.SMTPAuthenticationError) or "535" in text or (
            "username and password not accepted" in text):
        return (
            "האימות מול שרת המייל נדחה. בג'ימייל נדרשת App Password "
            "(16 תווים, עם אימות דו-שלבי מופעל) ולא סיסמת החשבון."
        )
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return f"כתובת הנמען נדחתה — בדוק את `{ENV_TO}`."
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return f"כתובת השולח נדחתה — היא חייבת להיות אותה כתובת של `{ENV_USER}`."
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return "השרת לא תומך ב-STARTTLS בפורט הזה. נסה SMTP_PORT=465."
    if isinstance(exc, ssl.SSLError):
        return ("כשל בהצפנה — כנראה פורט לא מתאים. 587 ל-STARTTLS, "
                "465 ל-TLS מלא.")
    if isinstance(exc, (TimeoutError, ConnectionRefusedError, OSError)) or (
            "timed out" in text or "connection refused" in text
            or "network is unreachable" in text):
        return (
            "לא נפתח חיבור לשרת המייל. זה הסימן המובהק לחסימת פורטי SMTP "
            f"על ידי המאחסן. {SMTP_BLOCKED_HINT}"
        )
    return f"השליחה נכשלה ({name})."


def _http_error(status: int, body: str) -> str:
    """A Resend rejection, translated. Its own message is usually precise."""
    detail = ""
    try:
        parsed = json.loads(body or "{}")
        detail = str(parsed.get("message") or "")
    except (TypeError, ValueError):
        detail = (body or "")[:200]

    if status in (401, 403) and "own email address" in detail.lower():
        return (
            "Resend מאפשר שליחה רק לכתובת שבה נרשמת אליו, כל עוד לא אימתת "
            f"דומיין. ודא ש-`{ENV_TO}` היא אותה כתובת של חשבון ה-Resend."
        )
    if status in (401, 403):
        return f"מפתח ה-API של Resend נדחה — בדוק את `{ENV_RESEND_KEY}`."
    if status == 422:
        return (f"Resend דחה את הבקשה: {detail or 'נתונים לא תקינים'}. "
                f"אם הגדרת `{ENV_RESEND_FROM}` — הדומיין שבו חייב להיות מאומת.")
    if status == 429:
        return "חרגת ממגבלת השליחה של Resend. נסה שוב בעוד כמה דקות."
    return f"Resend החזיר שגיאה {status}{f': {detail}' if detail else ''}."


# ── Sending ───────────────────────────────────────────────────────────────────

class EmailNotifier:
    """Sends one alert. Reads the environment on every send.

    Reading each time rather than at construction means adding the variables
    on the host is enough — no code path has to remember whether mail was
    configured when the process started.
    """

    def __init__(self) -> None:
        #: Why the last send failed, in Hebrew. Empty after a success.
        self.last_error = ""

    def send(self, subject: str, body: str, to: Optional[str] = None) -> bool:
        current = settings()
        recipient = (to or current["to"]).strip()
        provider = active_provider()

        if not provider or not recipient:
            missing = ", ".join(missing_settings()) or "הגדרות"
            self.last_error = f"שליחת מייל אינה מוגדרת (חסר {missing})."
            logger.error("Email not sent: missing %s", missing)
            return False

        try:
            if provider == "resend":
                self._send_resend(subject, body, recipient, current)
            else:
                self._send_smtp(subject, body, recipient, current)
        except _HttpRejection as exc:
            self.last_error = _http_error(exc.status, exc.body)
            logger.error("Email rejected by Resend (%s): %s", exc.status, exc.body[:300])
            return False
        except Exception as exc:                       # noqa: BLE001
            # Never fatal: a failed mail must not cost the Telegram alert that
            # the same event also sends, nor stop the monitor's cycle.
            self.last_error = friendly_error(exc)
            logger.error("Email send failed (%s): %s", exc.__class__.__name__, exc)
            return False

        self.last_error = ""
        logger.info("Alert emailed to %s via %s", recipient, provider)
        return True

    # -- providers ----------------------------------------------------------

    @staticmethod
    def _send_resend(subject: str, body: str, recipient: str, current: dict) -> None:
        response = requests.post(
            RESEND_URL,
            headers={
                "Authorization": f"Bearer {os.getenv(ENV_RESEND_KEY, '').strip()}",
                "Content-Type": "application/json",
            },
            json={
                "from": current["resend_from"],
                "to": [recipient],
                "subject": subject,
                "text": body,
            },
            timeout=TIMEOUT,
        )
        if response.status_code >= 400:
            raise _HttpRejection(response.status_code, response.text)

    @staticmethod
    def _send_smtp(subject: str, body: str, recipient: str, current: dict) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = current["user"]
        message["To"] = recipient
        message.set_content(body)

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


class _HttpRejection(Exception):
    """A mail API answered, and said no. Carries the status and the body."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}")
        self.status, self.body = status, body or ""


#: Shared instance, so ``last_error`` from the most recent send is readable by
#: the diagnostics endpoint as well as by the dispatcher that sent it.
notifier = EmailNotifier()

TEST_SUBJECT = "בדיקה: התראות המניות שלך"
TEST_BODY = "\n".join([
    "זו הודעת בדיקה שנשלחה ביוזמתך מתוך מערכת מעקב המניות.",
    "",
    "אם הגיעה — שליחת המייל עובדת, והתראות שתגדיר עם 'במייל' יגיעו לכאן.",
    "כך תיראה התראה אמיתית:",
    "",
    "    ZS — חצתה את $233.30 כלפי מעלה (מחיר נוכחי $233.57)",
    "    הבקשה שהגדרת: תתריע במייל אם ZS חוצה את מחיר 233.3",
    "",
    "לא נדרשת שום פעולה בעקבות ההודעה הזו.",
])


def send_test(to: Optional[str] = None) -> dict:
    """Send one test message on demand, and report exactly what happened.

    Without this, the only way to find out whether mail works was to write a
    rule, wait for the market to satisfy it, and see whether anything arrived —
    which is how a blocked SMTP port went unnoticed until a real alert needed
    it. This runs the same code path a real alert takes, on the host that will
    take it, and hands back the provider's own answer.
    """
    provider = active_provider()
    if not provider:
        missing = ", ".join(missing_settings())
        return {
            "sent": False, "provider": "none",
            "to": settings()["to"],
            "error": f"שליחת מייל אינה מוגדרת (חסר {missing}).",
        }

    recipient = (to or settings()["to"]).strip()
    sent = notifier.send(TEST_SUBJECT, TEST_BODY, recipient)
    return {
        "sent": sent,
        "provider": provider,
        "to": recipient,
        "error": "" if sent else notifier.last_error,
    }


def status() -> dict:
    """For diagnostics: which provider, what is missing, why the last send failed."""
    current = settings()
    provider = active_provider()
    report = {
        "provider": provider or "none",
        "configured": bool(provider),
        "to": current["to"],
        "missing": missing_settings(),
        "last_error": notifier.last_error,
        "resend_key_set": current["resend_key"],
        "smtp_host": current["host"],
        "smtp_port": current["port"],
        "smtp_user": current["user"],
    }
    if provider == "smtp":
        report["warning"] = SMTP_BLOCKED_HINT
    return report
