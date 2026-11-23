"""AI stock analysis via Claude API (claude-opus-4-7), with Hebrew output."""

import logging
from typing import Optional

import anthropic
import httpx
import urllib3
import yfinance as yf

from . import fundamentals, news
from .data_feed import MIN_SESSION_FRACTION, session_elapsed_fraction

logger = logging.getLogger(__name__)


#: Per-item blurb budget in the analysis prompt. Five items of a full feed
#: paragraph each would crowd out the fundamentals the same prompt carries.
_NEWS_BLURB_LIMIT = 280


def _news_block(result) -> str:
    """The prompt's news section, worded to match what we actually know.

    Three states, and the model is told which one it is in. Conflating the
    last two — as a bare empty list does — invites an analysis that reads as
    though the news was checked and found quiet.
    """
    items = getattr(result, "items", result) or []
    available = getattr(result, "available", True)

    if items:
        lines = [f"**חדשות אחרונות ({news.FRESH_WINDOW_HOURS} שעות):**"]
        for item in items:
            age = item.get("age_hours")
            when = f"לפני {age:.0f} ש׳ · " if age is not None else ""
            pub = f" — {item['publisher']}" if item.get("publisher") else ""
            lines.append(f"• {when}{item['title']}{pub}")
            # The feed's own paragraph, when it sent one. A headline says that
            # guidance was raised; the blurb says by how much, which is the
            # difference between an analysis that read the news and one that
            # read its title.
            blurb = (item.get("summary") or "").strip()
            if blurb:
                lines.append(f"  {blurb[:_NEWS_BLURB_LIMIT]}")
        return "\n".join(lines)

    if available:
        return (
            f"**חדשות אחרונות:** לא פורסמו כותרות ב-{news.FRESH_WINDOW_HOURS} "
            "השעות האחרונות (מקור החדשות נבדק והשיב)."
        )

    return (
        "**חדשות אחרונות:** מקור החדשות אינו זמין כרגע — אין לי כותרות לנתח. "
        "אל תתייחס לחדשות או לאירועים מהתקופה האחרונה כאילו בדקת אותם, "
        "וציין במפורש שהניתוח נשען על נתוני מחיר ופונדמנטלס בלבד."
    )


def friendly_error(exc: Exception) -> str:
    """What went wrong with the AI call, and what to do about it.

    The raw exception used to go straight onto the dashboard: a Python repr of
    a JSON error object, in English, ending in a request id. Every one of the
    common failures here has a specific and obvious remedy — top up the
    account, wait a minute, fix the key — and none of that survives being
    printed as a stack value. The unrecognised case still carries the class
    name, which is the part worth searching for.
    """
    text = str(exc).lower()

    if "credit balance is too low" in text or ("insufficient" in text and "credit" in text):
        return (
            "יתרת הקרדיט בחשבון Anthropic נגמרה. "
            "היכנס ל-console.anthropic.com ← Plans & Billing והוסף קרדיט. "
            "שאר הדשבורד ממשיך לעבוד — רק תכונות ה-AI מושבתות."
        )
    if "authentication" in text or "invalid x-api-key" in text or "401" in text:
        return (
            "מפתח ה-API של Anthropic נדחה. "
            "בדוק את ANTHROPIC_API_KEY במשתני הסביבה — ייתכן שהוחלף או פג."
        )
    if "rate limit" in text or "429" in text:
        return "יותר מדי בקשות ל-Anthropic ברצף. נסה שוב בעוד דקה."
    if "overloaded" in text or "529" in text:
        return "השירות של Anthropic עמוס כרגע. נסה שוב בעוד מספר דקות."
    if isinstance(exc, (anthropic.APIConnectionError, httpx.ConnectError)):
        return "לא ניתן להגיע ל-Anthropic. בדוק את החיבור לרשת."
    return f"שגיאה בפנייה ל-AI ({exc.__class__.__name__})."


