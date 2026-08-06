"""
Application entry-point: wires all subsystems together and runs as a daemon.

Architecture
────────────
┌──────────────────────────────────────────────────────────────────┐
│  StockMonitorApp                                                  │
│                                                                   │
│  MarketScheduler ──tick()──► monitor_cycle()                     │
│                                  │                                │
│                       StockDataFeed.get_current_data()           │
│                                  │                                │
│                       AlertEngine.evaluate()                     │
│                                  │                                │
│                       NotificationDispatcher.dispatch()          │
│                                                                   │
│  FastAPI  ──POST /webhook/tradingview──► NotificationDispatcher  │
└──────────────────────────────────────────────────────────────────┘
"""

import asyncio
import logging
import logging.handlers
import signal
import sys
from pathlib import Path
from typing import Dict, List, Optional

import uvicorn

from . import db
from .alert_engine import AlertEngine
from .config import AppConfig, StockConfig, default_alerts, load_config
from .dashboard import (
    prune_price_cache,
    set_analyst,
    set_data_feed,
    set_news_monitor,
    update_price_cache,
)
from .data_feed import StockDataFeed, get_market_session
from .earnings import EarningsMonitor
from .news_monitor import NewsMonitor
from .notifier import NotificationDispatcher
from .scheduler import MarketScheduler
from .store import alert_store, portfolio_store, watchlist_store
from .telegram_bot import TelegramCommandBot
from .version import build_label
from .webhook_server import create_webhook_app

#: The pre-market news scan runs 30 minutes before the 09:30 ET opening bell.
NEWS_SCAN_TIME = (9, 0)


# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging(level: str, log_file: str) -> None:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Quiet noisy third-party loggers
    for noisy in ("yfinance", "urllib3", "httpx", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ── Application ───────────────────────────────────────────────────────────────

class StockMonitorApp:
    def __init__(self, config: AppConfig) -> None:
        self.config     = config
        self.data_feed  = StockDataFeed()
        # Share the feed with the dashboard so its analysis endpoint reuses
        # this instance instead of opening a second one.
        set_data_feed(self.data_feed)
        self.engine     = AlertEngine(config, self.data_feed)
        self.dispatcher = NotificationDispatcher(config.notifications)
        self.scheduler  = MarketScheduler(
            interval_seconds=config.monitoring.interval_seconds,
            include_extended_hours=config.monitoring.include_extended_hours,
        )
        # Tracks last regular-session price per symbol for since-close calculation
        self._regular_close: Dict[str, Optional[float]] = {}
        self._log = logging.getLogger(__name__)

        # ── Optional subsystems ───────────────────────────────────────────────
        self._analyst = None
        if config.ai.enabled and config.ai.api_key:
            from .ai_analyst import StockAnalyst
            self._analyst = StockAnalyst(api_key=config.ai.api_key, model=config.ai.model)
            # Share it with the dashboard so /api/portfolio/analyze can use it.
            set_analyst(self._analyst)
            self._log.info("AI analyst enabled (model=%s)", config.ai.model)

        self._earnings: Optional[EarningsMonitor] = None
        if config.earnings.enabled:
            self._earnings = EarningsMonitor(
                symbols=[s.symbol for s in config.stocks],
                dispatcher=self.dispatcher,
                alert_days=config.earnings.alert_at_days,
            )
            h, m = map(int, config.earnings.check_time.split(":"))
            self.scheduler.add_daily_job(self._earnings.daily_check, hour=h, minute=m)
            self._log.info(
                "Earnings monitor enabled — daily check at %s ET", config.earnings.check_time
            )

        # Breaking news for held tickers, scanned once before the open. The
        # dashboard reads the same instance, so its tab serves the cached scan
        # instead of re-fetching every headline on each page load.
        self._news = NewsMonitor(portfolio_store)
        set_news_monitor(self._news)
        self.scheduler.add_daily_job(
            self._news.daily_scan, hour=NEWS_SCAN_TIME[0], minute=NEWS_SCAN_TIME[1]
        )

    # ── Watchlist ─────────────────────────────────────────────────────────────

    def watched_stocks(self) -> List[StockConfig]:
        """Symbols to poll, read fresh each cycle so dashboard edits take effect.

        The store falls back to config.yaml while it is empty; a symbol that
        exists in config keeps its tuned alert rules, and one added from the UI
        gets the defaults.
        """
        configured = {s.symbol: s for s in self.config.stocks}
        symbols = watchlist_store.all() or list(configured)
        return [
            configured.get(symbol) or StockConfig(symbol=symbol, alerts=default_alerts())
            for symbol in symbols
        ]

    # ── Core monitoring loop ──────────────────────────────────────────────────

    async def monitor_cycle(self) -> None:
        """Scheduler entry point. The work itself runs off the event loop.

        The poll is network-bound from end to end — yfinance quotes in,
        Telegram sends out, a database read for the watchlist — and awaiting it
        here froze the web server for the poll's full duration. When Yahoo is
        slow that is tens of seconds, during which /health cannot answer;
        Render gives the probe five seconds and then restarts the instance.
        """
        await asyncio.to_thread(self._run_cycle)

    def _run_cycle(self) -> None:
        session   = get_market_session()
        stock_map = {sc.symbol: sc for sc in self.watched_stocks()}
        prune_price_cache(stock_map)

        # One request for the whole watchlist. Quoting each symbol separately
        # cost 26 requests a minute for thirteen symbols — 1,560 an hour, which
        # is what Yahoo started rate-limiting, and a throttled account then
        # fails to return the pre-market bars the dashboard is waiting for.
        quotes = self.data_feed.get_current_data_batch(list(stock_map))

        for symbol, stock_cfg in stock_map.items():
            try:
                data = quotes.get(symbol) or self.data_feed.get_current_data(symbol)
                if data is None:
                    self._log.warning("No data for %s — skipping", symbol)
                    continue

                # Track regular-session price for Telegram bot & alert engine
                if session == "regular":
                    self._regular_close[symbol] = data["price"]
                elif data.get("regular_close"):
                    self._regular_close[symbol] = data["regular_close"]

                self._log.info(
                    "%-6s  $%8.2f  %+.2f%%  sc=%s  vol=%10s  [%s]",
                    symbol,
                    data["price"],
                    data["change_pct"],
                    f"{data['since_close_pct']:+.2f}%" if data.get("since_close_pct") is not None else "N/A",
                    f"{data['volume']:,}",
                    session,
                )

                update_price_cache(data)

                for event in self.engine.evaluate(symbol, stock_cfg, data):
                    self.dispatcher.dispatch(event, session)
                    alert_store.add(
                        symbol=event.symbol,
                        alert_type=event.alert_type,
                        message=event.message,
                        severity=event.severity,
                    )

            except Exception as exc:
                self._log.error("Error monitoring %s: %s", symbol, exc, exc_info=True)

    # ── Run ───────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._log.info("=" * 60)
        self._log.info("Stock Monitor v1.0.0  starting up")
        self._log.info("Watching: %s", [s.symbol for s in self.watched_stocks()])
        self._log.info(
            "Notifications — telegram: %s  discord: %s  desktop: %s",
            self.config.notifications.telegram.enabled,
            self.config.notifications.discord.enabled,
            self.config.notifications.desktop.enabled,
        )
        self._log.info("=" * 60)

        self.scheduler.add_callback(self.monitor_cycle)
        self.scheduler.start()

        # Start Telegram command bot if token is configured
        tg_cfg = self.config.notifications.telegram
        if tg_cfg.enabled and tg_cfg.bot_token:
            tg_bot = TelegramCommandBot(
                bot_token=tg_cfg.bot_token,
                data_feed=self.data_feed,
                monitored_symbols=[s.symbol for s in self.watched_stocks()],
                regular_close_ref=self._regular_close,
                analyst=self._analyst,
                authorized_chat_id=tg_cfg.chat_id,
            )
            asyncio.create_task(tg_bot.poll_loop())
            self._log.info("Telegram command bot active — send a ticker to your bot")

        webhook_app = create_webhook_app(
            self.dispatcher, self.config.tradingview_secret
        )
        uv_config = uvicorn.Config(
            app=webhook_app,
            host=self.config.server.host,
            port=self.config.server.port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(uv_config)

        self._log.info(
            "Webhook server: http://%s:%d",
            self.config.server.host,
            self.config.server.port,
        )
        self._log.info(
            "TradingView URL: http://<YOUR_PUBLIC_IP>:%d/webhook/tradingview",
            self.config.server.port,
        )
        self._log.info("API docs: http://127.0.0.1:%d/docs", self.config.server.port)

        # Graceful shutdown on SIGINT / SIGTERM
        loop = asyncio.get_event_loop()

        def _shutdown(sig_num: int, _frame) -> None:  # type: ignore[type-arg]
            self._log.info("Signal %d received — shutting down…", sig_num)
            self.scheduler.stop()
            server.should_exit = True

        signal.signal(signal.SIGINT,  _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        await server.serve()
        self._log.info("Stock Monitor stopped.")


# ── Public runner ─────────────────────────────────────────────────────────────

def run_app(config_path: str = "config/config.yaml") -> None:
    config = load_config(config_path)
    setup_logging(config.logging.level, config.logging.file)
    # Warm the build identity before serving. It is lru_cached but the first
    # call may shell out to git, and the first caller must not be the host's
    # health probe — that request has a five-second budget.
    logging.getLogger(__name__).info("Build: %s", build_label())
    # Connect to PostgreSQL when DATABASE_URL is set; otherwise the stores stay
    # in memory and the monitor runs exactly as before.
    db.init_db()
    # First run only: copy config.yaml's symbols into the editable watchlist.
    # Once it holds anything, the user's edits are the source of truth.
    watchlist_store.seed([s.symbol for s in config.stocks])
    app = StockMonitorApp(config)
    asyncio.run(app.run())
