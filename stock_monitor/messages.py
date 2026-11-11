"""Hebrew wording for everything the system sends out.

Why the wording lives here and not in the engines
─────────────────────────────────────────────────
The alert engine used to write its own sentence: ``$AVGO moved UP 3.21% in 15
min``. One alert type — the earnings reminder — was written in Hebrew, because
it happened to be added later. So the same inbox carried two languages, and
translating the rest would have meant editing the strings the log lines and the
stored alert history are made of.

Instead the engine now records *facts* in ``AlertEvent.details`` and keeps its
English one-liner for the log and the alert table, which stay greppable. This
module turns those facts into the Hebrew a person reads. Adding a channel, or a
language, means another renderer here rather than another branch in the engine.

Bidirectional text
──────────────────
Hebrew with embedded prices is where naive formatting falls apart: "ירידה של
3.21% מ-$103.00 ל-$100.00" reorders in a mobile client, and the two prices can
swap places — a wrong number in an alert about a number. Every Latin or numeric
run therefore goes out inside a Unicode isolate, and every line starts with a
right-to-left mark, so each run keeps its own direction and the line as a whole
stays Hebrew.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from . import fx

#: Right-to-left mark: fixes each line's base direction, so a line that opens
#: with a ticker or a digit is still laid out as Hebrew.
RLM = "‏"
#: First-strong isolate … pop: an LTR run that cannot reorder its neighbours.
_ISOLATE_OPEN, _ISOLATE_CLOSE = "⁦", "⁩"

SEVERITY_ICON = {"INFO": "🔔", "WARNING": "⚠️", "CRITICAL": "🚨"}
SESSION_ICON = {"pre": "🌅", "regular": "📈", "after": "🌆", "closed": "🔒"}
SESSION_HE = {
    "pre": "טרום מסחר",
    "regular": "מסחר רגיל",
    "after": "אחרי שעות המסחר",
    "closed": "שוק סגור",
}


# ── Bidi-safe primitives ──────────────────────────────────────────────────────

def ltr(text) -> str:
    """Wrap a Latin/numeric run so it cannot reorder the Hebrew around it."""
    return f"{_ISOLATE_OPEN}{text}{_ISOLATE_CLOSE}"


def rtl(line: str) -> str:
    """Mark a line as Hebrew regardless of which character it opens with."""
    return f"{RLM}{line}"


def money(amount: Optional[float], currency: str = "USD") -> str:
    """A price in the currency it is quoted in, matching the dashboard's style.

    Tel Aviv quotes arrive in agorot, and printing those with a dollar sign is
    the hundredfold error this project has already shipped once.
    """
    if amount is None:
        return "—"
    code = fx.normalize_currency(currency)
    body = f"{abs(amount):,.2f}"
    sign = "-" if amount < 0 else ""
    if code == fx.AGOROT:
        return ltr(f"{sign}{body}") + " אג׳"
    if code == fx.SHEKEL:
        return ltr(f"{sign}₪{body}")
    return ltr(f"{sign}${body}")


def pct(value: Optional[float], decimals: int = 2) -> str:
    if value is None:
        return "—"
    return ltr(f"{value:+.{decimals}f}%")


def count(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return ltr(f"{int(value):,}")


def window_phrase(count_: int) -> str:
    """"in the last N minutes", declined properly.

    Hebrew has a dual form for two, takes no plural marker after a numeral,
    and prefixes its prepositions onto the following word — "ב-שתי דקות" is
    not something a Hebrew speaker writes. Only a numeral, which is foreign to
    the script, keeps the hyphen.
    """
    if count_ == 1:
        return "בדקה האחרונה"
    if count_ == 2:
        return "בשתי הדקות האחרונות"
    return f"ב-{ltr(count_)} הדקות האחרונות"


def days(count_: int) -> str:
    if count_ == 0:
        return "היום"
    if count_ == 1:
        return "מחר"
    if count_ == 2:
        return "יומיים"
    return f"{ltr(count_)} ימים"


def _currency_of(event) -> str:
    """The quote currency for an event's symbol, from its details or its suffix."""
    details = getattr(event, "details", None) or {}
    return details.get("currency") or fx.currency_for_ticker(event.symbol) or "USD"


def footer(session: str, timestamp: Optional[datetime] = None) -> str:
    icon = SESSION_ICON.get(session, "📊")
    name = SESSION_HE.get(session, session)
    when = (timestamp or datetime.now()).strftime("%H:%M")
    return rtl(f"{icon} {name} · {ltr(when)} ET")


# ── Alert renderers, one per alert type ───────────────────────────────────────

def _price_change(event, currency: str) -> list:
    d = event.details
    change = d.get("change_pct") or 0.0
    window = int(d.get("window_minutes") or 0)
    rising = change > 0
    arrow = "📈" if rising else "📉"
    headline = "זינוק" if rising else "צניחה"
    if event.severity == "INFO":
        headline = "עלייה" if rising else "ירידה"
    return [
        rtl(f"{SEVERITY_ICON.get(event.severity, '🔔')} *{ltr(event.symbol)}* — "
            f"{headline} של {ltr(f'{abs(change):.2f}%')}"),
        # "from X to Y" rather than "X → Y": arrow glyphs carry
        # Bidi_Mirrored=Yes, so an arrow typed here renders reversed inside a
        # right-to-left line — pointing from the new price back at the old one.
        rtl(f"{arrow} {window_phrase(window)}: "
            f"מ-{money(d.get('from_price'), currency)} "
            f"ל-{money(d.get('to_price'), currency)}"),
    ]


