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
from typing import Optional

import uvicorn

from .alert_engine import AlertEngine
from .config import AppConfig, load_config
from .data_feed import StockDataFeed, get_market_session
from .notifier import NotificationDispatcher
from .scheduler import MarketScheduler
from .webhook_server import create_webhook_app


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
        self.engine     = AlertEngine(config, self.data_feed)
        self.dispatcher = NotificationDispatcher(config.notifications)
        self.scheduler  = MarketScheduler(
            interval_seconds=config.monitoring.interval_seconds,
            include_extended_hours=config.monitoring.include_extended_hours,
        )
        self._log = logging.getLogger(__name__)

    # ── Core monitoring loop ──────────────────────────────────────────────────

    async def monitor_cycle(self) -> None:
        session   = get_market_session()
        stock_map = {sc.symbol: sc for sc in self.config.stocks}

        for symbol, stock_cfg in stock_map.items():
            try:
                data = self.data_feed.get_current_data(symbol)
                if data is None:
                    self._log.warning("No data for %s — skipping", symbol)
                    continue

                self._log.info(
                    "%-6s  $%8.2f  %+.2f%%  vol=%10s  [%s]",
                    symbol,
                    data["price"],
                    data["change_pct"],
                    f"{data['volume']:,}",
                    session,
                )

                for event in self.engine.evaluate(symbol, stock_cfg, data):
                    self.dispatcher.dispatch(event, session)

            except Exception as exc:
                self._log.error("Error monitoring %s: %s", symbol, exc, exc_info=True)

    # ── Run ───────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._log.info("=" * 60)
        self._log.info("Stock Monitor v1.0.0  starting up")
        self._log.info("Watching: %s", [s.symbol for s in self.config.stocks])
        self._log.info(
            "Notifications — telegram: %s  discord: %s  desktop: %s",
            self.config.notifications.telegram.enabled,
            self.config.notifications.discord.enabled,
            self.config.notifications.desktop.enabled,
        )
        self._log.info("=" * 60)

        self.scheduler.add_callback(self.monitor_cycle)
        self.scheduler.start()

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
    app = StockMonitorApp(config)
    asyncio.run(app.run())
