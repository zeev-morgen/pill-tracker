"""Alerts the user asks for in their own words.

    "תתריע לי כשמניית ANT עולה ב-3% מנקודת הפתיחה, ולא מנקודת השפל"
    "תעדכן אותי כשמניית ORCL חוצה כלפי מעלה את ממוצע 150 יום"

The model turns a sentence like that into one of a small number of rule kinds,
and the rest of this module is about the two things that decide whether such a
feature is trustworthy or merely impressive.

**The parse is confirmed before it is saved.** A misread request is worse than
a refused one: an alert that never fires means a move missed in silence, and
one that fires on the wrong condition teaches you to ignore the channel. So
the parsed rule is rendered back into Hebrew — the figure, the direction, the
reference point — and nothing is stored until the user says yes. The sentence
they typed is stored alongside the rule, because it is the only record of what
they meant rather than of what was understood.

**A condition is not a crossing.** "Crosses above the 150-day average" is an
event: it needs the previous side to have been below, which means state and
means the first observation can only record a side, never fire. Otherwise a
rule created while the price is already above its average fires at once and
says a crossing happened when nothing moved. The same goes for a price level.
Percentage-from-open rules are genuinely conditions and are rate-limited by a
cooldown instead, a day by default, so a stock sitting 3% up does not report
it every minute.

The reference point matters as much as the number: the user's own example asks
for 3% *from the open*, not from the session's low, which is what the built-in
rolling-window rule measures. Hence separate kinds, each naming its baseline
in the alert it sends.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from . import fx, messages
from .messages import ltr, money

logger = logging.getLogger(__name__)

#: Rule kinds, with the parameters each one takes. The model may use only
#: these; anything else is refused rather than approximated.
KINDS: Dict[str, Dict] = {
    "pct_from_open": {
        "params": ("direction", "threshold_pct"),
        "he": "שינוי באחוזים מנקודת הפתיחה של היום",
    },
    "pct_from_close": {
        "params": ("direction", "threshold_pct"),
        "he": "שינוי באחוזים מהסגירה של אתמול",
    },
    "price_level": {
        "params": ("direction", "price"),
        "he": "חציית מחיר מוחלט",
    },
    "ma_cross": {
        "params": ("direction", "days"),
        "he": "חציית ממוצע נע",
    },
    "volume_multiple": {
        "params": ("multiple",),
        "he": "מחזור חריג ביחס לממוצע",
    },
}

CHANNELS = ("telegram", "email", "both")
DIRECTIONS = ("up", "down")

#: Bounds that keep a misparse from becoming a rule that can never fire, or
#: one that fires constantly. A 0.01% move is noise; 500% is not an alert.
MIN_PCT, MAX_PCT = 0.1, 500.0
#: Two days is the shortest average worth the name; Yahoo's daily history runs
#: out well before 400 sessions for most listings.
MIN_MA_DAYS, MAX_MA_DAYS = 2, 400
MIN_MULTIPLE, MAX_MULTIPLE = 1.1, 50.0
DEFAULT_COOLDOWN_MINUTES = 1440

#: Phrases that mark a message as a request for an alert rather than a command
#: or a ticker. Checked only after the command router has had its turn.
REQUEST_HINTS = (
    "תתריע", "תתריעי", "התרע", "תעדכן", "עדכן אותי", "תודיע", "הודע לי",
    "תשלח לי", "שלח לי התראה", "כשהמניה", "כשמניית", "alert me", "notify me",
    "let me know", "tell me when",
)


def available_kinds_he() -> str:
    """What can be asked for, listed from the kinds the engine can evaluate.

    Generated rather than written out, so the help text cannot drift away from
    what the evaluator actually supports — which would mean inviting a request
    the system then refuses.
    """
    return " · ".join(spec["he"] for spec in KINDS.values())


def looks_like_request(text: str) -> bool:
    """True when a message reads as "tell me when…" rather than as a command.

    Checked only after the command router and the ticker pattern have had
    their turn, so a bare ``AVGO`` is still a quote request. Deliberately
    narrow: a sentence nobody meant as an alert should get the help message,
    not a trip through the model.
    """
    lowered = (text or "").strip().lower()
    if len(lowered) < 8:
        return False
    return any(hint.lower() in lowered for hint in REQUEST_HINTS)


class RuleError(ValueError):
    """The request could not be turned into a rule that would work."""


# ── The rule ──────────────────────────────────────────────────────────────────

@dataclass
class CustomAlert:
    symbol: str
    kind: str
    params: Dict = field(default_factory=dict)
    channel: str = "telegram"
    #: The sentence the user typed. Kept verbatim — it is the record of intent.
    request: str = ""
    enabled: bool = True
    #: True when the phrasing was explicitly one-time; the rule disables itself
    #: after firing instead of re-arming.
    once: bool = False
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES
    id: Optional[int] = None
    created_at: Optional[datetime] = None
    last_fired_at: Optional[datetime] = None

    # -- validation ---------------------------------------------------------

    def validate(self) -> "CustomAlert":
        """Raise RuleError unless this rule could actually fire as written."""
        if self.kind not in KINDS:
            raise RuleError(f"סוג התראה לא מוכר: {self.kind}")
        if self.channel not in CHANNELS:
            raise RuleError(f"ערוץ לא מוכר: {self.channel}")
        if not self.symbol:
            raise RuleError("חסר טיקר")

        needs_direction = "direction" in KINDS[self.kind]["params"]
        if needs_direction and self.params.get("direction") not in DIRECTIONS:
            raise RuleError("חסר כיוון (עלייה או ירידה)")

        if self.kind in ("pct_from_open", "pct_from_close"):
            self._bounded("threshold_pct", MIN_PCT, MAX_PCT, "אחוז")
        elif self.kind == "price_level":
            price = self._number("price")
            if price <= 0:
                raise RuleError("מחיר היעד חייב להיות חיובי")
        elif self.kind == "ma_cross":
            days = self._number("days")
            if not float(days).is_integer():
                raise RuleError("אורך הממוצע הנע חייב להיות מספר שלם של ימים")
            self._bounded("days", MIN_MA_DAYS, MAX_MA_DAYS, "ימים")
            self.params["days"] = int(days)
        elif self.kind == "volume_multiple":
            self._bounded("multiple", MIN_MULTIPLE, MAX_MULTIPLE, "מכפיל")

        if self.cooldown_minutes < 1:
            self.cooldown_minutes = DEFAULT_COOLDOWN_MINUTES
        return self

    def _number(self, name: str) -> float:
        try:
            value = float(self.params[name])
        except (KeyError, TypeError, ValueError):
            raise RuleError(f"חסר או שגוי: {name}") from None
        return value

    def _bounded(self, name: str, low: float, high: float, unit: str) -> None:
        value = self._number(name)
        if not low <= value <= high:
            raise RuleError(
                f"הערך של {unit} חייב להיות בין {low:g} ל-{high:g} (התקבל {value:g})"
            )
        self.params[name] = value

    # -- wording ------------------------------------------------------------

    @property
    def currency(self) -> str:
        return fx.currency_for_ticker(self.symbol) or "USD"

    def describe(self) -> str:
        """One Hebrew line saying exactly when this rule fires.

        This is what the user confirms, so it names the baseline rather than
        just the number: "3% from the open" and "3% from yesterday's close"
        are different alerts and a confirmation that blurred them would defeat
        the point of confirming.
        """
        direction = self.params.get("direction")
        rising = direction == "up"
        word = "עולה" if rising else "יורדת"

        if self.kind == "pct_from_open":
            pct = ltr(f"{self.params['threshold_pct']:g}%")
            return f"{word} {pct} או יותר מנקודת הפתיחה של היום"
        if self.kind == "pct_from_close":
            pct = ltr(f"{self.params['threshold_pct']:g}%")
            return f"{word} {pct} או יותר מהסגירה של אתמול"
        if self.kind == "price_level":
            level = money(self.params["price"], self.currency)
            return f"חוצה את {level} {'כלפי מעלה' if rising else 'כלפי מטה'}"
        if self.kind == "ma_cross":
            days = ltr(self.params["days"])
            return (f"חוצה את ממוצע {days} הימים "
                    f"{'כלפי מעלה' if rising else 'כלפי מטה'}")
        if self.kind == "volume_multiple":
            multiple = ltr("%g" % self.params["multiple"])
            return f"נסחרת בפי {multiple} מהמחזור הממוצע"
        return self.kind

    def summary(self) -> str:
        """Symbol, condition and channel — the listing line."""
        channel = {"telegram": "טלגרם", "email": "אימייל",
                   "both": "טלגרם + אימייל"}[self.channel]
        extra = " · פעם אחת" if self.once else ""
        return f"{self.symbol}: {self.describe()} · {channel}{extra}"

    # -- state --------------------------------------------------------------

    def on_cooldown(self, now: Optional[datetime] = None) -> bool:
        if self.last_fired_at is None:
            return False
        now = now or datetime.now(timezone.utc)
        fired = self.last_fired_at
        if fired.tzinfo is None:
            fired = fired.replace(tzinfo=timezone.utc)
        elapsed = (now - fired).total_seconds() / 60.0
        return elapsed < self.cooldown_minutes

    def to_json(self) -> str:
        return json.dumps(self.params, ensure_ascii=False)


# ── Evaluation ────────────────────────────────────────────────────────────────

#: rule id → which side of its level the price was on last poll. A crossing
#: cannot be read from one observation, and the first one must not fire.
_sides: Dict[object, bool] = {}


def reset_state() -> None:
    _sides.clear()


def _crossed(key, now_above: bool, rising: bool) -> bool:
    """True on the poll where the price moves onto the wanted side.

    The first observation only records where things stood: a rule written while
    the price is already above its level has not seen a crossing, and saying it
    did would be a false report of an event.
    """
    was_above = _sides.get(key)
    _sides[key] = now_above
    if was_above is None:
        return False
    return (now_above and not was_above) if rising else (was_above and not now_above)


def evaluate(alert: CustomAlert, data: Dict, feed=None) -> Optional[str]:
    """The Hebrew reason this rule fires right now, or None.

    ``data`` is a quote from :class:`StockDataFeed`; ``feed`` is only needed by
    the kinds that look past today's bar.
    """
    price = data.get("price")
    if price is None:
        return None
    rising = alert.params.get("direction") == "up"
    currency = data.get("currency") or alert.currency

    if alert.kind in ("pct_from_open", "pct_from_close"):
        if alert.kind == "pct_from_open":
            base, base_name = data.get("open_price"), "הפתיחה"
        else:
            base, base_name = data.get("prev_close"), "הסגירה של אתמול"
        if not base:
            return None
        change = (price - base) / base * 100.0
        threshold = float(alert.params["threshold_pct"])
        if (rising and change < threshold) or (not rising and change > -threshold):
            return None
        # "from X to Y", not "X → Y": arrow glyphs carry Bidi_Mirrored, so an
        # arrow typed here renders reversed inside a right-to-left line.
        return (f"{'עלתה' if rising else 'ירדה'} {ltr(f'{abs(change):.2f}%')} "
                f"מ{base_name}: מ-{money(base, currency)} "
                f"ל-{money(price, currency)}")

    if alert.kind == "price_level":
        level = float(alert.params["price"])
        if not _crossed((alert.id, "level"), price >= level, rising):
            return None
        return (f"חצתה את {money(level, currency)} "
                f"{'כלפי מעלה' if rising else 'כלפי מטה'} "
                f"(מחיר נוכחי {money(price, currency)})")

    if alert.kind == "ma_cross":
        if feed is None:
            return None
        days = int(alert.params["days"])
        average = feed.get_moving_average(alert.symbol, days)
        if not average:
            return None
        if not _crossed((alert.id, "ma", days), price >= average, rising):
            return None
        return (f"חצתה את ממוצע {ltr(days)} הימים "
                f"({money(average, currency)}) "
                f"{'כלפי מעלה' if rising else 'כלפי מטה'} "
                f"— מחיר נוכחי {money(price, currency)}")

    if alert.kind == "volume_multiple":
        volume = data.get("volume") or 0
        if volume <= 0 or feed is None:
            return None
        average = feed.get_average_daily_volume(alert.symbol)
        if not average:
            return None
        ratio = volume / average
        if ratio < float(alert.params["multiple"]):
            return None
        return (f"נסחרה ב-{messages.count(volume)} מניות — "
                f"פי {ltr(f'{ratio:.1f}')} מהמחזור הממוצע")

    return None


# ── Turning a sentence into a rule ────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "אתה ממיר בקשות התראה בשפה חופשית למבנה JSON. ענה JSON תקין בלבד, "
    "בלי טקסט לפני או אחרי, ובלי גדרות קוד.\n\n"
    "המבנה בהצלחה:\n"
    '{"ok": true, "symbol": "ORCL", "kind": "<אחד מהסוגים>", '
    '"params": {...}, "channel": "telegram|email|both", "once": false, '
    '"explain": "משפט אחד בעברית שמסביר מה הבנת"}\n\n'
    "המבנה בכישלון:\n"
    '{"ok": false, "reason": "משפט אחד בעברית: מה חסר או למה הבקשה לא ניתנת למימוש"}\n\n'
    "הסוגים האפשריים והפרמטרים שלהם — אין אחרים:\n"
    "• pct_from_open — שינוי באחוזים מנקודת *הפתיחה של היום*. "
    'params: {"direction": "up"|"down", "threshold_pct": מספר}\n'
    "• pct_from_close — שינוי באחוזים מ*הסגירה של אתמול*. "
    'params: {"direction": "up"|"down", "threshold_pct": מספר}\n'
    "• price_level — חציית מחיר מוחלט. "
    'params: {"direction": "up"|"down", "price": מספר}\n'
    "• ma_cross — חציית ממוצע נע של N ימים. "
    'params: {"direction": "up"|"down", "days": מספר שלם}\n'
    "• volume_multiple — מחזור גבוה פי N מהממוצע. "
    'params: {"multiple": מספר}\n\n'
    "כללים:\n"
    "• נקודת הייחוס קריטית. 'מהפתיחה' ⇒ pct_from_open. "
    "'מהסגירה' / 'שינוי יומי' / בלי ציון נקודת ייחוס ⇒ pct_from_close.\n"
    "• 'חוצה כלפי מעלה/מטה' ⇒ direction up/down בהתאמה.\n"
    "• הטיקר: החזר את סמל המניה כפי שהוא נסחר (למשל אורקל ⇒ ORCL, "
    "טבע בתל אביב ⇒ TEVA.TA). מניות הבורסה בתל אביב מסתיימות ב-.TA. "
    "אם המשתמש כתב סמל במפורש — החזר אותו בדיוק כפי שנכתב, באותיות גדולות.\n"
    "• channel: 'email' רק אם המשתמש ביקש מייל במפורש, 'both' אם ביקש גם וגם, "
    "אחרת 'telegram'.\n"
    "• once: true רק אם נאמר במפורש שזו התראה חד-פעמית.\n"
    "• אם הבקשה לא מתאימה לאף סוג (למשל תנאי על דוחות, על חדשות או על "
    "אינדיקטור שאינו ממוצע נע) — החזר ok: false והסבר מה אפשר לבקש במקום.\n"
    "• אל תנחש מספרים שלא נאמרו. אם חסר אחוז, מחיר או אורך ממוצע — ok: false.\n"
    "• explain הוא לאישור המשתמש: כתוב בו את הטיקר, התנאי ונקודת הייחוס."
)

_FENCE_RE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)


def _json_object(text: str) -> Dict:
    """The JSON object in a model reply, fences and stray prose tolerated."""
    cleaned = _FENCE_RE.sub("", text or "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


@dataclass
class ParseResult:
    alert: Optional[CustomAlert] = None
    #: The model's one-line account of what it understood, shown for confirmation.
    explain: str = ""
    #: Why no rule came back. Empty on success.
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.alert is not None


def parse_request(text: str, analyst) -> ParseResult:
    """Turn a free-text request into an unsaved, validated rule. Blocking.

    Never raises: a request that cannot be expressed comes back as an error to
    show the user, because "I did not understand" is a usable answer and a
    traceback is not.
    """
    request = (text or "").strip()
    if not request:
        return ParseResult(error="לא קיבלתי טקסט.")
    if analyst is None:
        return ParseResult(error=(
            "התראות בשפה חופשית דורשות AI מופעל — הגדר ANTHROPIC_API_KEY "
            "ואפשר ai.enabled בקונפיגורציה."
        ))

    try:
        answer = analyst.parse_alert_request(_SYSTEM_PROMPT, request)
    except Exception as exc:                           # noqa: BLE001
        logger.error("Alert request parsing failed: %s", exc, exc_info=True)
        from .ai_analyst import friendly_error
        return ParseResult(error=friendly_error(exc))

    try:
        payload = _json_object(answer)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Unparseable alert-rule reply: %.200s", answer)
        return ParseResult(error="לא הצלחתי לפענח את הבקשה. נסה לנסח אותה אחרת.")

    if not payload.get("ok"):
        return ParseResult(error=str(payload.get("reason")
                                     or "לא הצלחתי להמיר את הבקשה להתראה."))

    try:
        alert = CustomAlert(
            symbol=str(payload.get("symbol") or "").strip().upper(),
            kind=str(payload.get("kind") or ""),
            params=dict(payload.get("params") or {}),
            channel=str(payload.get("channel") or "telegram"),
            request=request,
            once=bool(payload.get("once")),
            cooldown_minutes=int(payload.get("cooldown_minutes")
                                 or DEFAULT_COOLDOWN_MINUTES),
        ).validate()
    except RuleError as exc:
        return ParseResult(error=f"הבקשה הובנה אבל לא תקפה: {exc}")
    except (TypeError, ValueError) as exc:
        logger.warning("Malformed alert rule from the model: %s (%.200s)", exc, answer)
        return ParseResult(error="הבקשה הובנה באופן שאינו תקין. נסה לנסח אותה אחרת.")

    return ParseResult(alert=alert, explain=str(payload.get("explain") or ""))


# ── The message a fired rule sends ────────────────────────────────────────────

def alert_text(alert: CustomAlert, reason: str, session: str = "regular") -> str:
    """The Telegram message, in the same Hebrew style as the built-in alerts."""
    lines = [
        messages.rtl(f"🔔 *{ltr(alert.symbol)}* — ההתראה שביקשת"),
        messages.rtl(reason),
        messages.rtl(f"📝 הבקשה: {alert.request}" if alert.request else ""),
    ]
    if alert.once:
        lines.append(messages.rtl("ℹ️ זו הייתה התראה חד-פעמית, והיא כובתה."))
    lines.append(messages.footer(session))
    return "\n".join(line for line in lines if line.strip(messages.RLM))


def email_text(alert: CustomAlert, reason: str) -> List[str]:
    """(subject, body) for the same alert by mail — plain text, no bidi marks."""
    plain_reason = reason
    for mark in (messages.RLM, "⁦", "⁩"):
        plain_reason = plain_reason.replace(mark, "")
    subject = f"התראה: {alert.symbol}"
    body = [plain_reason]
    if alert.request:
        body += ["", f"הבקשה שהגדרת: {alert.request}"]
    body += ["", f"התנאי: {alert.symbol} {_plain(alert.describe())}"]
    if alert.once:
        body.append("זו הייתה התראה חד-פעמית, והיא כובתה.")
    return [subject, "\n".join(body)]


def _plain(text: str) -> str:
    for mark in (messages.RLM, "⁦", "⁩"):
        text = text.replace(mark, "")
    return text
