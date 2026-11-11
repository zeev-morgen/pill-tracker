"""Pre-market news scan for portfolio holdings.

Runs once a day, 30 minutes before the opening bell, and caches what it finds
so the dashboard tab is instant. The scan is also exposed on demand, because a
cache that is only filled at 09:00 ET is empty the first time you open the
dashboard at any other hour.

The headlines themselves come from :mod:`stock_monitor.news`, which tries
several sources in turn — see that module for why a single source is not
trustworthy. This one adds the per-holding loop, the per-ticker cap and the
cache, and reports whether any source answered at all, so the tab can say
"the news feed is unavailable" instead of showing an empty list that looks
like a quiet day.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from . import news
from .news import FRESH_WINDOW_HOURS, _normalize, _published_at  # re-exported
from .store import PortfolioStore

logger = logging.getLogger(__name__)

#: Per-ticker cap, so one noisy stock cannot crowd out the rest.
MAX_ITEMS_PER_TICKER = 4

__all__ = [
    "FRESH_WINDOW_HOURS",
    "MAX_ITEMS_PER_TICKER",
    "NewsMonitor",
    "_normalize",
    "_published_at",
]


class NewsMonitor:
    """Collects and caches recent headlines for the tickers you hold."""

    def __init__(self, store: PortfolioStore) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._items: List[dict] = []
        self._scanned_at: Optional[datetime] = None
        self._sources: Dict[str, str] = {}
        self._unavailable: List[str] = []

    # -- collection ---------------------------------------------------------

    def scan(self) -> List[dict]:
        """Fetch headlines for every holding. Blocking — call off the loop."""
        tickers = [h.ticker for h in self._store.all()]
        collected: List[dict] = []
        sources: Dict[str, str] = {}
        unavailable: List[str] = []

        for ticker in tickers:
            result = news.fetch(ticker, limit=MAX_ITEMS_PER_TICKER)
            if not result.available:
                unavailable.append(ticker)
                continue
            sources[ticker] = result.source
            collected.extend(result.items)

        collected.sort(key=lambda i: (i["age_hours"] is None, i["age_hours"] or 0))
        with self._lock:
            self._items = collected
            self._scanned_at = datetime.now(timezone.utc)
            self._sources = sources
            self._unavailable = unavailable

        logger.info(
            "News scan: %d items across %d holdings (sources: %s%s)",
            len(collected), len(tickers),
            ", ".join(sorted(set(sources.values()))) or "none",
            f"; no source answered for {', '.join(unavailable)}" if unavailable else "",
        )
        return collected

    async def daily_scan(self) -> None:
        """Scheduler entry point — signature matches the other daily jobs.

        asyncio.to_thread rather than get_event_loop().run_in_executor: the
        latter is deprecated and raises outright when no loop is current.
        """
        import asyncio

        await asyncio.to_thread(self.scan)

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
            sources = dict(self._sources)
            unavailable = list(self._unavailable)

        return {
            "items": items,
            "scanned_at": scanned_at.isoformat() if scanned_at else None,
            "fresh_window_hours": FRESH_WINDOW_HOURS,
            "sources": sources,
            # Tickers for which every source failed — as opposed to tickers
            # that simply had no recent headline.
            "unavailable": unavailable,
        }
