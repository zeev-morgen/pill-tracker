"""AI stock analysis via Claude API (claude-opus-4-7), with Hebrew output."""

import logging
import math
from typing import Optional

import anthropic
import httpx
import urllib3
import yfinance as yf

from . import reference_data
from .data_feed import MIN_SESSION_FRACTION, session_elapsed_fraction

logger = logging.getLogger(__name__)


def _fast_get(fast_info, *names):
    """Read a fast_info field; its key naming has changed across versions.

    NaN counts as missing. yfinance hands back float('nan') for fields it could
    not resolve, and NaN is truthy — left alone it reaches the prompt as a
    literal "nan", which is worse than saying the figure is unavailable.
    """
    for name in names:
        try:
            value = getattr(fast_info, name, None)
            if value is None and hasattr(fast_info, "get"):
                value = fast_info.get(name)
            if value is not None and math.isfinite(float(value)):
                return float(value)
        except Exception:
            continue
    return None


def _finite_or_none(value):
    """Drop NaN / infinity so the prompt says 'אין נתון' instead of 'nan'."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if math.isfinite(value) else None
    return value

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

    def analyze(self, symbol: str, data: dict, holding=None) -> str:
        """Return a Hebrew analysis string. Blocks until the full response arrives.

        ``holding`` is an optional store.Holding — when present, the user's
        actual position (quantity, entry price, live P/L) is injected into the
        prompt so the recommendation is tailored to that entry point.
        """
        extra    = self._fetch_fundamentals(symbol)
        news     = self._fetch_news(symbol)
        momentum = self._fetch_momentum(symbol, data)
        prompt   = self._build_prompt(symbol, data, extra, news, momentum, holding)
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
            return f"❌ שגיאה בניתוח AI עבור {symbol}: {exc}"

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
            return f"❌ שגיאה בניתוח התיק: {exc}"

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
            return {"rating": None, "explanation": f"❌ שגיאה בניתוח העסקה: {exc}"}
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

    def _fetch_fundamentals(self, symbol: str) -> dict:
        """Valuation multiples and company facts.

        yfinance's ``.info`` is the only source for multiples but is unreliable
        — it regularly returns an empty payload or raises, which used to be
        swallowed silently and rendered as 'אין נתון' across the whole section
        with no way to tell a genuinely missing figure from a failed fetch.
        So: retry once, fall back to ``fast_info`` for the fields it carries,
        and fall back to the curated table for the sector.
        """
        info: dict = {}
        for attempt in (1, 2):
            try:
                info = yf.Ticker(symbol).info or {}
                if info:
                    break
                logger.warning(
                    "Empty fundamentals for %s (attempt %d/2)", symbol, attempt
                )
            except Exception as exc:
                logger.warning(
                    "Fundamentals fetch failed for %s (attempt %d/2): %s",
                    symbol, attempt, exc,
                )

        out = {
            "pe_ratio":            info.get("trailingPE"),
            "forward_pe":          info.get("forwardPE"),
            "peg_ratio":           info.get("pegRatio"),
            "market_cap":          info.get("marketCap"),
            "revenue_growth":      info.get("revenueGrowth"),
            "earnings_growth":     info.get("earningsGrowth"),
            "gross_margins":       info.get("grossMargins"),
            "profit_margins":      info.get("profitMargins"),
            "analyst_rating":      info.get("recommendationKey"),
            "target_price":        info.get("targetMeanPrice"),
            "fifty_two_high":      info.get("fiftyTwoWeekHigh"),
            "fifty_two_low":       info.get("fiftyTwoWeekLow"),
            "sector":              info.get("sector"),
            "short_name":          info.get("shortName"),
        }

        # fast_info is a separate, lighter endpoint that often succeeds when
        # .info does not. It cannot supply multiples, but it does carry market
        # cap and the 52-week range.
        if out["market_cap"] is None or out["fifty_two_high"] is None:
            try:
                fast = yf.Ticker(symbol).fast_info
                out["market_cap"] = out["market_cap"] or _fast_get(fast, "market_cap", "marketCap")
                out["fifty_two_high"] = out["fifty_two_high"] or _fast_get(fast, "year_high", "yearHigh")
                out["fifty_two_low"] = out["fifty_two_low"] or _fast_get(fast, "year_low", "yearLow")
            except Exception as exc:
                logger.debug("fast_info fallback failed for %s: %s", symbol, exc)

        # .info can also carry NaN for a field Yahoo has no value for.
        out = {key: _finite_or_none(value) for key, value in out.items()}

        if not out["sector"]:
            out["sector"] = reference_data.lookup_sector(symbol)
        if not out["short_name"]:
            out["short_name"] = symbol

        missing = [k for k in ("pe_ratio", "market_cap", "profit_margins") if out[k] is None]
        if missing:
            logger.info(
                "Fundamentals partially unavailable for %s (missing: %s) — "
                "the analysis will report 'אין נתון' for those",
                symbol, ", ".join(missing),
            )
        return out

    def _fetch_news(self, symbol: str, limit: int = 5) -> list:
        """Return a list of recent news headlines (title + publisher)."""
        try:
            items = yf.Ticker(symbol).news or []
        except Exception:
            return []
        out = []
        for item in items[:limit]:
            # yfinance news item formats vary across versions
            content = item.get("content") or item
            title     = content.get("title") or item.get("title")
            publisher = (
                content.get("provider", {}).get("displayName")
                if isinstance(content.get("provider"), dict)
                else content.get("publisher") or item.get("publisher")
            )
            if title:
                out.append({"title": title, "publisher": publisher or ""})
        return out

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
            return "N/A"
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
        news: list,
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

        parts += [
            "",
            "**מכפילים ויסוד:**",
            f"• P/E נוכחי: {f'{pe:.1f}' if pe is not None else 'אין נתון'}",
            f"• P/E עתידי: {f'{fpe:.1f}' if fpe is not None else 'אין נתון'}",
            f"• PEG: {f'{peg:.2f}' if peg is not None else 'אין נתון'}",
            f"• שווי שוק: {self._fmt_cap(extra.get('market_cap'))}",
            f"• צמיחת הכנסות: {f'{rg * 100:.1f}%' if rg is not None else 'אין נתון'}",
            f"• צמיחת רווחים: {f'{eg * 100:.1f}%' if eg is not None else 'אין נתון'}",
            f"• מרווח גולמי: {f'{gm * 100:.1f}%' if gm is not None else 'אין נתון'}",
            f"• מרווח נקי: {f'{pm * 100:.1f}%' if pm is not None else 'אין נתון'}",
            f"• מחיר יעד אנליסטים: {f'${tp:.2f}' if tp is not None else 'אין נתון'}",
            f"• המלצת אנליסטים: {extra.get('analyst_rating') or 'אין נתון'}",
            f"• סקטור: {extra.get('sector') or 'אין נתון'}",
        ]

        if news:
            parts += ["", "**חדשות אחרונות (כותרות):**"]
            for item in news:
                pub = f" — {item['publisher']}" if item.get("publisher") else ""
                parts.append(f"• {item['title']}{pub}")
        else:
            parts += ["", "**חדשות אחרונות:** אין נתון"]

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
