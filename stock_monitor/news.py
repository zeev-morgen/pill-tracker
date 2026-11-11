"""Recent headlines for a ticker, from whichever source still answers.

Why this module exists
──────────────────────
Both the AI analysis and the dashboard's news tab used to call
``yf.Ticker(symbol).news`` directly and trust the result. In October 2026 Yahoo
retired the endpoint that call used; it began answering 404 with a JSON body,
which yfinance parses successfully, finds no stream in, and returns ``[]`` for.
No exception, no log line — so every analysis simply stopped carrying news and
said nothing about it. Pinning a newer yfinance fixes today's breakage, but the
endpoint underneath is unofficial and will move again.

So the lesson, rather than the patch:

* **An empty answer is not an answer.** Zero headlines for a liquid stock means
  the source is broken far more often than it means nothing happened, so a
  source that returns nothing is treated as a failed source and the next one is
  tried. Only when a source returns items do we believe its zero-after-filtering.
* **Three independent paths.** yfinance's ticker news, yfinance's search news
  (a different Yahoo endpoint), and Yahoo's RSS headline feed — plain XML that
  has outlived every JSON endpoint around it, and needs no cookie or crumb.
* **Say which one answered, or that none did.** Callers can then tell "nothing
  was published" from "we cannot see the news right now", and the AI is told
  the difference instead of quietly analysing without it.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, List, Optional

import requests
import urllib3
import yfinance as yf

logger = logging.getLogger(__name__)

#: How far back an item may be published and still count as recent.
FRESH_WINDOW_HOURS = 24

#: Yahoo's per-ticker RSS feed: the last source standing whenever the JSON
#: endpoints move. No authentication, no crumb, no rate limit worth speaking of.
RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"

HTTP_TIMEOUT = 10


@dataclass
class NewsResult:
    """Headlines plus the provenance a caller needs to word itself honestly."""

    items: List[dict] = field(default_factory=list)
    #: Which source answered: "ticker", "search", "rss", or "" if none did.
    #: A source that responded with an empty list counts as having answered —
    #: see ``available``.
    source: str = ""
    #: Which sources failed, and how. Empty only when none of them did.
    error: str = ""

    @property
    def available(self) -> bool:
        """True when some source responded, even with nothing to report.

        The line this draws: a *failure* to reach the news is reported as a
        failure, while a ticker nobody has written about reads as quiet. Those
        are different facts and the old code could report neither. Getting it
        the other way round would mean crying outage over every thinly covered
        Tel Aviv small-cap, which is most of them.

        A silent, simultaneous failure of all three sources would still read as
        quiet; ``status()`` exists to catch exactly that, by probing a ticker
        that always has news.
        """
        return bool(self.source)


# ── Normalization ─────────────────────────────────────────────────────────────

def _published_at(item: dict) -> Optional[datetime]:
    """yfinance news items carry the timestamp under several shapes."""
    content = item.get("content") or item
    raw = (
        content.get("pubDate")
        or content.get("displayTime")
        or item.get("providerPublishTime")
    )
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_hours(published: Optional[datetime]) -> Optional[float]:
    if published is None:
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - published).total_seconds() / 3600.0, 1)


#: HTML a feed's blurb can arrive wrapped in. RSS descriptions in particular
#: carry anchors and <p> tags that would reach the prompt and the screen as-is.
_TAG_RE = re.compile(r"<[^>]+>")
#: Enough of a blurb to summarise from, bounded so one chatty feed cannot
#: dominate a prompt that carries ten of them.
SUMMARY_LIMIT = 700


def _blurb(content: dict, item: dict) -> str:
    """The item's own description, as plain text.

    This is what makes a Hebrew summary possible without fetching the article:
    Yahoo ships a paragraph of its own with most items.
    """
    raw = (
        content.get("summary")
        or content.get("description")
        or item.get("summary")
        or item.get("description")
        or ""
    )
    text = _TAG_RE.sub(" ", str(raw))
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = " ".join(text.split())
    return text[:SUMMARY_LIMIT]


def _normalize(item: dict, ticker: str) -> Optional[dict]:
    """One news item from any yfinance shape, or None when it carries no title."""
    content = item.get("content") or item
    title = content.get("title") or item.get("title")
    if not title:
        return None
    provider = content.get("provider")
    publisher = (
        provider.get("displayName")
        if isinstance(provider, dict)
        else content.get("publisher") or item.get("publisher") or ""
    )
    link = (
        (content.get("canonicalUrl") or {}).get("url")
        if isinstance(content.get("canonicalUrl"), dict)
        else content.get("link") or item.get("link") or ""
    )
    published = _published_at(item)
    return {
        "ticker": ticker,
        "title": str(title).strip(),
        "summary": _blurb(content, item),
        "publisher": publisher or "",
        "link": link or "",
        "published_at": published.isoformat() if published else None,
        "age_hours": _age_hours(published),
    }


def _dedupe(items: List[dict]) -> List[dict]:
    """Drop repeats by title: the sources overlap, and so do syndicated wires."""
    seen, out = set(), []
    for item in items:
        key = item["title"].casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# ── Sources ───────────────────────────────────────────────────────────────────

def _http_get(url: str, **kwargs) -> requests.Response:
    """GET with the SSL fallback the rest of the project uses."""
    try:
        return requests.get(url, timeout=HTTP_TIMEOUT, **kwargs)
    except requests.exceptions.SSLError:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return requests.get(url, timeout=HTTP_TIMEOUT, verify=False, **kwargs)


def _from_ticker(ticker: str, limit: int) -> List[dict]:
    """Yahoo's quote-page news list, through yfinance."""
    return list(yf.Ticker(ticker).news or [])[: limit * 2]


