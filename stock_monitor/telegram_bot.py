"""
Telegram command bot — responds to ticker queries and slash commands in Hebrew.

Commands
────────
/start              ← ברוך הבא
/help               ← רשימת פקודות
/status             ← כל המניות במעקב עם מחיר עדכני
/portfolio  |  תיק  ← סטטוס התיק האישי + כפתור לניתוח AI של התיק
/analyze AAPL       ← ניתוח AI של מניה
ניתוח AAPL          ← ניתוח AI (עברית)
/[ticker]           ← נתוני מניה, לדוגמה /aapl
[TICKER]            ← שלח טיקר ישירות, לדוגמה NVDA

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
from .portfolio_risk import PortfolioRiskAnalyzer
from .store import portfolio_store, watchlist_store

logger = logging.getLogger(__name__)
NYSE_TZ = pytz.timezone("America/New_York")

_TICKER_RE  = re.compile(r"^[A-Z]{1,6}$")
_ANALYZE_RE = re.compile(r"^(?:ANALYZE|ניתוח)\s+([A-Z]{1,6})$", re.IGNORECASE)

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
    "• `ניתוח AAPL` ← ניתוח AI של מניה\n"
    "• /portfolio או `תיק` ← סטטוס התיק שלך\n"
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
        analyst=None,                       # Optional[StockAnalyst]
        authorized_chat_id: str = "",       # if set, bot ignores all other users
    ) -> None:
        self._base               = f"https://api.telegram.org/bot{bot_token}"
        self._feed               = data_feed
        self._config_syms        = [s.upper() for s in monitored_symbols]
        self._rc_ref             = regular_close_ref  # shared dict from StockMonitorApp
        self._analyst            = analyst
        self._risk               = PortfolioRiskAnalyzer(portfolio_store, data_feed=data_feed)
        self._authorized_chat_id = authorized_chat_id
        self._offset             = 0

    @property
    def _syms(self) -> List[str]:
        """The live watchlist, so /status reflects dashboard edits immediately.

        Falls back to the symbols passed at construction while the store is
        empty, which is what happens when no database is configured.
        """
        return watchlist_store.all() or self._config_syms

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
                    elif "callback_query" in upd:
                        asyncio.create_task(self._handle_callback(upd["callback_query"]))
            except Exception as exc:
                logger.debug("Bot poll error: %s", exc)
            await asyncio.sleep(2)

    # ── Telegram API ──────────────────────────────────────────────────────────

    def _get_updates(self) -> list:
        r = _get(
            f"{self._base}/getUpdates",
            params={
                "offset":          self._offset,
                "timeout":         1,
                "limit":           10,
                "allowed_updates": '["message","callback_query"]',
            },
        )
        if r.status_code == 200:
            return r.json().get("result", [])
        return []

    def _send(self, chat_id: int, text: str, reply_markup: Optional[dict] = None) -> None:
        try:
            payload = {
                "chat_id":    chat_id,
                "text":       text,
                "parse_mode": "Markdown",
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            _post(f"{self._base}/sendMessage", json=payload)
        except Exception as exc:
            logger.error("Bot send error: %s", exc)

    def _answer_callback(self, callback_id: str, text: str = "") -> None:
        """Acknowledge a callback_query so the loading spinner stops."""
        try:
            _post(
                f"{self._base}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text},
            )
        except Exception as exc:
            logger.debug("answerCallbackQuery error: %s", exc)

    # ── Message handling ──────────────────────────────────────────────────────

    async def _handle(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        text    = msg.get("text", "").strip()
        if not text:
            return

        # Ignore messages from unauthorized users
        if self._authorized_chat_id and str(chat_id) != self._authorized_chat_id:
            logger.debug("Ignoring message from unauthorized chat_id %s", chat_id)
            return

        # Strip leading slash and uppercase
        cmd = text.lstrip("/").upper()

        analyze_match = _ANALYZE_RE.match(text.strip())

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
        elif cmd in ("PORTFOLIO", "תיק", "התיק"):
            await self._send_portfolio(chat_id)
        elif analyze_match:
            symbol = analyze_match.group(1).upper()
            await self._send_analysis(chat_id, symbol)
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

        # Inline button to trigger AI analysis on-demand
        reply_markup = None
        if self._analyst is not None:
            reply_markup = {
                "inline_keyboard": [[
                    {"text": "🧠 ניתוח AI + המלצה", "callback_data": f"analyze:{symbol}"}
                ]]
            }
        self._send(chat_id, _format_stock(symbol, data), reply_markup=reply_markup)

    async def _handle_callback(self, cb: dict) -> None:
        """Process an inline-keyboard button press."""
        cb_id   = cb.get("id", "")
        chat_id = cb.get("message", {}).get("chat", {}).get("id")
        data    = cb.get("data", "")

        # Authorization check
        if self._authorized_chat_id and str(chat_id) != self._authorized_chat_id:
            self._answer_callback(cb_id)
            return

        self._answer_callback(cb_id, "מתחיל ניתוח…")

        if data == "analyze_portfolio":
            await self._send_portfolio_analysis(chat_id)
        elif data.startswith("analyze:"):
            symbol = data.split(":", 1)[1].upper()
            await self._send_analysis(chat_id, symbol)

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

    # ── Portfolio ─────────────────────────────────────────────────────────────

    async def _send_portfolio(self, chat_id: int) -> None:
        """Portfolio status with an inline button for the AI review."""
        self._send(chat_id, "📂 טוען את התיק…")
        loop = asyncio.get_event_loop()
        # full_report() hits yfinance once per holding — keep it off the loop.
        report = await loop.run_in_executor(None, self._risk.full_report)

        positions = report.get("positions") or []
        if not positions:
            self._send(
                chat_id,
                "התיק ריק.\nהוסף פוזיציות בדשבורד — טאב *התיק שלי*.",
            )
            return

        pnl = report.get("total_pnl_value", 0.0)
        pnl_icon = "🟢" if pnl >= 0 else "🔴"
        lines = [
            "💼 *התיק שלי*\n",
            f"שווי כולל: *${report.get('total_value', 0):,.2f}*\n",
            f"{pnl_icon} רווח/הפסד: *{'+' if pnl >= 0 else '-'}${abs(pnl):,.2f}*\n\n",
        ]
        for p in sorted(positions, key=lambda x: x["market_value"], reverse=True):
            icon = "🟢" if p["pnl_pct"] >= 0 else "🔴"
            atr = f" | ATR {p['atr_pct']:.1f}%" if p.get("atr_pct") is not None else ""
            lines.append(
                f"{icon} *{p['ticker']}* {p['pnl_pct']:+.2f}%\n"
                f"   {p['quantity']:g} × ${p['current_price']:.2f} = "
                f"${p['market_value']:,.2f}{atr}\n"
            )

        # Surface the risk alerts that the dashboard tabs would show.
        warnings = []
        if report.get("sector", {}).get("alert"):
            names = ", ".join(
                s["sector"] for s in report["sector"].get("concentrated_sectors", [])
            )
            warnings.append(f"⚠️ ריכוזיות סקטוריאלית: {names}")
        if report.get("volatility", {}).get("alert"):
            warnings.append(
                f"⚠️ חשיפת תנודתיות: {report['volatility']['exposure_pct']:.0f}% מהתיק"
            )
        if warnings:
            lines.append("\n" + "\n".join(warnings) + "\n")

        reply_markup = None
        if self._analyst is not None:
            reply_markup = {
                "inline_keyboard": [[
                    {"text": "🧠 ניתוח AI של התיק", "callback_data": "analyze_portfolio"}
                ]]
            }
        self._send(chat_id, "".join(lines), reply_markup=reply_markup)

    async def _send_portfolio_analysis(self, chat_id: int) -> None:
        if self._analyst is None:
            self._send(
                chat_id,
                "❌ ניתוח AI אינו מופעל.\n"
                "הגדר `ANTHROPIC_API_KEY` ואפשר `ai.enabled: true` בקונפיגורציה.",
            )
            return
        self._send(chat_id, "🤖 מנתח את התיק כמכלול… (עשוי לקחת עד דקה)")
        loop = asyncio.get_event_loop()

        def _report_and_analyze() -> str:
            report = self._risk.full_report()
            return self._analyst.analyze_portfolio(report)

        analysis = await loop.run_in_executor(None, _report_and_analyze)
        self._send(chat_id, f"🧠 *ניתוח AI — התיק שלי*\n{'─' * 20}\n{analysis}")

    async def _send_analysis(self, chat_id: int, symbol: str) -> None:
        if self._analyst is None:
            self._send(
                chat_id,
                "❌ ניתוח AI אינו מופעל.\n"
                "הגדר `ANTHROPIC_API_KEY` ב-`.env` ואפשר `ai.enabled: true` בקונפיגורציה.",
            )
            return
        self._send(chat_id, f"🤖 מנתח את *{symbol}* עם AI… (עשוי לקחת עד 30 שניות)")
        loop = asyncio.get_event_loop()
        # Both get_current_data and analyze are blocking — run both in the executor
        def _fetch_and_analyze() -> str:
            data = self._feed.get_current_data(symbol) or {}
            # When the user holds this stock, the analysis is tailored to their
            # entry price instead of being a generic rating.
            holding = portfolio_store.get(symbol)
            return self._analyst.analyze(symbol, data, holding=holding)

        analysis = await loop.run_in_executor(None, _fetch_and_analyze)
        self._send(chat_id, f"🧠 *ניתוח AI — {symbol}*\n{'─' * 20}\n{analysis}")
