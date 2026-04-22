"""
Telegram command bot — responds to ticker queries and slash commands in Hebrew.

Commands
────────
/start          ← ברוך הבא
/help           ← רשימת פקודות
/status         ← כל המניות במעקב עם מחיר עדכני
/[ticker]       ← נתוני מניה, לדוגמה /aapl
[TICKER]        ← שלח טיקר ישירות, לדוגמה NVDA

The bot runs as a background asyncio task using long-polling.
"""

import asyncio
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional

import pytz
import requests
import urllib3

from .data_feed import StockDataFeed, get_market_session

logger = logging.getLogger(__name__)
NYSE_TZ = pytz.timezone("America/New_York")

_TICKER_RE = re.compile(r"^[A-Z]{1,6}$")

_SESSION_HE = {
    "pre":     "טרום מסחר 🌅",
    "regular": "מסחר רגיל 📈",
    "after":   "אחרי שעות הפעילות 🌆",
    "closed":  "שוק סגור 🔒",
}
_HELP_TEXT = (
    "📋 *פקודות זמינות:*\n\n"
    "• שלח *טיקר* כלשהו ← נתונים מלאים\n"
    "  לדוגמה: `AAPL`, `NVDA`, `AMZN`\n\n"
    "• /status ← כל המניות במעקב\n"
    "• /help   ← הודעה זו\n"
)


def _fmt_vol(v: int) -> str:
    if v >= 1_000_000_000:
        return f"{v/1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"{v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v/1_000:.0f}K"
    return str(v)


def _pct_line(label: str, pct: Optional[float], price_delta: Optional[float] = None) -> str:
    if pct is None:
        return ""
    arrow = "📈" if pct > 0 else ("📉" if pct < 0 else "➡️")
    sign  = "+" if pct >= 0 else ""
    delta_str = f" (${sign}{price_delta:.2f})" if price_delta is not None else ""
    return f"{arrow} {label}: {sign}{pct:.2f}%{delta_str}\n"


def _format_stock(symbol: str, data: dict) -> str:
    p      = data["price"]
    pc     = data.get("prev_close")
    hi     = data.get("day_high")
    lo     = data.get("day_low")
    vol    = data["volume"]
    sess   = data.get("session", "closed")
    chg    = data.get("change_pct", 0.0)
    fop    = data.get("from_open_pct")
    sc     = data.get("since_close_pct")
    rc     = data.get("regular_close")
    ts     = datetime.now(NYSE_TZ).strftime("%H:%M ET")

    lines = [f"📊 *{symbol}*\n{'─' * 20}\n"]
    lines.append(f"💰 מחיר: *${p:.2f}*\n")

    # Daily change
    if pc:
        delta = p - pc
        lines.append(_pct_line("שינוי יומי", chg, delta))

    # Since regular close (after-hours)
    if sc is not None and rc:
        lines.append(_pct_line("מסגירה (16:00)", sc, p - rc))
    elif fop is not None and sess == "regular":
        op = data.get("open_price")
        lines.append(_pct_line("מהפתיחה", fop, (p - op) if op else None))

    # Volume & range
    if vol:
        lines.append(f"📦 נפח: {_fmt_vol(vol)}\n")
    if hi and lo:
        lines.append(f"📊 גבוה/נמוך: ${hi:.2f} / ${lo:.2f}\n")

    lines.append(f"\n📡 סשן: {_SESSION_HE.get(sess, sess)}\n")
    lines.append(f"⏰ {ts}")
    return "".join(lines)


def _post(url: str, **kwargs) -> requests.Response:
    """POST with SSL fallback (same pattern as notifier.py)."""
    try:
        return requests.post(url, timeout=10, **kwargs)
    except requests.exceptions.SSLError:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return requests.post(url, timeout=10, verify=False, **kwargs)


def _get(url: str, **kwargs) -> requests.Response:
    """GET with SSL fallback."""
    try:
        return requests.get(url, timeout=10, **kwargs)
    except requests.exceptions.SSLError:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return requests.get(url, timeout=10, verify=False, **kwargs)


