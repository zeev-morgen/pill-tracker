"""AI stock analysis via Claude API (claude-opus-4-7), with Hebrew output."""

import logging
from typing import Optional

import anthropic
import yfinance as yf

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "אתה אנליסט מניות מקצועי שעונה אך ורק בעברית.\n"
    "הניתוח שלך צריך להיות תמציתי, מקצועי ומבוסס נתונים.\n\n"
    "מבנה התשובה:\n"
    "📰 *חדשות אחרונות*: 2-3 נקודות מרכזיות מהחדשות שסופקו\n"
    "📈 *מומנטום*: שינוי, נפח יחסית לממוצע, חוזק המגמה\n"
    "💹 *מכפילים*: P/E, שווי שוק, צמיחת הכנסות, מרווח גולמי\n"
    "⚠️ *סיכונים*: 1-2 סיכונים מרכזיים\n"
    "🎯 *המלצה*: קנייה חזקה / קנייה / החזקה / מכירה / הימנעות\n"
    "   ופרט בשורה אחת למה.\n\n"
    "חשוב: אל תמציא מידע. אם חסר נתון — כתוב 'אין נתון'.\n"
    "מגבלת אורך: עד 500 מילים. התאמה להודעת Telegram."
)


class StockAnalyst:
    """Generates a Hebrew AI analysis for a stock symbol using Claude."""

    def __init__(self, api_key: str, model: str = "claude-opus-4-7") -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model  = model

    def analyze(self, symbol: str, data: dict) -> str:
        """Return a Hebrew analysis string. Blocks until the full response arrives."""
        extra    = self._fetch_fundamentals(symbol)
        news     = self._fetch_news(symbol)
        momentum = self._fetch_momentum(symbol, data)
        prompt   = self._build_prompt(symbol, data, extra, news, momentum)
        try:
            with self._client.messages.stream(
                model=self._model,
                max_tokens=1500,
                thinking={"type": "adaptive"},
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                msg = stream.get_final_message()
            return next(
                (b.text for b in msg.content if b.type == "text"),
                "לא התקבל ניתוח.",
            )
        except Exception as exc:
            logger.error("AI analysis failed for %s: %s", symbol, exc)
            return f"❌ שגיאה בניתוח AI עבור {symbol}: {exc}"

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _fetch_fundamentals(self, symbol: str) -> dict:
        try:
            info = yf.Ticker(symbol).info
            return {
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
        except Exception:
            return {}

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
        self, symbol: str, data: dict, extra: dict, news: list, momentum: dict
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
            parts.append(f"• נפח יחסית לממוצע 30 יום: x{vva:.2f}")
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

        return "\n".join(parts)
