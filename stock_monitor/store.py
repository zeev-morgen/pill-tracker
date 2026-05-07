"""
In-memory store for recent alerts — shared between the engine and the dashboard.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import List

MAX_ALERTS = 100


@dataclass
class AlertRecord:
    timestamp: str
    symbol: str
    alert_type: str
    message: str
    severity: str


class AlertStore:
    def __init__(self) -> None:
        self._alerts: deque = deque(maxlen=MAX_ALERTS)

    def add(self, symbol: str, alert_type: str, message: str, severity: str) -> None:
        self._alerts.appendleft(
            AlertRecord(
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                symbol=symbol,
                alert_type=alert_type,
                message=message,
                severity=severity,
            )
        )

    def recent(self, n: int = 50) -> List[AlertRecord]:
        return list(self._alerts)[:n]


# Global singleton shared across all modules
alert_store = AlertStore()