_SYSTEM_PROMPT = (
    "אתה אנליסט מניות מקצועי שעונה אך ורק בעברית.\n"
    "הניתוח שלך צריך להיות תמציתי, מקצועי ומבוסס נתונים.\n\n"
    "מבנה התשובה:\n"
    "📰 *חדשות אחרונות*: 2-3 נקודות מרכזיות מהחדשות שסופקו\n"
    "📈 *מומנטום*: שינוי, נפח יחסית לממוצע ולקצב הצפוי לפי שעת המסחר, חוזק המגמה\n"
    "💹 *מכפילים*: P/E, שווי שוק, צמיחת הכנסות, מרווח גולמי\n"
    "⚠️ *סיכונים*: 1-2 סיכונים מרכזיים\n"
    "🎯 *המלצה*: קנייה חזקה / קנייה / החזקה / מכירה / הימנעות\n"
    "   ופרט בשורה אחת למה.\n\n"
    "אם סופקו נתוני פוזיציה אישית, הוסף לפני ההמלצה:\n"
    "💼 *הפוזיציה שלך*: השווה את הרווח/הפסד בפועל ממחיר הכניסה מול התמונה\n"
    "   הטכנית היום, וקבע האם התזה שהצדיקה את הכניסה עדיין תקפה.\n"
    "   ההמלצה חייבת להיות מותאמת לנקודת הכניסה הספציפית (הגדלה / החזקה /\n"
    "   צמצום / יציאה) ולא דירוג גנרי, כולל רמות מחיר שיצדיקו פעולה.\n\n"
    "חשוב: אל תמציא מידע. אם חסר נתון — כתוב 'אין נתון'.\n"
    "מגבלת אורך: עד 500 מילים. התאמה להודעת Telegram."
)

_PORTFOLIO_SYSTEM_PROMPT = (
    "אתה מנהל תיקים מקצועי שעונה אך ורק בעברית.\n"
    "אתה מנתח תיק השקעות כמכלול — לא מניה-מניה.\n\n"
    "מבנה התשובה:\n"
    "📊 *תמונת מצב*: שורה אחת על מצב התיק והתשואה הכוללת\n"
    "⚖️ *פיזור וריכוזיות*: האם התיק מפוזר נכון? איזה סקטור או מניה דומיננטיים מדי?\n"
    "🌪️ *סיכון ותנודתיות*: מה ה-ATR מלמד על רמת הסיכון בפועל\n"
    "🏆 *מובילים וגוררים*: הפוזיציות שתורמות ושפוגעות בתשואה\n"
    "🎯 *המלצות פעולה*: 2-4 צעדים קונקרטיים ומדורגים לפי חשיבות\n\n"
    "התייחס לגדלים היחסיים: פוזיציה של 2% מהתיק לא מצדיקה אותה תשומת לב\n"
    "כמו פוזיציה של 40%, גם אם התשואה עליה דרמטית יותר.\n"
    "אל תמציא מידע. אם חסר נתון — כתוב 'אין נתון'.\n"
    "מגבלת אורך: עד 450 מילים. זו תמיכה בקבלת החלטות, לא ייעוץ השקעות — "
    "ציין זאת במשפט אחד בסוף."
)


_CLOSED_POSITION_SYSTEM_PROMPT = (
    "אתה מאמן מסחר שמנתח עסקאות שנסגרו, ועונה אך ורק בעברית.\n"
    "המטרה: ללמוד מהעסקה, לא לחגוג אותה או לבקר אותה.\n\n"
    "החזר JSON תקין בלבד, ללא טקסט לפני או אחרי, במבנה:\n"
    '{\n'
    '  \"rating\": \"green\" | \"orange\" | \"red\",\n'
    '  \"explanation\": \"הסבר בעברית, 3-5 משפטים\"\n'
    '}\n\n'
    "קריטריון הדירוג — *איכות ההחלטה*, לא גודל הרווח:\n"
    "• green  — עסקה מנוהלת היטב: יחס סיכון/תשואה סביר, זמן החזקה שתואם\n"
    "           את התזה, יציאה מסודרת. גם הפסד קטן ומבוקר יכול להיות ירוק.\n"
    "• orange — תוצאה סבירה עם ליקוי בניהול: יציאה מוקדמת או מאוחרת מדי,\n"
    "           גודל פוזיציה לא פרופורציונלי, או החזקה ממושכת ללא תזה.\n"
    "• red    — ניהול לקוי: הפסד גדול שנתנו לו להתפתח, החזקה ארוכה בהפסד,\n"
    "           או סיכון שלא תאם את התנודתיות של המניה.\n\n"
    "בהסבר: ציין מה נעשה נכון, מה ניתן לשפר, ולקח אחד קונקרטי להמשך.\n"
    "אל תמציא נתונים שלא סופקו."
)


