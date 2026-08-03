"""
Pre-market news scan for portfolio holdings.

Runs once a day, 30 minutes before the opening bell, and caches what it finds
so the dashboard tab is instant. The scan is also exposed on demand, because a
cache that is only filled at 09:00 ET is empty the first time you open the
dashboard at any other hour.

News comes from yfinance, which is the same source the AI analysis already
uses; there is no extra API key or paid feed involved.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import yfinance as yf

from .store import PortfolioStore

logger = logging.getLogger(__name__)

#: How far back an item may be published and still count as "breaking".
FRESH_WINDOW_HOURS = 24
#: Per-ticker cap, so one noisy stock cannot crowd out the rest.
MAX_ITEMS_PER_TICKER = 4


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


def _normalize(item: dict, ticker: str) -> Optional[dict]:
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
        "title": title,
        "publisher": publisher,
        "link": link,
        "published_at": published.isoformat() if published else None,
        "age_hours": (
            round((datetime.now(timezone.utc) - published).total_seconds() / 3600.0, 1)
            if published else None
        ),
    }


class NewsMonitor:
    """Collects and caches recent headlines for the tickers you hold."""

    def __init__(self, store: PortfolioStore) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._items: List[dict] = []
        self._scanned_at: Optional[datetime] = None

    # -- collection ---------------------------------------------------------

    def scan(self) -> List[dict]:
        """Fetch headlines for every holding. Blocking — call off the loop."""
        tickers = [h.ticker for h in self._store.all()]
        collected: List[dict] = []
        for ticker in tickers:
            try:
                raw = yf.Ticker(ticker).news or []
            except Exception as exc:
                logger.warning("News fetch failed for %s: %s", ticker, exc)
                continue
            fresh = []
            for item in raw:
                normalized = _normalize(item, ticker)
                if normalized is None:
                    continue
                # Items with no timestamp are kept: yfinance omits it often
                # enough that dropping them would empty the feed.
                if (
                    normalized["age_hours"] is not None
                    and normalized["age_hours"] > FRESH_WINDOW_HOURS
                ):
                    continue
                fresh.append(normalized)
                if len(fresh) >= MAX_ITEMS_PER_TICKER:
                    break
            collected.extend(fresh)

        collected.sort(key=lambda i: (i["age_hours"] is None, i["age_hours"] or 0))
        with self._lock:
            self._items = collected
            self._scanned_at = datetime.now(timezone.utc)
        logger.info("Pre-market news scan: %d items across %d holdings",
                    len(collected), len(tickers))
        return collected

    async def daily_scan(self) -> None:
        """Scheduler entry point — signature matches the other daily jobs."""
        import asyncio

        await asyncio.get_event_loop().run_in_executor(None, self.scan)

    # -- reading ------------------------------------------------------------

    def snapshot(self, max_age_minutes: int = 60) -> Dict:
        """Cached results, refreshed on demand when stale or never run."""
        with self._lock:
            items, scanned_at = list(self._items), self._scanned_at

        is_stale = scanned_at is None or (
            datetime.now(timezone.utc) - scanned_at > timedelta(minutes=max_age_minutes)
        )
        if is_stale:
            items = self.scan()
            with self._lock:
                scanned_at = self._scanned_at

        return {
            "items": items,
            "scanned_at": scanned_at.isoformat() if scanned_at else None,
            "fresh_window_hours": FRESH_WINDOW_HOURS,
        }
