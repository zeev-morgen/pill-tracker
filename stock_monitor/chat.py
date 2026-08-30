"""A conversation with Claude that can see the portfolio as it currently is.

The existing AI features are one-shot: ask about a stock, grade a closed trade,
review the portfolio. Each answers once and forgets. This is the other mode —
a back-and-forth about what to actually do, where the follow-up question ("and
if I halve NBIS instead?") depends on the answer before it.

Two things make that work:

  The portfolio is re-read on every turn, not captured when the conversation
  started. Prices move while you are talking, and an answer computed against a
  snapshot from twenty minutes ago is worse than no answer — it looks current.

  That snapshot arrives as a mid-conversation *system* message rather than
  being pasted into the user's turn. It is operator data, not something the
  user said, and keeping the two channels separate means the model is never
  asked to guess which parts of a turn are instructions and which are numbers.
  It also leaves the conversation history byte-identical between turns, so the
  cached prefix survives; folding a changing snapshot into the history would
  invalidate the cache on every message.
"""

from __future__ import annotations

import logging
import threading
from typing import Iterator, List, Optional

import anthropic
import httpx
import urllib3

logger = logging.getLogger(__name__)

#: Mid-conversation system messages are the whole design here, and they are
#: model-gated — a model without them rejects the request outright. Models that
#: do support them are listed so an unsupported one degrades to folding the
#: snapshot into the user turn rather than failing the conversation.
SNAPSHOT_AS_SYSTEM_MODELS = (
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-fable-5",
    "claude-mythos-5",
)

DEFAULT_MODEL = "claude-opus-5"

#: Turns kept in memory. A portfolio conversation is a working session, not an
#: archive, and every retained turn is resent — and paid for — on every message.
MAX_TURNS = 40

#: Chat replies are read on screen between glances at the table. The cap is a
#: ceiling, not a target; brevity is asked for in the system prompt.
MAX_TOKENS = 8000

SYSTEM_PROMPT = (
    "אתה שותף לדיון על תיק השקעות, ועונה אך ורק בעברית.\n"
    "המשתמש מנהל את התיק בעצמו. התפקיד שלך הוא לחדד את החשיבה שלו — "
    "לא לתת ציונים ולא לחזור על מה שהוא כבר אמר.\n\n"
    "עקרונות:\n"
    "• ענה על מה שנשאל. שאלה קצרה מקבלת תשובה קצרה — 2-3 משפטים זו תשובה "
    "לגיטימית ולרוב הנכונה.\n"
    "• תמיד התבסס על המספרים בתמונת המצב שסופקה לך, וצטט אותם. אם נתון חסר, "
    "אמור שהוא חסר — אל תשלים אותו מהזיכרון.\n"
    "• התייחס לגדלים היחסיים: פוזיציה של 2% מהתיק לא שווה את אותה תשומת לב "
    "כמו פוזיציה של 40%, גם אם התשואה עליה דרמטית יותר.\n"
    "• כשאתה מציע פעולה — היא צריכה להיות ספציפית: כמה, באיזה מחיר, ומה "
    "יפריך את ההנחה. \"לשקול צמצום\" זו לא הצעה.\n"
    "• אם המשתמש שוקל משהו שנראה לך שגוי, אמור זאת ישירות ונמק. אתה לא כאן "
    "כדי להסכים.\n"
    "• אם המחירים מסומנים כלא מעודכנים, אמור זאת לפני שאתה מסיק מהם מסקנה.\n\n"
    "אתה מסייע בקבלת החלטות ולא נותן ייעוץ השקעות מוסדר. אל תחזור על "
    "המשפט הזה בכל תשובה — הוא מוצג בממשק."
)


