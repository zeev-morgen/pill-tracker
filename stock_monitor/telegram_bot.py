"""Telegram command bot — everything the dashboard does, in Hebrew, by tapping.

What you can send
─────────────────
A persistent keyboard sits under the message box, so the common things are one
tap rather than a remembered command:

    💼 התיק שלי   📋 מעקב
    📰 חדשות      🧾 סיכום
    🔔 התראות     ❓ עזרה

Typed equivalents all still work — ``/portfolio``, ``תיק``, ``/news``,
``סיכום``, ``/alerts`` — as does sending a bare ticker (``AVGO``, ``ESLT.TA``)
or ``ניתוח AVGO`` for an AI opinion.

Alerts, not just queries
────────────────────────
Alerts arrive from :mod:`stock_monitor.notifier` with two buttons on them:
analyse the move, or mute that symbol for an hour. 🔔 התראות shows what is
currently muted, what fired recently, and offers mute and unmute outright —
which is the whole difference between an alert stream you steer and one you
end up ignoring. See :mod:`stock_monitor.mute`.

The bot runs as a background asyncio task using long-polling.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pytz
import requests
import urllib3

from . import messages, mute, news
from .data_feed import StockDataFeed, get_market_session
from .messages import ltr, money, rtl
from .portfolio_risk import PortfolioRiskAnalyzer
from .store import alert_store, portfolio_store, watchlist_store

logger = logging.getLogger(__name__)
NYSE_TZ = pytz.timezone("America/New_York")

#: Tickers may carry an exchange suffix: ESLT.TA is as valid an input as AVGO.
_TICKER_RE  = re.compile(r"^[A-Z]{1,6}(?:\.[A-Z]{2})?$")
_ANALYZE_RE = re.compile(
    r"^(?:ANALYZE|ניתוח)\s+([A-Za-z]{1,6}(?:\.[A-Za-z]{2})?)$", re.IGNORECASE
)
#: Anything that is not a letter, digit, space or dot — emoji and punctuation —
#: so a tapped keyboard label ("💼 התיק שלי") routes like the typed command.
_DECORATION_RE = re.compile(r"[^\w\s.]", re.UNICODE)
#: Characters that make Telegram's legacy Markdown reject a whole message.
#: Stripped from text that came from elsewhere (headlines, mostly).
_MD_UNSAFE_RE = re.compile(r"[*_`\[\]]")

#: Keyboard label → the command it stands for. Typed forms share the table, so
#: there is one router and not two.
_COMMANDS: Dict[str, str] = {
    "START": "start", "שלום": "start", "HELLO": "start", "היי": "start",
    "HELP": "help", "עזרה": "help",
    "MENU": "menu", "תפריט": "menu",
    "STATUS": "status", "מעקב": "status", "רשימת מעקב": "status",
    "PORTFOLIO": "portfolio", "תיק": "portfolio", "התיק": "portfolio",
    "התיק שלי": "portfolio",
    "NEWS": "news", "חדשות": "news",
    "SUMMARY": "summary", "סיכום": "summary", "DIGEST": "summary",
    "ALERTS": "alerts", "התראות": "alerts", "MUTE": "alerts", "השתקה": "alerts",
}

MENU_KEYBOARD = {
    "keyboard": [
        [{"text": "💼 התיק שלי"}, {"text": "📋 מעקב"}],
        [{"text": "📰 חדשות"}, {"text": "🧾 סיכום"}],
        [{"text": "🔔 התראות"}, {"text": "❓ עזרה"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "שלח טיקר, למשל AVGO",
}

_SESSION_HE = {
    "pre":     "טרום מסחר 🌅",
    "regular": "מסחר רגיל 📈",
    "after":   "אחרי שעות המסחר 🌆",
    "closed":  "שוק סגור 🔒",
}

_HELP_TEXT = "\n".join([
    rtl("📋 *מה אפשר לעשות*"),
    "",
    rtl("הכפתורים שמתחת לשורת ההקלדה עושים את הכול — ואפשר גם להקליד:"),
    "",
    rtl(f"• טיקר כלשהו ← נתונים מלאים ({ltr('AVGO')}, {ltr('ESLT.TA')})"),
    rtl(f"• {ltr('`ניתוח AVGO`')} ← ניתוח AI של מניה"),
    rtl("• 💼 *התיק שלי* ← פוזיציות, שווי ורווח/הפסד"),
    rtl("• 📋 *מעקב* ← כל המניות ברשימת המעקב"),
    rtl("• 📰 *חדשות* ← כותרות אחרונות על המניות שבתיק"),
    rtl("• 🧾 *סיכום* ← תמונת מצב אחת: תיק, תנועות חריגות וחדשות"),
    rtl("• 🔔 *התראות* ← מה הושתק, מה נשלח לאחרונה, והשתקה בלחיצה"),
])


def _fmt_vol(v: int) -> str:
    if v >= 1_000_000_000:
        return f"{v/1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"{v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v/1_000:.0f}K"
    return str(v)


def _plain(text: str) -> str:
    """Text from outside, with the characters that break Markdown removed."""
    return _MD_UNSAFE_RE.sub("", str(text or "")).strip()


def route(text: str) -> Optional[str]:
    """Which command a message stands for, tapped or typed. None for neither."""
    stripped = _DECORATION_RE.sub(" ", text.lstrip("/")).strip()
    stripped = re.sub(r"\s+", " ", stripped)
    return _COMMANDS.get(stripped.upper()) or _COMMANDS.get(stripped)


def _pct_line(label: str, pct: Optional[float], price_delta: Optional[float] = None,
              currency: str = "USD") -> str:
    if pct is None:
        return ""
    arrow = "📈" if pct > 0 else ("📉" if pct < 0 else "➡️")
    delta = f" ({money(price_delta, currency)})" if price_delta is not None else ""
    return rtl(f"{arrow} {label}: {ltr(f'{pct:+.2f}%')}{delta}") + "\n"


def _format_stock(symbol: str, data: dict) -> str:
    """One stock's card, in the currency it is quoted in.

    A Tel Aviv quote arrives in agorot, and printing 10,500 agorot as
    ``$10,500`` — which this function used to do for every symbol — overstates
    a Teva share a hundredfold.
    """
    currency = data.get("currency") or ""
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
    ts     = datetime.now(NYSE_TZ).strftime("%H:%M")

    lines = [rtl(f"📊 *{ltr(symbol)}*") + "\n", rtl("─" * 18) + "\n\n"]
    lines.append(rtl(f"💰 מחיר: *{money(p, currency)}*") + "\n")

    if pc:
        lines.append(_pct_line("שינוי יומי", chg, p - pc, currency))

    if sc is not None and rc:
        lines.append(_pct_line("מהסגירה הרגילה", sc, p - rc, currency))
    elif fop is not None and sess == "regular":
        op = data.get("open_price")
        lines.append(_pct_line("מהפתיחה", fop, (p - op) if op else None, currency))

    if vol:
        lines.append(rtl(f"📦 מחזור: {ltr(_fmt_vol(vol))}") + "\n")
    if hi and lo:
        lines.append(rtl(f"📊 גבוה/נמוך: {money(hi, currency)} / {money(lo, currency)}") + "\n")

    lines.append("\n" + rtl(f"📡 מצב שוק: {_SESSION_HE.get(sess, sess)}") + "\n")
    lines.append(rtl(f"⏰ {ltr(ts)} ET"))
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
        news_monitor=None,                  # Optional[NewsMonitor] — shared cache
    ) -> None:
        self._base               = f"https://api.telegram.org/bot{bot_token}"
        self._feed               = data_feed
        self._config_syms        = [s.upper() for s in monitored_symbols]
        self._rc_ref             = regular_close_ref  # shared dict from StockMonitorApp
        self._analyst            = analyst
        self._risk               = PortfolioRiskAnalyzer(portfolio_store, data_feed=data_feed)
        self._authorized_chat_id = authorized_chat_id
        self._news               = news_monitor
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
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            resp = _post(f"{self._base}/sendMessage", json=payload)
            if resp.status_code == 400 and "parse" in resp.text.lower():
                # Legacy Markdown cannot be escaped, so a single stray asterisk
                # in a headline or in AI output rejects the message outright.
                logger.warning("Telegram rejected the Markdown — resending as plain text")
                payload.pop("parse_mode")
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

        command = route(text)
        analyze_match = _ANALYZE_RE.match(text)

        if command == "start":
            self._send(
                chat_id,
                "\n".join([
                    rtl("👋 *שלום!*"),
                    "",
                    rtl("אני בוט מעקב המניות שלך. הכפתורים למטה פתוחים תמיד — "
                        "ואפשר גם לשלוח טיקר ישירות."),
                    "",
                    _HELP_TEXT,
                ]),
                reply_markup=MENU_KEYBOARD,
            )
        elif command == "menu":
            self._send(chat_id, rtl("⌨️ התפריט למטה. בחר מה להציג."),
                       reply_markup=MENU_KEYBOARD)
        elif command == "help":
            self._send(chat_id, _HELP_TEXT, reply_markup=MENU_KEYBOARD)
        elif command == "status":
            await self._send_status(chat_id)
        elif command == "portfolio":
            await self._send_portfolio(chat_id)
        elif command == "news":
            await self._send_news(chat_id)
        elif command == "summary":
            await self._send_summary(chat_id)
        elif command == "alerts":
            await self._send_alerts(chat_id)
        elif analyze_match:
            await self._send_analysis(chat_id, analyze_match.group(1).upper())
        elif _TICKER_RE.match(text.lstrip("/").upper()):
            await self._send_ticker(chat_id, text.lstrip("/").upper())
        else:
            self._send(
                chat_id,
                rtl(f"❓ לא הבנתי את {ltr(f'`{_plain(text)}`')}.") + "\n\n"
                + rtl("נסה טיקר כמו `AVGO`, או בחר מהתפריט למטה."),
                reply_markup=MENU_KEYBOARD,
            )

    async def _send_ticker(self, chat_id: int, symbol: str) -> None:
        self._send(chat_id, rtl(f"🔍 מחפש נתונים עבור *{ltr(symbol)}*…"))
        data = await asyncio.to_thread(self._feed.get_current_data, symbol)
        if data is None:
            self._send(chat_id, rtl(f"❌ לא נמצאו נתונים עבור *{ltr(symbol)}*.") + "\n"
                       + rtl("בדוק שהטיקר נכון — לבורסה בתל אביב יש להוסיף סיומת `.TA`."))
            return
        # Enrich with cached regular close if available
        rc = self._rc_ref.get(symbol)
        if rc and data["session"] != "regular":
            data["regular_close"]    = rc
            data["since_close_pct"]  = (data["price"] - rc) / rc * 100.0

        # Inline buttons: the two things worth doing about a quote.
        buttons = [[{"text": "🧠 ניתוח AI + המלצה", "callback_data": f"analyze:{symbol}"}]] \
            if self._analyst is not None else []
        buttons.append([{"text": "📰 חדשות על המניה", "callback_data": f"news:{symbol}"}])
        self._send(chat_id, _format_stock(symbol, data),
                   reply_markup={"inline_keyboard": buttons})

    async def _handle_callback(self, cb: dict) -> None:
        """Process an inline-keyboard button press."""
        cb_id   = cb.get("id", "")
        chat_id = cb.get("message", {}).get("chat", {}).get("id")
        data    = cb.get("data", "")

        # Authorization check
        if self._authorized_chat_id and str(chat_id) != self._authorized_chat_id:
            self._answer_callback(cb_id)
            return

        if data.startswith("mute:"):
            # Silencing has to feel instant, so it happens before the ack.
            _, symbol, minutes = (data.split(":") + ["60"])[:3]
            self._mute(chat_id, symbol, int(minutes))
            self._answer_callback(cb_id, "הושתק")
            return
        if data.startswith("unmute:"):
            symbol = data.split(":", 1)[1]
            mute.unmute(symbol)
            self._answer_callback(cb_id, "ההשתקה בוטלה")
            await self._send_alerts(chat_id)
            return
        if data == "mute_today":
            self._mute_until_tomorrow(chat_id)
            self._answer_callback(cb_id, "הושתק עד מחר")
            return
        if data == "unmute_all":
            mute.unmute_everything()
            self._answer_callback(cb_id, "כל ההשתקות בוטלו")
            self._send(chat_id, rtl("🔔 ההשתקות בוטלו — ההתראות חזרו לפעול."))
            return

        self._answer_callback(cb_id, "מתחיל…")

        if data == "analyze_portfolio":
            await self._send_portfolio_analysis(chat_id)
        elif data.startswith("analyze:"):
            await self._send_analysis(chat_id, data.split(":", 1)[1].upper())
        elif data.startswith("news:"):
            await self._send_news(chat_id, data.split(":", 1)[1].upper())

    async def _send_status(self, chat_id: int) -> None:
        if not self._syms:
            self._send(chat_id, rtl("אין מניות במעקב.") + "\n"
                       + rtl("הוסף מניות בדשבורד — טאב *מעקב חי*."))
            return
        session = _SESSION_HE.get(get_market_session(), get_market_session())
        lines = [rtl(f"📋 *רשימת מעקב* — {session}"), ""]
        quotes = await asyncio.to_thread(self._feed.get_current_data_batch, list(self._syms))
        for sym in self._syms:
            data = quotes.get(sym)
            if data:
                chg = data.get("change_pct", 0)
                icon = "🟢" if chg >= 0 else "🔴"
                lines.append(rtl(
                    f"{icon} *{ltr(sym)}* {money(data['price'], data.get('currency'))} "
                    f"({ltr(f'{chg:+.2f}%')})"
                ))
            else:
                lines.append(rtl(f"⚪ *{ltr(sym)}* — אין נתון"))
        self._send(chat_id, "\n".join(lines))

    # ── Portfolio ─────────────────────────────────────────────────────────────

    async def _send_portfolio(self, chat_id: int) -> None:
        """Portfolio status with an inline button for the AI review."""
        self._send(chat_id, rtl("📂 טוען את התיק…"))
        # full_report() hits yfinance once per holding — keep it off the loop.
        report = await asyncio.to_thread(self._risk.full_report)

        positions = report.get("positions") or []
        if not positions:
            self._send(
                chat_id,
                rtl("התיק ריק.") + "\n" + rtl("הוסף פוזיציות בדשבורד — טאב *התיק שלי*."),
            )
            return

        self._send(chat_id, "\n".join(self._portfolio_lines(report)),
                   reply_markup=self._portfolio_buttons())

    def _portfolio_lines(self, report: dict) -> List[str]:
        """The portfolio block, shared by 💼 התיק שלי and 🧾 סיכום."""
        positions = report.get("positions") or []
        pnl = report.get("total_pnl_value", 0.0)
        lines = [
            rtl("💼 *התיק שלי*"),
            rtl(f"שווי כולל: *{money(report.get('total_value', 0.0))}*"),
            rtl(f"{'🟢' if pnl >= 0 else '🔴'} רווח/הפסד: *{money(pnl)}*"),
            "",
        ]
        for p in sorted(positions, key=lambda x: x["market_value"], reverse=True):
            icon = "🟢" if p["pnl_pct"] >= 0 else "🔴"
            currency = p.get("currency") or ""
            atr_pct = p.get("atr_pct")
            atr = f" · {ltr('ATR %.1f%%' % atr_pct)}" if atr_pct is not None else ""
            change = ltr("%+.2f%%" % p["pnl_pct"])
            quantity = ltr("%g" % p["quantity"])
            lines.append(rtl(f"{icon} *{ltr(p['ticker'])}* {change}{atr}"))
            lines.append(rtl(
                f"   {quantity} × {money(p['current_price'], currency)} "
                f"= {money(p['market_value'])}"
            ))

        warnings = []
        if report.get("sector", {}).get("alert"):
            names = ", ".join(
                s["sector"] for s in report["sector"].get("concentrated_sectors", [])
            )
            warnings.append(rtl(f"⚠️ ריכוזיות סקטוריאלית: {_plain(names)}"))
        if report.get("volatility", {}).get("alert"):
            exposure = ltr("%.0f%%" % report["volatility"]["exposure_pct"])
            warnings.append(rtl(f"⚠️ חשיפה לתנודתיות גבוהה: {exposure} מהתיק"))
        if report.get("feed_lag_days"):
            warnings.append(rtl(
                f"🕒 הנתונים מאחרים ב-{ltr(report['feed_lag_days'])} ימי מסחר"
            ))
        if warnings:
            lines += [""] + warnings
        return lines

    def _portfolio_buttons(self) -> Optional[dict]:
        if self._analyst is None:
            return None
        return {"inline_keyboard": [[
            {"text": "🧠 ניתוח AI של התיק", "callback_data": "analyze_portfolio"}
        ]]}

    async def _send_portfolio_analysis(self, chat_id: int) -> None:
        if self._analyst is None:
            self._send(chat_id, self._ai_disabled_text())
            return
        self._send(chat_id, rtl("🤖 מנתח את התיק כמכלול… (עשוי לקחת עד דקה)"))

        def _report_and_analyze() -> str:
            report = self._risk.full_report()
            return self._analyst.analyze_portfolio(report)

        analysis = await asyncio.to_thread(_report_and_analyze)
        self._send(chat_id, rtl("🧠 *ניתוח AI — התיק שלי*") + "\n"
                   + rtl("─" * 18) + "\n" + analysis)

    async def _send_analysis(self, chat_id: int, symbol: str) -> None:
        if self._analyst is None:
            self._send(chat_id, self._ai_disabled_text())
            return
        self._send(chat_id, rtl(f"🤖 מנתח את *{ltr(symbol)}* עם AI… (עד 30 שניות)"))

        # Both get_current_data and analyze are blocking — run both in a thread
        def _fetch_and_analyze() -> str:
            data = self._feed.get_current_data(symbol) or {}
            # When the user holds this stock, the analysis is tailored to their
            # entry price instead of being a generic rating.
            holding = portfolio_store.get(symbol)
            return self._analyst.analyze(symbol, data, holding=holding)

        analysis = await asyncio.to_thread(_fetch_and_analyze)
        self._send(chat_id, rtl(f"🧠 *ניתוח AI — {ltr(symbol)}*") + "\n"
                   + rtl("─" * 18) + "\n" + analysis)

    @staticmethod
    def _ai_disabled_text() -> str:
        return "\n".join([
            rtl("❌ ניתוח AI אינו מופעל."),
            rtl(f"הגדר {ltr('`ANTHROPIC_API_KEY`')} ואפשר {ltr('`ai.enabled: true`')} "
                "בקונפיגורציה."),
        ])

    # ── News ──────────────────────────────────────────────────────────────────

    async def _send_news(self, chat_id: int, symbol: str = "") -> None:
        """Recent headlines — for one symbol, or for everything in the portfolio."""
        if symbol:
            self._send(chat_id, rtl(f"📰 מחפש חדשות על *{ltr(symbol)}*…"))
            result = await asyncio.to_thread(news.fetch, symbol, 5)
            self._send(chat_id, self._news_text(
                f"📰 *חדשות — {ltr(symbol)}*", result.items, result.available
            ))
            return

        self._send(chat_id, rtl("📰 אוסף כותרות…"))
        monitor = self._news
        if monitor is None:
            from .news_monitor import NewsMonitor
            monitor = self._news = NewsMonitor(portfolio_store)
        snapshot = await asyncio.to_thread(monitor.snapshot, 30)
        items = snapshot.get("items") or []
        # Unavailable only if no ticker's feed answered — one broken ticker
        # among several is not a feed outage.
        unavailable = snapshot.get("unavailable") or []
        available = bool(snapshot.get("sources")) or not unavailable
        text = self._news_text(
            f"📰 *חדשות על התיק* (עד {ltr(news.FRESH_WINDOW_HOURS)} שעות)",
            items, available,
        )
        self._send(chat_id, text)

    def _news_text(self, header: str, items: list, available: bool) -> str:
        lines = [rtl(header), ""]
        if items:
            for item in items[:10]:
                age = item.get("age_hours")
                when = f"לפני {ltr(f'{age:.0f}')} ש׳ · " if age is not None else ""
                publisher = _plain(item.get("publisher") or "")
                title = _plain(item.get("title"))
                link = item.get("link") or ""
                headline = f"[{title}]({link})" if link else title
                lines.append(rtl(f"• {when}*{ltr(item['ticker'])}* — {headline}"))
                if publisher:
                    lines.append(rtl(f"   {ltr(publisher)}"))
            return "\n".join(lines)

        if available:
            lines.append(rtl(
                f"אין כותרות חדשות ב-{ltr(news.FRESH_WINDOW_HOURS)} השעות האחרונות."
            ))
        else:
            # The failure that went unnoticed for a month: an empty news feed
            # looked exactly like a quiet day. Now it says which it is.
            lines.append(rtl("⚠️ מקור החדשות אינו זמין כרגע — זו תקלה בשירות, "
                             "לא יום שקט. נסה שוב בעוד כמה דקות."))
        return "\n".join(lines)

    # ── Daily digest ──────────────────────────────────────────────────────────

    async def _send_summary(self, chat_id: int) -> None:
        """One message with the day in it: portfolio, movers, news, mutes."""
        self._send(chat_id, rtl("🧾 מכין סיכום…"))
        report = await asyncio.to_thread(self._risk.full_report)

        lines = [rtl(f"🧾 *סיכום — {ltr(datetime.now(NYSE_TZ).strftime('%d/%m/%Y'))}*"), ""]
        if report.get("positions"):
            lines += self._portfolio_lines(report)
        else:
            lines.append(rtl("💼 התיק ריק."))

        positions = report.get("positions") or []
        # With three holdings or fewer the list above already is the movers;
        # repeating it would pad the digest rather than add to it.
        movers = sorted(
            positions, key=lambda p: abs(p.get("pnl_pct") or 0), reverse=True,
        )[:3] if len(positions) > 3 else []
        if movers:
            lines += ["", rtl("📊 *התנועות הגדולות בתיק*")]
            for p in movers:
                icon = "🟢" if p["pnl_pct"] >= 0 else "🔴"
                change = ltr("%+.2f%%" % p["pnl_pct"])
                lines.append(rtl(f"{icon} {ltr(p['ticker'])} {change}"))

        monitor = self._news
        if monitor is not None:
            snapshot = await asyncio.to_thread(monitor.snapshot, 60)
            items = snapshot.get("items") or []
            if items:
                # "1 כותרות" is not Hebrew; one headline takes the singular.
                count_he = ("כותרת חדשה אחת" if len(items) == 1
                            else f"{ltr(len(items))} כותרות חדשות")
                lines += ["", rtl(f"📰 *{count_he}* — שלח 📰 חדשות לפירוט")]
                for item in items[:3]:
                    lines.append(rtl(f"• *{ltr(item['ticker'])}* — {_plain(item['title'])}"))
            elif snapshot.get("unavailable") and not snapshot.get("sources"):
                lines += ["", rtl("📰 מקור החדשות אינו זמין כרגע.")]

        muted = mute.active()
        if muted:
            lines += ["", rtl("🔕 *השתקות פעילות:* ") + self._mute_summary(muted)]

        self._send(chat_id, "\n".join(lines), reply_markup=self._portfolio_buttons())

    async def daily_digest(self) -> None:
        """Scheduler entry point: push the summary once a day, unprompted.

        The point of the whole exercise — alerts you do not have to ask for.
        Silent when no chat is configured, since there is nowhere to send it.
        """
        if not self._authorized_chat_id:
            logger.info("Daily digest skipped: no authorized chat_id configured")
            return
        try:
            await self._send_summary(int(self._authorized_chat_id))
        except Exception as exc:
            logger.error("Daily digest failed: %s", exc, exc_info=True)

    # ── Alerts and muting ─────────────────────────────────────────────────────

    @staticmethod
    def _local(deadline: datetime) -> str:
        return deadline.astimezone(NYSE_TZ).strftime("%H:%M")

    def _mute_summary(self, muted: Dict[str, datetime]) -> str:
        parts = []
        for symbol, deadline in muted.items():
            name = "הכול" if symbol == mute.ALL else ltr(symbol)
            parts.append(f"{name} עד {ltr(self._local(deadline))} ET")
        return " · ".join(parts)

    def _mute(self, chat_id: int, symbol: str, minutes: int) -> None:
        deadline = mute.mute(symbol, minutes)
        name = "כל ההתראות" if symbol == mute.ALL else f"*{ltr(symbol)}*"
        self._send(chat_id, rtl(
            f"🔕 {name} בהשתקה עד {ltr(self._local(deadline))} ET."
        ))

    def _mute_until_tomorrow(self, chat_id: int) -> None:
        """Quiet for the rest of the day: until 09:00 ET tomorrow, before the open."""
        now = datetime.now(NYSE_TZ)
        tomorrow = (now + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        deadline = mute.mute_until(tomorrow, mute.ALL)
        self._send(chat_id, rtl(
            f"🔕 כל ההתראות מושתקות עד מחר ב-{ltr(self._local(deadline))} ET "
            "(לפני פתיחת המסחר)."
        ))

    async def _send_alerts(self, chat_id: int) -> None:
        """What is muted, what fired recently, and the buttons to change it."""
        lines = [rtl("🔔 *התראות*"), ""]

        muted = mute.active()
        if muted:
            lines.append(rtl("🔕 מושתק: ") + self._mute_summary(muted))
        else:
            lines.append(rtl("🔔 כל ההתראות פעילות."))

        recent = await asyncio.to_thread(alert_store.recent, 5)
        if recent:
            lines += ["", rtl("📜 *נשלחו לאחרונה*")]
            for record in recent:
                lines.append(rtl(
                    f"• {ltr(record.timestamp)} *{ltr(record.symbol)}* — "
                    f"{_plain(record.message)}"
                ))
        else:
            lines += ["", rtl("עדיין לא נשלחו התראות.")]

        buttons = [
            [{"text": "🔕 השתק הכול לשעה", "callback_data": f"mute:{mute.ALL}:60"},
             {"text": "🌙 השתק עד מחר", "callback_data": "mute_today"}],
        ]
        if muted:
            buttons.append([{"text": "🔔 בטל את כל ההשתקות", "callback_data": "unmute_all"}])
        self._send(chat_id, "\n".join(lines),
                   reply_markup={"inline_keyboard": buttons})