class StockAnalyst:
    """Generates a Hebrew AI analysis for a stock symbol using Claude."""

    def __init__(self, api_key: str, model: str = "claude-opus-4-7") -> None:
        self._api_key        = api_key
        self._model          = model
        self._client         = anthropic.Anthropic(api_key=api_key)
        self._insecure_client: Optional[anthropic.Anthropic] = None   # lazy SSL fallback

    def _get_insecure_client(self) -> anthropic.Anthropic:
        """Build a verify=False client for networks with TLS inspection (Kaspersky etc.)."""
        if self._insecure_client is None:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            self._insecure_client = anthropic.Anthropic(
                api_key=self._api_key,
                http_client=httpx.Client(verify=False, timeout=60.0),
            )
        return self._insecure_client

    def _stream_once(
        self,
        client: anthropic.Anthropic,
        prompt: str,
        system_prompt: str = _SYSTEM_PROMPT,
    ):
        with client.messages.stream(
            model=self._model,
            max_tokens=1500,
            thinking={"type": "adaptive"},
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            return stream.get_final_message()

    def summarize_news(
        self, system_prompt: str, prompt: str, max_tokens: int = 1400
    ) -> str:
        """Raw model output for a batch of headlines. Blocking.

        Deliberately thin: the wording, the batching and the parsing belong to
        :mod:`stock_monitor.news_digest`, and this is the one place that knows
        how to talk to the API — including the verify=False retry for networks
        with TLS inspection. No adaptive thinking: translating a supplied
        paragraph is not a reasoning task, and the latency lands in front of
        someone waiting in a chat.
        """
        def once(client: anthropic.Anthropic):
            return client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": prompt}],
            )

        try:
            msg = once(self._client)
        except (anthropic.APIConnectionError, httpx.ConnectError) as exc:
            logger.warning(
                "Anthropic API connection error (%s) — retrying with SSL verify=False.",
                exc.__class__.__name__,
            )
            msg = once(self._get_insecure_client())
        return next((b.text for b in msg.content if b.type == "text"), "")

    def analyze(self, symbol: str, data: dict, holding=None) -> str:
        """Return a Hebrew analysis string. Blocks until the full response arrives.

        ``holding`` is an optional store.Holding — when present, the user's
        actual position (quantity, entry price, live P/L) is injected into the
        prompt so the recommendation is tailored to that entry point.
        """
        # The live price lets a trailing P/E be derived from earnings per share
        # when no Yahoo endpoint will quote one.
        extra    = self._fetch_fundamentals(symbol, price=data.get("price"))
        headlines = self._fetch_news(symbol)
        momentum = self._fetch_momentum(symbol, data)
        prompt   = self._build_prompt(symbol, data, extra, headlines, momentum, holding)
        try:
            try:
                msg = self._stream_once(self._client, prompt)
            except (anthropic.APIConnectionError, httpx.ConnectError) as exc:
                logger.warning(
                    "Anthropic API connection error (%s) — retrying with SSL verify=False. "
                    "A proxy/antivirus is likely intercepting HTTPS.",
                    exc.__class__.__name__,
                )
                msg = self._stream_once(self._get_insecure_client(), prompt)

            return next(
                (b.text for b in msg.content if b.type == "text"),
                "לא התקבל ניתוח.",
            )
        except Exception as exc:
            logger.error("AI analysis failed for %s: %s", symbol, exc, exc_info=True)
            return f"❌ {friendly_error(exc)}"

    # ── Portfolio-level analysis ──────────────────────────────────────────────

    def analyze_portfolio(self, report: dict) -> str:
        """Analyze the portfolio as a whole from a portfolio_risk full_report().

        Looks at the positions together — concentration, volatility exposure,
        winners and losers — rather than rating each stock in isolation.
        """
        positions = report.get("positions") or []
        if not positions:
            return "אין פוזיציות בתיק לניתוח. הוסף פוזיציות בטאב \"התיק שלי\"."

        prompt = self._build_portfolio_prompt(report)
        try:
            try:
                msg = self._stream_once(self._client, prompt, _PORTFOLIO_SYSTEM_PROMPT)
            except (anthropic.APIConnectionError, httpx.ConnectError) as exc:
                logger.warning(
                    "Anthropic API connection error (%s) — retrying with SSL verify=False.",
                    exc.__class__.__name__,
                )
                msg = self._stream_once(
                    self._get_insecure_client(), prompt, _PORTFOLIO_SYSTEM_PROMPT
                )
            return next(
                (b.text for b in msg.content if b.type == "text"), "לא התקבל ניתוח."
            )
        except Exception as exc:
            logger.error("Portfolio AI analysis failed: %s", exc, exc_info=True)
            return f"❌ {friendly_error(exc)}"

    @staticmethod
    def _build_portfolio_prompt(report: dict) -> str:
        positions = report["positions"]
        volatility = report.get("volatility", {})
        sector = report.get("sector", {})
        allocation = report.get("allocation", {})

        parts = [
            "נתח את התיק הבא כמכלול.",
            "",
            f"**שווי תיק כולל:** ${report.get('total_value', 0):,.2f}",
            f"**רווח/הפסד כולל:** "
            f"{'+' if report.get('total_pnl_value', 0) >= 0 else '-'}"
            f"${abs(report.get('total_pnl_value', 0)):,.2f}",
            "",
            "**פוזיציות:**",
        ]
        for p in positions:
            atr = f"{p['atr_pct']:.2f}%" if p.get("atr_pct") is not None else "אין נתון"
            parts.append(
                f"• {p['ticker']}: {p['quantity']:g} מניות | כניסה ${p['entry_price']:.2f} "
                f"| נוכחי ${p['current_price']:.2f} | תשואה {p['pnl_pct']:+.2f}% "
                f"| שווי ${p['market_value']:,.2f} | ATR {atr} | סקטור {p['sector']}"
            )

        if sector.get("sectors"):
            parts += ["", "**פיזור סקטוריאלי:**"]
            for s in sector["sectors"]:
                parts.append(f"• {s['sector']}: {s['weight_pct']:.1f}% מהתיק")
            if sector.get("alert"):
                names = ", ".join(s["sector"] for s in sector.get("concentrated_sectors", []))
                parts.append(
                    f"⚠️ ריכוזיות מעל סף {sector.get('threshold_pct')}% בסקטור: {names}"
                )

        parts += [
            "",
            "**חשיפת תנודתיות:**",
            f"• {volatility.get('exposure_pct', 0):.1f}% משווי התיק במניות בעלות ATR גבוה "
            f"(סף: {volatility.get('threshold_pct')}%)",
        ]
        if volatility.get("high_volatility_positions"):
            names = ", ".join(
                f"{p['ticker']} ({p['atr_pct']:.1f}%)"
                for p in volatility["high_volatility_positions"]
            )
            parts.append(f"• מניות תנודתיות: {names}")

        if allocation.get("by_index"):
            parts += ["", "**חשיפה למדדים:**"]
            for i in allocation["by_index"]:
                parts.append(f"• {i['label']}: {i['weight_pct']:.1f}%")

        return "\n".join(parts)

    # ── Closed-position review (trade journal) ────────────────────────────────

    def review_closed_position(self, closed: dict) -> dict:
        """Grade a completed trade.

        Returns ``{"rating": "green|orange|red", "explanation": str}``. The
        model is asked for JSON so the rating can drive the traffic-light in the
        journal; if it answers with prose anyway we still keep the text and fall
        back to a neutral rating rather than losing the review.
        """
        prompt = self._build_closed_position_prompt(closed)
        try:
            try:
                msg = self._stream_once(self._client, prompt, _CLOSED_POSITION_SYSTEM_PROMPT)
            except (anthropic.APIConnectionError, httpx.ConnectError) as exc:
                logger.warning(
                    "Anthropic API connection error (%s) — retrying with SSL verify=False.",
                    exc.__class__.__name__,
                )
                msg = self._stream_once(
                    self._get_insecure_client(), prompt, _CLOSED_POSITION_SYSTEM_PROMPT
                )
            text = next((b.text for b in msg.content if b.type == "text"), "")
        except Exception as exc:
            logger.error("Closed-position review failed: %s", exc, exc_info=True)
            return {"rating": None, "explanation": f"❌ {friendly_error(exc)}"}
        return _parse_review(text)

    @staticmethod
    def _build_closed_position_prompt(closed: dict) -> str:
        parts = [
            f"נתח את העסקה הסגורה הבאה במניית {closed['ticker']}.",
            "",
            f"• כמות שנמכרה: {closed['quantity']:g}"
            + (" (מכירה חלקית)" if closed.get("is_partial") else " (יציאה מלאה)"),
            f"• מחיר כניסה: ${closed['entry_price']:.2f}",
            f"• מחיר יציאה: ${closed['exit_price']:.2f}",
            f"• תוצאה: {closed['pnl_pct']:+.2f}% "
            f"({'+' if closed['pnl_value'] >= 0 else '-'}${abs(closed['pnl_value']):,.2f})",
        ]
        if closed.get("holding_days") is not None:
            parts.append(f"• זמן החזקה: {closed['holding_days']} ימים")
        else:
            parts.append("• זמן החזקה: אין נתון (לא הוזן מועד רכישה)")
        if closed.get("atr_pct_at_close") is not None:
            parts.append(
                f"• תנודתיות המניה (ATR): {closed['atr_pct_at_close']:.2f}% מהמחיר — "
                f"השתמש בזה כדי לשפוט אם גודל התנועה חריג או שגרתי"
            )
        if closed.get("sector"):
            parts.append(f"• סקטור: {closed['sector']}")
        if closed.get("portfolio_weight_pct") is not None:
            parts.append(
                f"• משקל הפוזיציה בתיק בעת הפתיחה: כ-{closed['portfolio_weight_pct']:.1f}%"
            )
        parts += ["", "החזר JSON בלבד לפי המבנה שהוגדר."]
        return "\n".join(parts)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _fetch_fundamentals(self, symbol: str, price: Optional[float] = None) -> dict:
        """Valuation multiples and company facts, from whichever endpoint answers.

        The assembling lives in :mod:`stock_monitor.fundamentals`; see that
        module for why one endpoint was never going to be enough. The dict
        shape is unchanged, with two additions the prompt uses: ``_sources``
        and ``_growth_basis``.
        """
        figures = fundamentals.fetch(symbol, price=price)
        out = dict(figures.values)
        out["_sources"] = figures.sources
        out["_growth_basis"] = figures.growth_basis
        out["_any_data"] = figures.any_data
        return out

    def _fetch_news(self, symbol: str, limit: int = 5) -> news.NewsResult:
        """Recent headlines, with the provenance the prompt needs.

        The result distinguishes "nothing was published" from "no source
        answered". The old code could not: it called one endpoint and turned
        every outcome, failure included, into an empty list — which is how
        months of analyses came to carry no news at all without ever saying so.
        """
        return news.fetch(symbol, limit=limit)

    def _fetch_momentum(self, symbol: str, data: dict) -> dict:
        """Compute volume-vs-average and distance-from-52w metrics."""
        out = {}
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="30d", interval="1d")
            if not hist.empty:
                avg_vol = float(hist["Volume"].mean())
                cur_vol = data.get("volume") or float(hist["Volume"].iloc[-1])
                if avg_vol > 0:
                    out["volume_vs_avg"] = cur_vol / avg_vol
                    # Time-adjusted pace: how the volume so far compares to what
                    # would be expected by this point in the trading session, so
                    # a full day's volume done in 3 h reads as a clear spike.
                    frac = session_elapsed_fraction()
                    out["session_elapsed_pct"] = frac * 100.0
                    if 0.0 < frac < 1.0:
                        expected = avg_vol * max(frac, MIN_SESSION_FRACTION)
                        out["volume_vs_pace"] = cur_vol / expected
                pct_30d = (
                    (float(hist["Close"].iloc[-1]) - float(hist["Close"].iloc[0]))
                    / float(hist["Close"].iloc[0]) * 100.0
                )
                out["change_30d_pct"] = pct_30d
        except Exception:
            pass
        return out

    @staticmethod
    def _fmt_cap(v: Optional[float]) -> str:
        if v is None:
            # "אין נתון", like every other missing figure in the block: an
            # English "N/A" in a Hebrew list reads as a different kind of gap.
            return "אין נתון"
        if v >= 1e12:
            return f"${v / 1e12:.1f}T"
        if v >= 1e9:
            return f"${v / 1e9:.1f}B"
        return f"${v / 1e6:.1f}M"

    def _build_prompt(
        self,
        symbol: str,
        data: dict,
        extra: dict,
        headlines,                  # news.NewsResult, or a bare list of items
        momentum: dict,
        holding=None,
    ) -> str:
        p    = data.get("price",      0.0)
        chg  = data.get("change_pct", 0.0)
        sc   = data.get("since_close_pct")
        vol  = data.get("volume",     0)
        hi   = data.get("day_high")
        lo   = data.get("day_low")
        sess = data.get("session",    "regular")

        rg  = extra.get("revenue_growth")
        eg  = extra.get("earnings_growth")
        gm  = extra.get("gross_margins")
        pm  = extra.get("profit_margins")
        tp  = extra.get("target_price")
        pe  = extra.get("pe_ratio")
        fpe = extra.get("forward_pe")
        peg = extra.get("peg_ratio")
        h52 = extra.get("fifty_two_high")
        l52 = extra.get("fifty_two_low")

        vva = momentum.get("volume_vs_avg")
        vvp = momentum.get("volume_vs_pace")
        sep = momentum.get("session_elapsed_pct")
        p30 = momentum.get("change_30d_pct")

        parts = [
            f"נתח את המניה {symbol} ({extra.get('short_name', symbol)}).",
            "",
            "**נתונים עדכניים:**",
            f"• מחיר נוכחי: ${p:.2f}",
            f"• שינוי יומי: {chg:+.2f}%",
        ]
        if sc is not None:
            parts.append(f"• שינוי מסגירה: {sc:+.2f}%")
        if p30 is not None:
            parts.append(f"• שינוי 30 יום: {p30:+.2f}%")
        if hi and lo:
            parts.append(f"• גבוה/נמוך יומי: ${hi:.2f} / ${lo:.2f}")
        if h52 and l52:
            parts.append(f"• טווח 52 שבועות: ${l52:.2f} – ${h52:.2f}")
        parts.append(f"• נפח: {vol:,}")
        if vva is not None:
            parts.append(f"• נפח יחסית לממוצע 30 יום (יום מלא): x{vva:.2f}")
        if sep is not None and 0 < sep < 100:
            parts.append(f"• חלף מסשן המסחר הרגיל: {sep:.0f}%")
        if vvp is not None:
            parts.append(
                f"• נפח יחסית לקצב הצפוי לפי שעת המסחר: x{vvp:.2f} "
                f"(x1 = קצב רגיל; מעל x1 = מהיר מהרגיל לנקודת הזמן הזו ביום)"
            )
        parts.append(f"• סשן: {sess}")

        growth_basis = extra.get("_growth_basis")
        basis = f" ({growth_basis})" if growth_basis else ""
        parts += [
            "",
            "**מכפילים ויסוד:**",
            f"• P/E נוכחי: {f'{pe:.1f}' if pe is not None else 'אין נתון'}",
            f"• P/E עתידי: {f'{fpe:.1f}' if fpe is not None else 'אין נתון'}",
            f"• PEG: {f'{peg:.2f}' if peg is not None else 'אין נתון'}",
            f"• שווי שוק: {self._fmt_cap(extra.get('market_cap'))}",
            f"• צמיחת הכנסות{basis}: {f'{rg * 100:.1f}%' if rg is not None else 'אין נתון'}",
            f"• צמיחת רווחים{basis}: {f'{eg * 100:.1f}%' if eg is not None else 'אין נתון'}",
            f"• מרווח גולמי: {f'{gm * 100:.1f}%' if gm is not None else 'אין נתון'}",
            f"• מרווח נקי: {f'{pm * 100:.1f}%' if pm is not None else 'אין נתון'}",
            f"• מחיר יעד אנליסטים: {f'${tp:.2f}' if tp is not None else 'אין נתון'}",
            f"• המלצת אנליסטים: {extra.get('analyst_rating') or 'אין נתון'}",
            f"• סקטור: {extra.get('sector') or 'אין נתון'}",
        ]
        # The distinction the old prompt could not draw. A block of "אין נתון"
        # reads as a company without multiples — a loss-making one has no P/E,
        # after all — when in fact Yahoo refused to answer. Saying which it is
        # keeps the analysis from treating absent data as a finding.
        if "_any_data" in extra and not extra.get("_any_data"):
            parts.append(
                "⚠️ אף אחד ממקורות הנתונים לא השיב כרגע, ולכן כל השורות בסעיף "
                "הזה ריקות — זו תקלה בשליפה, לא היעדר מכפילים במניה. "
                "אל תסיק דבר מהיעדר הנתונים, וציין במפורש שהסעיף הזה חסר."
            )

        parts += ["", _news_block(headlines)]

        if holding is not None and p > 0:
            pnl_pct   = (p - holding.entry_price) / holding.entry_price * 100.0
            pnl_value = (p - holding.entry_price) * holding.quantity
            parts += [
                "",
                "**הפוזיציה האישית של המשתמש:**",
                f"• כמות מוחזקת: {holding.quantity:g}",
                f"• מחיר כניסה (הוזן ידנית): ${holding.entry_price:.2f}",
                f"• שווי פוזיציה נוכחי: ${p * holding.quantity:,.2f}",
                f"• רווח/הפסד בפועל: {pnl_pct:+.2f}% "
                f"({'+' if pnl_value >= 0 else '-'}${abs(pnl_value):,.2f})",
                "",
                "התייחס לפוזיציה הזו: השווה את הביצועים בפועל ממחיר הכניסה מול "
                "התמונה הטכנית היום, וספק המלצה המותאמת לנקודת הכניסה הזו.",
            ]
        else:
            parts += ["", "**הפוזיציה האישית של המשתמש:** אין פוזיציה במניה זו."]

        return "\n".join(parts)


def _parse_review(text: str) -> dict:
    """Extract {rating, explanation} from the model's reply.

    The prompt asks for bare JSON, but models occasionally wrap it in a code
    fence or add a sentence around it. Rather than discarding a perfectly good
    review over formatting, pull the first JSON object out of the text and fall
    back to keeping the prose with no rating.
    """
    import json
    import re

    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        braces = re.search(r"\{.*\}", candidate, re.DOTALL)
        if braces:
            candidate = braces.group(0)

    try:
        parsed = json.loads(candidate)
        rating = str(parsed.get("rating", "")).strip().lower()
        explanation = str(parsed.get("explanation", "")).strip()
        if rating not in ("green", "orange", "red"):
            logger.warning("Unexpected rating from the model: %r", rating)
            rating = None
        if explanation:
            return {"rating": rating, "explanation": explanation}
    except (ValueError, AttributeError) as exc:
        logger.warning("Could not parse the review as JSON (%s) — keeping the raw text", exc)

    return {"rating": None, "explanation": text.strip() or "לא התקבל ניתוח."}
