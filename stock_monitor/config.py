"""Configuration loader: reads config/config.yaml with ${ENV_VAR} interpolation."""

import os
import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


# ── Interpolation ─────────────────────────────────────────────────────────────

def _interpolate_env(text: str) -> str:
    """Replace ${VAR_NAME} placeholders with values from the environment.

    Comment-only lines are left untouched: documentation that mentions the
    ${VAR} syntax should not be treated as a real placeholder, otherwise every
    startup logs a spurious "not set" warning for it.
    """
    def _replace(match: re.Match) -> str:
        var = match.group(1)
        val = os.environ.get(var, "")
        if not val:
            logger.warning("Environment variable %s is not set", var)
        return val

    return "\n".join(
        line if line.lstrip().startswith("#") else re.sub(r"\$\{([^}]+)\}", _replace, line)
        for line in text.splitlines()
    )


def _load_yaml(path: str) -> Dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    return yaml.safe_load(_interpolate_env(text))


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class AlertConfig:
    type: str                         # price_change_pct | volume_spike | price_threshold
    threshold_pct: float = 2.0        # for price_change_pct
    window_minutes: int = 10          # for price_change_pct
    multiplier: float = 2.0           # for volume_spike
    time_adjusted: bool = True        # for volume_spike: scale benchmark to elapsed session time
    above: Optional[float] = None     # for price_threshold
    below: Optional[float] = None     # for price_threshold
    cooldown_minutes: int = 30        # min gap between repeat alerts


@dataclass
class StockConfig:
    symbol: str
    alerts: List[AlertConfig] = field(default_factory=list)


def default_alerts() -> List[AlertConfig]:
    """Alert rules for a symbol added at runtime from the dashboard.

    Symbols typed into the watchlist have no entry in config.yaml, so without
    these they would be polled and charted but could never fire an alert. The
    numbers match the middle of the range used by the configured stocks.
    """
    return [
        AlertConfig(
            type="price_change_pct",
            threshold_pct=3.0,
            window_minutes=10,
            cooldown_minutes=30,
        ),
        AlertConfig(type="volume_spike", multiplier=2.5, cooldown_minutes=60),
    ]


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class DiscordConfig:
    enabled: bool = False
    webhook_url: str = ""


@dataclass
class DesktopConfig:
    enabled: bool = True


@dataclass
class NotificationConfig:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    desktop: DesktopConfig = field(default_factory=DesktopConfig)


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class MonitoringConfig:
    interval_seconds: int = 60
    include_extended_hours: bool = True
    price_history_minutes: int = 60


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/stock_monitor.log"


@dataclass
class AIConfig:
    enabled: bool = False
    api_key: str = ""
    model: str = "claude-opus-4-7"


@dataclass
class EarningsConfig:
    enabled: bool = True
    alert_at_days: List[int] = field(default_factory=lambda: [7, 3, 1])
    check_time: str = "08:00"   # HH:MM ET


@dataclass
class AppConfig:
    stocks: List[StockConfig] = field(default_factory=list)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    tradingview_secret: str = ""
    server: ServerConfig = field(default_factory=ServerConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    earnings: EarningsConfig = field(default_factory=EarningsConfig)


# ── Parsers ───────────────────────────────────────────────────────────────────

def _parse_alert(raw: Dict[str, Any]) -> AlertConfig:
    return AlertConfig(
        type=raw["type"],
        threshold_pct=float(raw.get("threshold_pct", raw.get("threshold", 2.0))),
        window_minutes=int(raw.get("window_minutes", 10)),
        multiplier=float(raw.get("multiplier", 2.0)),
        time_adjusted=bool(raw.get("time_adjusted", True)),
        above=float(raw["above"]) if raw.get("above") is not None else None,
        below=float(raw["below"]) if raw.get("below") is not None else None,
        cooldown_minutes=int(raw.get("cooldown_minutes", 30)),
    )


def load_config(config_path: str = "config/config.yaml") -> AppConfig:
    from dotenv import load_dotenv
    load_dotenv()

    raw = _load_yaml(config_path)

    stocks = [
        StockConfig(
            symbol=s["symbol"],
            alerts=[_parse_alert(a) for a in s.get("alerts", [])],
        )
        for s in raw.get("stocks", [])
    ]

    n = raw.get("notifications", {})
    tg = n.get("telegram", {})
    dc = n.get("discord", {})
    dt = n.get("desktop", {})

    notifications = NotificationConfig(
        telegram=TelegramConfig(
            enabled=bool(tg.get("enabled", False)),
            bot_token=str(tg.get("bot_token", "")),
            chat_id=str(tg.get("chat_id", "")),
        ),
        discord=DiscordConfig(
            enabled=bool(dc.get("enabled", False)),
            webhook_url=str(dc.get("webhook_url", "")),
        ),
        desktop=DesktopConfig(enabled=bool(dt.get("enabled", True))),
    )

    sv = raw.get("server", {})
    mo = raw.get("monitoring", {})
    lg = raw.get("logging", {})
    tv = raw.get("tradingview", {})
    ai = raw.get("ai", {})
    er = raw.get("earnings", {})

    return AppConfig(
        stocks=stocks,
        notifications=notifications,
        tradingview_secret=str(tv.get("webhook_secret", "")),
        server=ServerConfig(
            host=str(sv.get("host", "0.0.0.0")),
            # Cloud hosts (Render, Railway, Fly) assign the port at runtime and
            # expect the app to bind to it; fall back to the configured value.
            port=int(os.environ.get("PORT") or sv.get("port") or 8080),
        ),
        monitoring=MonitoringConfig(
            interval_seconds=int(mo.get("interval_seconds", 60)),
            include_extended_hours=bool(mo.get("include_extended_hours", True)),
            price_history_minutes=int(mo.get("price_history_minutes", 60)),
        ),
        logging=LoggingConfig(
            level=str(lg.get("level", "INFO")),
            file=str(lg.get("file", "logs/stock_monitor.log")),
        ),
        ai=AIConfig(
            enabled=bool(ai.get("enabled", False)),
            api_key=str(ai.get("api_key", "")),
            model=str(ai.get("model", "claude-opus-4-7")),
        ),
        earnings=EarningsConfig(
            enabled=bool(er.get("enabled", True)),
            alert_at_days=list(er.get("alert_at_days", [7, 3, 1])),
            check_time=str(er.get("check_time", "08:00")),
        ),
    )
