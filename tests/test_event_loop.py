"""Scheduled work must not block the event loop.

The monitor poll, the earnings check and the news scan are all network-bound.
Run directly on the loop they freeze the web server for their whole duration,
and a slow Yahoo makes that tens of seconds — during which /health cannot
answer. Render's probe gives up after five seconds and restarts the instance.
"""

import asyncio
import threading
import time

import pytest

from stock_monitor.earnings import EarningsMonitor
from stock_monitor.news_monitor import NewsMonitor
from stock_monitor.main import StockMonitorApp

#: Long enough to be unambiguous, short enough to keep the suite fast.
BLOCK_SECONDS = 0.4


async def loop_stall_during(coro) -> float:
    """Longest gap the event loop went unserviced while *coro* ran.

    A heartbeat ticks every 10ms. If the coroutine keeps the loop, the
    heartbeat cannot run and the gap grows to the coroutine's full duration.
    """
    worst = 0.0
    running = True

    async def heartbeat():
        nonlocal worst
        last = time.monotonic()
        while running:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            worst = max(worst, now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)          # let the heartbeat settle
    await coro
    running = False
    await beat
    return worst


# ── The monitor poll ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_monitor_cycle_keeps_the_loop_responsive():
    app = StockMonitorApp.__new__(StockMonitorApp)   # no config, no network

    def slow_poll():
        time.sleep(BLOCK_SECONDS)

    app._run_cycle = slow_poll
    stall = await loop_stall_during(app.monitor_cycle())
    assert stall < BLOCK_SECONDS / 2, (
        f"the loop was blocked for {stall:.2f}s — /health cannot answer during that"
    )


@pytest.mark.anyio
async def test_monitor_cycle_runs_off_the_main_thread():
    app = StockMonitorApp.__new__(StockMonitorApp)
    seen = {}

    def record():
        seen["thread"] = threading.current_thread()

    app._run_cycle = record
    await app.monitor_cycle()
    assert seen["thread"] is not threading.main_thread()


@pytest.mark.anyio
async def test_monitor_cycle_still_completes_the_work():
    """Offloading must not turn into fire-and-forget."""
    app = StockMonitorApp.__new__(StockMonitorApp)
    done = []
    app._run_cycle = lambda: done.append(True)
    await app.monitor_cycle()
    assert done == [True], "the caller must await the work, not just schedule it"


@pytest.mark.anyio
async def test_an_error_in_the_poll_still_propagates():
    """The scheduler logs callback errors; swallowing them here would hide bugs."""
    app = StockMonitorApp.__new__(StockMonitorApp)

    def boom():
        raise RuntimeError("poll failed")

    app._run_cycle = boom
    with pytest.raises(RuntimeError):
        await app.monitor_cycle()


# ── The daily jobs ────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_earnings_check_keeps_the_loop_responsive(monkeypatch):
    monitor = EarningsMonitor(symbols=["AMZN"], dispatcher=None)
    monkeypatch.setattr(monitor, "_check_all", lambda: time.sleep(BLOCK_SECONDS))
    stall = await loop_stall_during(monitor.daily_check())
    assert stall < BLOCK_SECONDS / 2


@pytest.mark.anyio
async def test_news_scan_keeps_the_loop_responsive(monkeypatch):
    monitor = NewsMonitor(store=None)
    monkeypatch.setattr(monitor, "scan", lambda: time.sleep(BLOCK_SECONDS))
    stall = await loop_stall_during(monitor.daily_scan())
    assert stall < BLOCK_SECONDS / 2


@pytest.mark.anyio
async def test_news_scan_does_not_use_the_deprecated_loop_accessor(monkeypatch):
    """get_event_loop() raises outright when no loop is current."""
    def forbidden():
        raise AssertionError("daily_scan must not call get_event_loop()")

    monkeypatch.setattr(asyncio, "get_event_loop", forbidden)
    monitor = NewsMonitor(store=None)
    monkeypatch.setattr(monitor, "scan", lambda: None)
    await monitor.daily_scan()


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── Startup, before the port opens ────────────────────────────────────────────

def test_startup_timings_reach_the_health_endpoint():
    """The probe that times out should be the one that reports why."""
    from fastapi.testclient import TestClient

    from stock_monitor import version
    from stock_monitor.config import NotificationConfig
    from stock_monitor.notifier import NotificationDispatcher
    from stock_monitor.webhook_server import create_webhook_app

    version.record_startup(6.2, {"database": 6.0, "config": 0.2})
    try:
        body = TestClient(
            create_webhook_app(NotificationDispatcher(NotificationConfig()), "")
        ).get("/health").json()
        assert body["startup"]["seconds"] == 6.2
        assert body["startup"]["phases"]["database"] == 6.0
    finally:
        version.startup_timings.clear()


def test_health_is_unchanged_before_any_startup_is_recorded():
    """Importing the app must not make /health claim a startup it never saw."""
    from stock_monitor import version

    version.startup_timings.clear()
    assert "startup" not in version.build_info()


def test_a_slow_phase_is_named_not_just_totalled(caplog):
    """'Startup was slow' is not actionable; 'the database took 6s' is."""
    import logging

    from stock_monitor import main

    with caplog.at_level(logging.WARNING, logger="stock_monitor.main"):
        main.record_startup(6.2, {"database": 6.0})
        logging.getLogger("stock_monitor.main").warning(
            "Startup took %.2fs before the port opened (%s)", 6.2, "database 6.00s")

    assert "database" in caplog.text


def test_the_budget_matches_the_hosts_health_check_timeout():
    """Render's probe gives five seconds; a different number here would not warn."""
    from stock_monitor.main import STARTUP_BUDGET

    assert STARTUP_BUDGET == 5.0