def build_snapshot(report: dict, journal: Optional[dict] = None) -> str:
    """The portfolio as it stands right now, as text for the model.

    Deliberately includes the awkward parts — holdings that could not be
    priced, prices from an earlier session — because a total that silently
    excludes two positions invites confident advice about a portfolio that does
    not exist.
    """
    positions = report.get("positions") or []
    if not positions:
        return "התיק ריק — אין פוזיציות פתוחות."

    lines = [
        "תמונת מצב של התיק, נכון לרגע זה:",
        "",
        f"שווי כולל: ${report.get('total_value', 0):,.2f}",
        f"רווח/הפסד כולל: ${report.get('total_pnl_value', 0):,.2f}",
    ]

    lag = report.get("feed_lag_days")
    if lag:
        much = "ביום מסחר אחד" if lag == 1 else f"ב-{lag} ימי מסחר"
        lines.append(
            f"⚠️ המחירים אינם מעודכנים — ספק הנתונים מפגר {much}. "
            f"המחיר האחרון שהתקבל הוא מ-{report.get('latest_bar_date')}."
        )
    if report.get("has_foreign") and report.get("fx_rate"):
        lines.append(
            f"שער דולר/שקל בשימוש: {report['fx_rate']:.3f} "
            f"(מחירי מניות ישראליות מוצגים באגורות, השווי מומר לדולר)"
        )

    lines += ["", "פוזיציות:"]
    for p in positions:
        atr = f"{p['atr_pct']:.2f}%" if p.get("atr_pct") is not None else "אין נתון"
        weight = (
            f" | {p['market_value'] / report['total_value'] * 100:.1f}% מהתיק"
            if report.get("total_value") else ""
        )
        held = (
            f" | מוחזק {p['holding_days']} ימים"
            if p.get("holding_days") is not None else ""
        )
        # Agorot is written after the number, the way a unit is; a currency
        # symbol goes before it. Getting that backwards produces "אג׳3,450",
        # which reads as neither.
        agorot = str(p.get("currency", "")).upper() == "ILA"
        price = (
            (lambda v: f"{v:,.2f} אג׳") if agorot else (lambda v: f"${v:,.2f}")
        )
        stale = " | ⚠️ מחיר מסשן קודם" if p.get("price_is_stale") else ""
        lines.append(
            f"• {p['ticker']}: {p['quantity']:g} מניות"
            f" | כניסה {price(p['entry_price'])} | נוכחי {price(p['current_price'])}"
            f" | תשואה {p['pnl_pct']:+.2f}%"
            f" | שווי ${p['market_value']:,.2f}{weight} | ATR {atr}"
            f" | סקטור {p['sector']}{held}{stale}"
        )

    skipped = report.get("skipped_tickers") or []
    if skipped:
        lines.append(
            f"⚠️ לא ניתן לתמחר כרגע ואינן נכללות בסכומים: {', '.join(skipped)}"
            f" ({report.get('skip_reason') or 'סיבה לא ידועה'})"
        )

    sector = report.get("sector") or {}
    if sector.get("sectors"):
        lines += ["", "פיזור סקטוריאלי:"]
        lines += [
            f"• {s['sector']}: {s['weight_pct']:.1f}%" for s in sector["sectors"]
        ]
        if sector.get("alert"):
            names = ", ".join(
                s["sector"] for s in sector.get("concentrated_sectors", [])
            )
            lines.append(f"⚠️ ריכוזיות מעל סף {sector.get('threshold_pct')}%: {names}")

    volatility = report.get("volatility") or {}
    if volatility:
        lines += [
            "",
            f"חשיפת תנודתיות: {volatility.get('exposure_pct', 0):.1f}% מהתיק "
            f"במניות בעלות ATR גבוה (סף {volatility.get('threshold_pct')}%)",
        ]

    entries = (journal or {}).get("entries") or []
    if entries:
        summary = (journal or {}).get("summary") or {}
        lines += [
            "",
            f"עסקאות שנסגרו: {summary.get('count', len(entries))} | "
            f"רווח/הפסד מצטבר ${summary.get('total_pnl', 0):,.2f} | "
            f"אחוז רווחיות {summary.get('win_rate_pct', 0):.0f}%",
        ]
        for entry in entries[:5]:
            lines.append(
                f"• {entry['ticker']}: {entry['pnl_pct']:+.2f}% "
                f"ב-{entry.get('holding_days', '?')} ימים"
                + (f" | דירוג {entry['rating']}" if entry.get("rating") else "")
            )

    return "\n".join(lines)