def _volume_spike(event, currency: str) -> list:
    d = event.details
    ratio = d.get("ratio")
    traded = f"📦 {count(d.get('volume'))} מניות"
    if ratio is not None:
        against = "מהקצב הצפוי לשעה זו" if d.get("time_adjusted") else "מהמחזור היומי הרגיל"
        traded += f" — פי {ltr(f'{ratio:.1f}')} {against}"
    lines = [
        rtl(f"{SEVERITY_ICON.get(event.severity, '🔔')} *{ltr(event.symbol)}* — "
            f"זינוק במחזור המסחר"),
        rtl(traded),
    ]
    elapsed = d.get("session_elapsed_pct")
    average = d.get("avg_volume")
    if elapsed is not None:
        lines.append(rtl(
            f"⏳ {ltr(f'{elapsed:.0f}%')} מיום המסחר חלפו · "
            f"ממוצע 10 ימים: {count(average)}"
        ))
    elif average is not None:
        lines.append(rtl(f"📊 ממוצע 10 ימים: {count(average)}"))
    return lines


def _price_threshold(event, currency: str) -> list:
    d = event.details
    upward = d.get("direction") == "above"
    return [
        rtl(f"{SEVERITY_ICON.get(event.severity, '🔔')} *{ltr(event.symbol)}* — "
            f"חצתה את הרף {'כלפי מעלה' if upward else 'כלפי מטה'}"),
        rtl(f"{'⬆️' if upward else '⬇️'} רף: {money(d.get('level'), currency)} · "
            f"מחיר נוכחי: {money(d.get('price') or event.price, currency)}"),
    ]


_RENDERERS = {
    "price_change_pct": _price_change,
    "volume_spike": _volume_spike,
    "price_threshold": _price_threshold,
}


def alert(event, session: str = "regular") -> str:
    """One alert, in Hebrew, for any alert type the engine can produce.

    An unknown type still gets a sane message: its English one-liner under a
    Hebrew header, which is strictly better than dropping the alert because
    nobody wrote a renderer for it.
    """
    currency = _currency_of(event)
    renderer = _RENDERERS.get(event.alert_type)
    if renderer is not None and getattr(event, "details", None):
        lines = renderer(event, currency)
    else:
        lines = [
            rtl(f"{SEVERITY_ICON.get(event.severity, '🔔')} *{ltr(event.symbol)}* — התראה"),
            rtl(ltr(event.message)),
        ]
    lines.append(footer(session, getattr(event, "timestamp", None)))
    return "\n".join(lines)


def rules_summary(alerts, currency: str = "USD") -> str:
    """The alert rules for one symbol, in a phrase: "שינוי ±3% ב-10 דק׳ · מחזור ×2.5".

    So the stock-management screen can say what adding a stock actually buys —
    rather than leaving it to be inferred from whether an alert ever arrives.
    A price threshold is a figure in the stock's own currency, so the caller
    passes the currency; percentages and volume multiples need none.
    """
    parts = []
    for alert in alerts or []:
        kind = getattr(alert, "type", "")
        if kind == "price_change_pct":
            threshold = ltr(f"±{alert.threshold_pct:g}%")
            parts.append(f"שינוי {threshold} ב-{ltr(alert.window_minutes)} דק׳")
        elif kind == "volume_spike":
            parts.append(f"מחזור {ltr(f'×{alert.multiplier:g}')}")
        elif kind == "price_threshold":
            if getattr(alert, "above", None) is not None:
                parts.append(f"מעל {money(alert.above, currency)}")
            if getattr(alert, "below", None) is not None:
                parts.append(f"מתחת ל-{money(alert.below, currency)}")
    return " · ".join(parts) if parts else "ללא כללי התראה"


def alert_summary(event) -> str:
    """One plain Hebrew line for the alert history — no markup, no direction marks.

    What gets stored, and so what the dashboard's alert table and 🔔 התראות
    both show. Kept free of Markdown and of the bidi marks the chat message
    carries, because this string ends up in a table cell and in a database
    column rather than in a Telegram bubble. ``event.message`` stays English
    for the log, where searching matters more than reading.
    """
    rendered = alert(event)
    # Drop the header icon, the footer line and the formatting, leaving the
    # facts: "AVGO — צניחה של 3.21% · מ-$412.50 ל-$399.26".
    lines = [line for line in rendered.split("\n")[:-1]]
    plain = " · ".join(lines)
    for mark in (RLM, _ISOLATE_OPEN, _ISOLATE_CLOSE, "*"):
        plain = plain.replace(mark, "")
    for icon in SEVERITY_ICON.values():
        plain = plain.replace(icon, "")
    return " ".join(plain.split()).strip()


# ── Other outbound messages ───────────────────────────────────────────────────

def earnings_alert(symbol: str, days_ahead: int, when: str) -> tuple:
    """(title, body) for the earnings reminder, in the shared Hebrew style."""
    title = f"📅 דיווח תוצאות מתקרב: {symbol}"
    # "היום"/"מחר" read as adverbs and take no preposition; anything further out
    # needs "בעוד" — "בעוד מחר" is not Hebrew.
    lead = "" if days_ahead <= 1 else "בעוד "
    body = "\n".join([
        rtl(f"📅 *{ltr(symbol)}* — דיווח תוצאות {lead}{days(days_ahead)}"),
        rtl(f"🗓 מועד: {ltr(when)}"),
        rtl("⚠️ כדאי להיערך מראש — דיווח מזיז את המניה בחדות לשני הכיוונים."),
    ])
    return title, body


def external_alert(title: str, body: str) -> str:
    """A free-form alert from outside — a TradingView webhook, say.

    The body is written by whoever set up the webhook, so it is passed through
    untranslated and isolated; only the frame is ours.
    """
    return "\n".join([rtl(f"📡 *{ltr(title)}*"), rtl(ltr(body))])
