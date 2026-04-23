"""AI stock analysis via Claude API (claude-opus-4-7), with Hebrew output."""

import logging
from typing import Optional

import anthropic
import yfinance as yf

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "אתה אנליסט מניות מקצועי שעונה אך ורק בעברית.\n"
    "ניתוח שלך צריך להיות תמציתי, מקצועי ומבוסס נתונים.\n"
    "כלול:\n"
    "1. סיכום קצר של מצב המניה הנוכחי\n"
    "2. ניתוח טכני בסיסי (מגמה, נפח, רמות מחיר)\n"
    "3. נתוני יסוד עיקריים (P/E, שווי שוק, צמיחת הכנסות)\n"
    "4. זיהוי סיכונים ואפשרויות\n"
    "5. סיכום ומסקנה\n\n"
    "השב בצורה קצרה וישירה — מתאימה להודעת Telegram (עד 600 מילים)."
)


class StockAnalyst:
    """Generates a Hebrew AI analysis for a stock symbol using Claude."""

    def __init__(self, api_key: str, model: str = "claude-opus-4-7") -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model  = model

    def analyze(self, symbol: str, data: dict) -> str:
        """Return a Hebrew analysis string. Blocks until the full response arrives."""
        extra  = self._fetch_fundamentals(symbol)
        prompt = self._build_prompt(symbol, data, extra)
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
                "pe_ratio":       info.get("trailingPE"),
                "market_cap":     info.get("marketCap"),
                "revenue_growth": info.get("revenueGrowth"),
                "gross_margins":  info.get("grossMargins"),
                "analyst_rating": info.get("recommendationKey"),
                "target_price":   info.get("targetMeanPrice"),
                "sector":         info.get("sector"),
                "short_name":     info.get("shortName"),
            }
        except Exception:
            return {}

    @staticmethod
    def _fmt_cap(v: Optional[float]) -> str:
        if v is None:
            return "N/A"
        if v >= 1e12:
            return f"${v / 1e12:.1f}T"
        if v >= 1e9:
            return f"${v / 1e9:.1f}B"
        return f"${v / 1e6:.1f}M"

    def _build_prompt(self, symbol: str, data: dict, extra: dict) -> str:
        p    = data.get("price",      0.0)
        chg  = data.get("change_pct", 0.0)
        sc   = data.get("since_close_pct")
        vol  = data.get("volume",     0)
        hi   = data.get("day_high")
        lo   = data.get("day_low")
        sess = data.get("session",    "regular")

        rg = extra.get("revenue_growth")
        gm = extra.get("gross_margins")
        tp = extra.get("target_price")
        pe = extra.get("pe_ratio")

        parts = [
            f"נתח את המניה {symbol} ({extra.get('short_name', symbol)}).",
            "",
            "**נתונים עדכניים:**",
            f"• מחיר נוכחי: ${p:.2f}",
            f"• שינוי יומי: {chg:+.2f}%",
        ]
        if sc is not None:
            parts.append(f"• שינוי מסגירה: {sc:+.2f}%")
        if hi and lo:
            parts.append(f"• גבוה/נמוך יומי: ${hi:.2f} / ${lo:.2f}")
        parts.append(f"• נפח: {vol:,}")
        parts.append(f"• סשן: {sess}")
        parts += [
            "",
            "**נתוני יסוד:**",
            f"• P/E: {pe if pe is not None else 'N/A'}",
            f"• שווי שוק: {self._fmt_cap(extra.get('market_cap'))}",
            f"• צמיחת הכנסות: {f'{rg * 100:.1f}%' if rg is not None else 'N/A'}",
            f"• מרווח גולמי: {f'{gm * 100:.1f}%' if gm is not None else 'N/A'}",
            f"• מחיר יעד אנליסטים: {f'${tp:.2f}' if tp is not None else 'N/A'}",
            f"• המלצת אנליסטים: {extra.get('analyst_rating') or 'N/A'}",
            f"• סקטור: {extra.get('sector') or 'N/A'}",
        ]
        return "\n".join(parts)