class PortfolioChat:
    """A single ongoing conversation, held in memory.

    One conversation because this is a single-operator tool. Memory rather than
    the database because a chat is a working session — it is worth keeping for
    the afternoon, not for the year — and a restart losing it costs a retype,
    not data. Holdings and the journal are what the database is for.
    """

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        self._api_key = api_key
        self._model = model
        self._client = anthropic.Anthropic(api_key=api_key)
        self._insecure_client: Optional[anthropic.Anthropic] = None
        self._messages: List[dict] = []
        self._lock = threading.Lock()

    # ── Conversation state ────────────────────────────────────────────────────

    def history(self) -> List[dict]:
        with self._lock:
            return list(self._messages)

    def clear(self) -> None:
        with self._lock:
            self._messages.clear()

    def _trim(self) -> None:
        """Drop the oldest turns past the cap, keeping the conversation valid.

        The history must still begin with a user turn — the API rejects one
        that starts with an assistant message — so the cut lands on a user
        boundary rather than wherever the arithmetic falls.
        """
        if len(self._messages) <= MAX_TURNS:
            return
        cut = len(self._messages) - MAX_TURNS
        while cut < len(self._messages) and self._messages[cut]["role"] != "user":
            cut += 1
        del self._messages[:cut]

    # ── The request ───────────────────────────────────────────────────────────

    def _get_insecure_client(self) -> anthropic.Anthropic:
        """A verify=False client, for networks that intercept TLS.

        Same fallback the analyst carries: some antivirus and corporate proxies
        re-sign HTTPS, which the SDK correctly refuses.
        """
        if self._insecure_client is None:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            self._insecure_client = anthropic.Anthropic(
                api_key=self._api_key,
                http_client=httpx.Client(verify=False, timeout=120.0),
            )
        return self._insecure_client

    def _build_request_messages(self, snapshot: str) -> List[dict]:
        """History plus the current portfolio state.

        The snapshot is never stored in the history: it changes every turn, and
        keeping past snapshots would leave the model reading several
        contradictory versions of the same portfolio and paying for all of
        them. It is attached fresh to each request instead.
        """
        messages = list(self._messages)
        if not snapshot:
            return messages
        if self._model in SNAPSHOT_AS_SYSTEM_MODELS:
            # Must follow a user turn and be last — which it is, since the
            # caller has just appended the user's message.
            messages.append({"role": "system", "content": snapshot})
        else:
            # Older models reject a system role inside messages, so the state
            # rides along with the user's turn instead. Marked off explicitly
            # so the model can still tell data from instruction.
            last = messages[-1]
            messages[-1] = {
                "role": last["role"],
                "content": f"{last['content']}\n\n---\n{snapshot}",
            }
        return messages

    def stream_reply(self, user_message: str, snapshot: str) -> Iterator[str]:
        """Yield the reply in fragments as the model produces them.

        Streamed because a portfolio question can take half a minute to answer,
        and a blank box for half a minute reads as a broken feature.

        The user's turn is appended before the request and removed again if the
        request fails, so a failed send does not leave a question in the
        history that was never answered — the next turn would carry it along
        and the model would answer it late, out of context.
        """
        text = str(user_message or "").strip()
        if not text:
            raise ValueError("הודעה ריקה")

        with self._lock:
            self._messages.append({"role": "user", "content": text})
            self._trim()
            request_messages = self._build_request_messages(snapshot)

        collected: List[str] = []
        try:
            try:
                yield from self._stream(self._client, request_messages, collected)
            except (anthropic.APIConnectionError, httpx.ConnectError) as exc:
                if collected:
                    raise   # Mid-stream: retrying would duplicate what was sent.
                logger.warning(
                    "Anthropic connection error (%s) — retrying with SSL verify=False",
                    exc.__class__.__name__,
                )
                yield from self._stream(
                    self._get_insecure_client(), request_messages, collected
                )
        except Exception:
            with self._lock:
                if self._messages and self._messages[-1]["role"] == "user":
                    self._messages.pop()
            raise

        reply = "".join(collected).strip()
        with self._lock:
            if reply:
                self._messages.append({"role": "assistant", "content": reply})
            elif self._messages and self._messages[-1]["role"] == "user":
                # An empty reply is a failed turn, not an answer worth keeping.
                self._messages.pop()

    def _stream(
        self, client: anthropic.Anthropic, messages: List[dict], collected: List[str]
    ) -> Iterator[str]:
        with client.messages.stream(
            model=self._model,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    # The persona is fixed for the life of the process, so it
                    # is worth caching; everything that changes sits after it.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=messages,
        ) as stream:
            for fragment in stream.text_stream:
                collected.append(fragment)
                yield fragment
