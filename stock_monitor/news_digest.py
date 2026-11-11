"""Hebrew summaries for news items — one Claude call for a whole screenful.

Why this exists
───────────────
The news screen used to hand over an English headline and a link, which means
the reader does the translating and the clicking before learning whether the
item mattered. What is wanted is the substance, in Hebrew, with the time it was
published — so the list can be read rather than worked through.

Two constraints shape the implementation:

* **One call, not one per item.** Ten separate requests for ten headlines would
  be ten round trips and ten times the price, for a task that is identical for
  each item. The items go in numbered and come back numbered.
* **No invention, ever.** A fabricated detail in a financial news summary is
  worse than no summary: it reads exactly like a fact. The model is given the
  title and the feed's own blurb and told to use nothing else, and items that
  arrive with no blurb are marked as headline-only so the reader knows the
  Hebrew line is a translated title and not a summary of an article nobody read.

Summaries are cached by link, because a news item's text does not change and
the screen is re-opened far more often than Yahoo publishes.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

#: Most items summarised in one request. Ten headlines is already a long
#: Telegram message; beyond that the reader scrolls instead of reading.
MAX_ITEMS = 10
#: Published text does not change, so a long cache is safe. Bounded by size
#: below so a long-running process cannot grow one headline at a time.
CACHE_TTL = 12 * 3600
CACHE_LIMIT = 400
#: Keeps the reply short enough to read on a phone, at about 35 words an item.
MAX_TOKENS = 1400

_SYSTEM_PROMPT = (
    "אתה עורך חדשות פיננסיות שמתמצת פרסומים לעברית, עבור משקיע פרטי.\n\n"
    "לכל פריט כתוב משפט אחד או שניים בעברית: מה התפרסם, ומה המשמעות "
    "למשקיע אם היא נובעת מהטקסט עצמו.\n\n"
    "חוקים מוחלטים:\n"
    "• הסתמך *רק* על הכותרת והתקציר שסופקו לך. אל תוסיף מספרים, תאריכים, "
    "שמות, אחוזים או עובדות שלא מופיעים שם, וגם לא מידע שאתה מכיר ממקום אחר.\n"
    "• כשסופקה כותרת בלבד (ללא תקציר) — נסח אותה בעברית ועצור שם. "
    "אל תשלים פרטים שאינם בכותרת.\n"
    "• בלי המלצות קנייה או מכירה. זה דיווח, לא ייעוץ.\n"
    "• עד 35 מילים לפריט. בלי כותרות, בלי אימוג'י, בלי מירכאות.\n\n"
    "פורמט התשובה — שורה אחת לכל פריט, בדיוק במבנה:\n"
    "<מספר>| <הטקסט בעברית>\n"
    "ללא שום טקסט נוסף לפני או אחרי."
)

_LINE_RE = re.compile(r"^\s*(\d+)\s*[|:.\-]\s*(.+)$")

_lock = threading.Lock()
#: cache key → (hebrew text, stored at)
_cache: Dict[str, tuple] = {}


def _key(item: dict) -> str:
    """What identifies an item's text: its link, or failing that its title."""
    return (item.get("link") or "").strip() or f"title:{item.get('title', '')}"


def _cached(key: str) -> Optional[str]:
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        text, stored_at = entry
        if time.monotonic() - stored_at > CACHE_TTL:
            del _cache[key]
            return None
        return text


def _store(key: str, text: str) -> None:
    with _lock:
        if len(_cache) >= CACHE_LIMIT:
            # Drop the oldest quarter rather than one entry, so pruning is not
            # a per-insert cost once the cache is full.
            for stale in sorted(_cache, key=lambda k: _cache[k][1])[: CACHE_LIMIT // 4]:
                del _cache[stale]
        _cache[key] = (text, time.monotonic())


def reset_cache() -> None:
    with _lock:
        _cache.clear()


def _prompt(items: List[dict]) -> str:
    parts = ["הפריטים לתמצות:", ""]
    for index, item in enumerate(items, start=1):
        parts.append(f"[{index}] מניה: {item.get('ticker', '')}")
        parts.append(f"כותרת: {item.get('title', '')}")
        blurb = (item.get("summary") or "").strip()
        parts.append(f"תקציר: {blurb}" if blurb else "תקציר: (לא סופק — כותרת בלבד)")
        parts.append("")
    return "\n".join(parts)


def _parse(text: str, count: int) -> Dict[int, str]:
    """Numbered lines back into {index: hebrew}, ignoring anything else.

    Tolerant on purpose: a model that answers "1. ..." instead of "1| ..." has
    done the job, and dropping the whole screenful over a separator would be
    the wrong trade.
    """
    out: Dict[int, str] = {}
    for line in (text or "").splitlines():
        match = _LINE_RE.match(line)
        if not match:
            continue
        index = int(match.group(1))
        if 1 <= index <= count:
            out[index] = match.group(2).strip()
    return out


def summarize(items: List[dict], analyst=None) -> List[dict]:
    """Copy of *items* with ``hebrew`` and ``headline_only`` filled in.

    Blocking — call it off the event loop. Never raises and never drops an
    item: an item with no summary comes back with ``hebrew`` empty, and the
    caller shows the original headline. A news screen that fails to render
    because the AI is down would be a worse screen than the one we had.
    """
    enriched = []
    for item in items[:MAX_ITEMS]:
        copy = dict(item)
        copy["headline_only"] = not (item.get("summary") or "").strip()
        copy["hebrew"] = _cached(_key(item)) or ""
        enriched.append(copy)

    pending = [item for item in enriched if not item["hebrew"]]
    if not pending:
        return enriched
    if analyst is None:
        logger.info("News summaries skipped: no AI analyst configured")
        return enriched

    try:
        answer = analyst.summarize_news(_SYSTEM_PROMPT, _prompt(pending), MAX_TOKENS)
    except Exception as exc:                        # noqa: BLE001 — never fatal here
        logger.error("News summarisation failed: %s", exc, exc_info=True)
        return enriched

    summaries = _parse(answer, len(pending))
    if not summaries:
        logger.warning(
            "News summarisation returned nothing parseable (%d chars)", len(answer or "")
        )
    for index, item in enumerate(pending, start=1):
        text = summaries.get(index, "").strip()
        if text:
            item["hebrew"] = text
            _store(_key(item), text)
    return enriched