class TelegramCommandBot:
    def __init__(
        self,
        bot_token: str,
        data_feed: StockDataFeed,
        monitored_symbols: List[str],
        regular_close_ref: Dict[str, Optional[float]],
    ) -> None:
        self._base   = f"https://api.telegram.org/bot{bot_token}"
        self._feed   = data_feed
        self._syms   = [s.upper() for s in monitored_symbols]
        self._rc_ref = regular_close_ref   # shared dict from StockMonitorApp
        self._offset = 0

    # ── Polling loop ──────────────────────────────────────────────────────────

    async def poll_loop(self) -> None:
        logger.info("Telegram command bot polling started")
        while True:
            try:
                updates = self._get_updates()
                for upd in updates:
                    self._offset = upd["update_id"] + 1
                    if "message" in upd:
                        asyncio.create_task(self._handle(upd["message"]))
            except Exception as exc:
                logger.debug("Bot poll error: %s", exc)
            await asyncio.sleep(2)

    # ── Telegram API ──────────────────────────────────────────────────────────

    def _get_updates(self) -> list:
        r = _get(
            f"{self._base}/getUpdates",
            params={"offset": self._offset, "timeout": 1, "limit": 10},
        )
        if r.status_code == 200:
            return r.json().get("result", [])
        return []

    def _send(self, chat_id: int, text: str) -> None:
        try:
            _post(
                f"{self._base}/sendMessage",
                json={
                    "chat_id":    chat_id,
                    "text":       text,
                    "parse_mode": "Markdown",
                },
            )
        except Exception as exc:
            logger.error("Bot send error: %s", exc)

    # ── Message handling ──────────────────────────────────────────────────────

    async def _handle(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        text    = msg.get("text", "").strip()
        if not text:
            return

        # Strip leading slash and uppercase
        cmd = text.lstrip("/").upper()

        if cmd in ("START", "שלום", "HELLO"):
            self._send(
                chat_id,
                "👋 *שלום!*\n\nאני בוט מעקב מניות.\n\n"
                "שלח לי טיקר כמו `AAPL` או `NVDA` ואחזיר לך נתונים עדכניים.\n\n"
                + _HELP_TEXT,
            )
        elif cmd == "HELP":
            self._send(chat_id, _HELP_TEXT)
        elif cmd == "STATUS":
            await self._send_status(chat_id)
        elif _TICKER_RE.match(cmd):
            await self._send_ticker(chat_id, cmd)
        else:
            self._send(
                chat_id,
                f"❓ לא הבנתי את `{text}`.\n\nנסה לשלוח טיקר כמו `AAPL`, או /help לעזרה.",
            )

    async def _send_ticker(self, chat_id: int, symbol: str) -> None:
        self._send(chat_id, f"🔍 מחפש נתונים עבור *{symbol}*…")
        data = self._feed.get_current_data(symbol)
        if data is None:
            self._send(chat_id, f"❌ לא נמצאו נתונים עבור *{symbol}*.\nבדוק שהטיקר נכון.")
            return
        # Enrich with cached regular close if available
        rc = self._rc_ref.get(symbol)
        if rc and data["session"] != "regular":
            data["regular_close"]    = rc
            data["since_close_pct"]  = (data["price"] - rc) / rc * 100.0
        self._send(chat_id, _format_stock(symbol, data))

    async def _send_status(self, chat_id: int) -> None:
        if not self._syms:
            self._send(chat_id, "אין מניות במעקב.")
            return
        lines = [f"📋 *מניות במעקב — {get_market_session()} session*\n\n"]
        for sym in self._syms:
            data = self._feed.get_current_data(sym)
            if data:
                chg  = data.get("change_pct", 0)
                sign = "+" if chg >= 0 else ""
                lines.append(
                    f"• *{sym}*: ${data['price']:.2f} ({sign}{chg:.2f}%)\n"
                )
            else:
                lines.append(f"• *{sym}*: N/A\n")
        self._send(chat_id, "".join(lines))
