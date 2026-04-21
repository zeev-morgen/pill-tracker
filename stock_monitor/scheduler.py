"""
APScheduler-based job runner that respects NYSE/NASDAQ market hours.

The monitor callback fires every N seconds but is silently skipped when
the market is outside the configured session window (regular + optional extended).
Cron jobs log market open/close events at the correct ET times.
"""

import logging
from typing import Callable, List

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .data_feed import get_market_session, is_market_open

logger = logging.getLogger(__name__)
NYSE_TZ = pytz.timezone("America/New_York")


class MarketScheduler:
    def __init__(
        self,
        interval_seconds: int = 60,
        include_extended_hours: bool = True,
    ) -> None:
        self.interval_seconds     = interval_seconds
        self.include_extended     = include_extended_hours
        self._scheduler           = AsyncIOScheduler(timezone=NYSE_TZ)
        self._callbacks: List[Callable] = []

    # ── Registration ──────────────────────────────────────────────────────────

    def add_callback(self, fn: Callable) -> None:
        """Register an async function to be called on each monitor tick."""
        self._callbacks.append(fn)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        # Main polling job
        self._scheduler.add_job(
            self._tick,
            trigger=IntervalTrigger(seconds=self.interval_seconds),
            id="stock_monitor_tick",
            replace_existing=True,
            max_instances=1,       # never overlap
        )

        # Market open / close announcements (ET)
        self._scheduler.add_job(
            self._on_market_open,
            trigger=CronTrigger(day_of_week="mon-fri", hour=9, minute=30, timezone=NYSE_TZ),
            id="market_open",
        )
        self._scheduler.add_job(
            self._on_pre_market_open,
            trigger=CronTrigger(day_of_week="mon-fri", hour=4, minute=0, timezone=NYSE_TZ),
            id="pre_market_open",
        )
        self._scheduler.add_job(
            self._on_market_close,
            trigger=CronTrigger(day_of_week="mon-fri", hour=16, minute=0, timezone=NYSE_TZ),
            id="market_close",
        )
        self._scheduler.add_job(
            self._on_after_market_close,
            trigger=CronTrigger(day_of_week="mon-fri", hour=20, minute=0, timezone=NYSE_TZ),
            id="after_market_close",
        )

        self._scheduler.start()
        logger.info(
            "Scheduler started — polling every %ds, extended hours: %s",
            self.interval_seconds,
            self.include_extended,
        )

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        if not is_market_open(self.include_extended):
            logger.debug("Market %s — skipping tick", get_market_session())
            return

        logger.debug("Monitor tick (%s session)", get_market_session())
        for fn in self._callbacks:
            try:
                await fn()
            except Exception as exc:
                logger.error("Monitor callback error: %s", exc, exc_info=True)

    async def _on_pre_market_open(self) -> None:
        logger.info("Pre-market session opened (04:00 ET)")

    async def _on_market_open(self) -> None:
        logger.info("Regular session opened (09:30 ET)")

    async def _on_market_close(self) -> None:
        logger.info("Regular session closed (16:00 ET)")

    async def _on_after_market_close(self) -> None:
        logger.info("After-hours session closed (20:00 ET)")