def _from_search(ticker: str, limit: int) -> List[dict]:
    """Yahoo's search endpoint, which carries news under a different path.

    Its items are the flat legacy shape (title/publisher/link/
    providerPublishTime), which ``_normalize`` already reads.
    """
    # A short timeout on purpose: this runs while someone waits for a reply in
    # Telegram, and the next source is cheaper than a slow answer from this one.
    found = yf.Search(
        ticker, news_count=limit * 2, max_results=1, timeout=HTTP_TIMEOUT
    ).news or []
    return list(found)


def _from_rss(ticker: str, limit: int) -> List[dict]:
    """Yahoo's RSS headline feed, parsed into the flat legacy shape."""
    resp = _http_get(RSS_URL, params={"s": ticker, "region": "US", "lang": "en-US"})
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    items = []
    for node in root.findall(".//item")[: limit * 2]:
        title = (node.findtext("title") or "").strip()
        if not title:
            continue
        published = None
        raw_date = node.findtext("pubDate")
        if raw_date:
            try:
                published = parsedate_to_datetime(raw_date)
            except (TypeError, ValueError):
                published = None
        items.append({
            "title": title,
            "description": (node.findtext("description") or "").strip(),
            "link": (node.findtext("link") or "").strip(),
            "publisher": (node.findtext("source") or "Yahoo Finance").strip(),
            "providerPublishTime": published.timestamp() if published else None,
        })
    return items


#: Tried in order. The first source to return any item wins; a source that
#: returns nothing is treated as broken rather than as proof of no news.
SOURCES: List[tuple] = [
    ("ticker", _from_ticker),
    ("search", _from_search),
    ("rss", _from_rss),
]


# ── Public API ────────────────────────────────────────────────────────────────

def fetch(
    ticker: str,
    limit: int = 6,
    window_hours: Optional[float] = FRESH_WINDOW_HOURS,
) -> NewsResult:
    """Recent headlines for one ticker. Blocking — keep it off the event loop.

    ``window_hours=None`` disables the freshness filter. Items without a
    timestamp are kept either way: Yahoo omits it often enough that dropping
    them would empty the feed.
    """
    errors: List[str] = []
    answered = ""          # the first source that responded without raising
    for name, source in SOURCES:
        try:
            raw = source(ticker, limit)
        except Exception as exc:                      # noqa: BLE001 — any source may fail
            errors.append(f"{name}: {exc.__class__.__name__}")
            logger.debug("News source %s failed for %s: %s", name, ticker, exc)
            continue

        answered = answered or name
        if not raw:
            # Not believed on its own — an empty list is what a retired
            # endpoint looks like — so the next source still gets a turn.
            errors.append(f"{name}: empty")
            continue

        normalized = [n for n in (_normalize(i, ticker) for i in raw) if n]
        fresh = [
            item for item in normalized
            if window_hours is None
            or item["age_hours"] is None
            or item["age_hours"] <= window_hours
        ]
        if fresh:
            fresh = _dedupe(fresh)
            fresh.sort(key=lambda i: (i["age_hours"] is None, i["age_hours"] or 0))
            return NewsResult(items=fresh[:limit], source=name)

    detail = "; ".join(errors) or "no sources configured"
    if answered:
        # Someone responded with nothing recent to report, which is an answer.
        # The errors still travel with it: a ticker that only ever answers from
        # the third source is worth seeing in the diagnostics.
        return NewsResult(items=[], source=answered, error=detail)

    logger.warning("No news source answered for %s (%s)", ticker, detail)
    return NewsResult(items=[], source="", error=detail)


#: The probe ticker for ``status()``. Whatever else is true on a given day,
#: Apple has been written about in the last 24 hours — so zero headlines here
#: means the feed is broken, not that the news is quiet.
PROBE_TICKER = "AAPL"


def status() -> dict:
    """What a diagnostics endpoint needs to say why news is or is not arriving.

    ``healthy`` is the single number worth reading: the probe ticker always has
    recent coverage, so no items for it is a broken feed however cheerfully the
    sources answered.
    """
    probe = fetch(PROBE_TICKER, limit=3)
    return {
        "yfinance_version": getattr(yf, "__version__", "unknown"),
        "probe_ticker": PROBE_TICKER,
        "source": probe.source,
        "items": len(probe.items),
        "healthy": bool(probe.items),
        "error": probe.error,
        "fresh_window_hours": FRESH_WINDOW_HOURS,
    }
