"""In-memory + JSON-persisted portfolio store.

Holds the user's manually entered positions (ticker, quantity, entry price).
Thread-safe and dependency-free so it can be unit-tested in isolation.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

DEFAULT_STORE_PATH = Path(os.environ.get("STORE_PATH", "data/portfolio.json"))


class Holding(BaseModel):
    """A single manually entered position."""

    ticker: str = Field(..., min_length=1, max_length=12)
    quantity: float = Field(..., gt=0)
    entry_price: float = Field(..., gt=0)

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        value = value.strip().upper()
        if not value or not all(c.isalnum() or c in ".-^" for c in value):
            raise ValueError("ticker must contain only letters, digits, '.', '-' or '^'")
        return value


class PortfolioStore:
    """Thread-safe store for holdings, persisted to a JSON file.

    Pass ``path=None`` to keep the store purely in memory (used by tests).
    """

    def __init__(self, path: Optional[Path] = DEFAULT_STORE_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._holdings: Dict[str, Holding] = {}
        if self._path is not None:
            self._load()

    # -- public API ---------------------------------------------------------

    def upsert(self, holding: Holding) -> Holding:
        with self._lock:
            self._holdings[holding.ticker] = holding
            self._save()
        return holding

    def get(self, ticker: str) -> Optional[Holding]:
        with self._lock:
            return self._holdings.get(ticker.strip().upper())

    def all(self) -> List[Holding]:
        with self._lock:
            return list(self._holdings.values())

    def delete(self, ticker: str) -> bool:
        with self._lock:
            removed = self._holdings.pop(ticker.strip().upper(), None) is not None
            if removed:
                self._save()
        return removed

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for item in raw:
                try:
                    # Extra keys from older schema versions (e.g. purchase_date)
                    # are dropped rather than failing the whole load.
                    holding = Holding(
                        ticker=item.get("ticker", ""),
                        quantity=item.get("quantity", 0),
                        entry_price=item.get("entry_price", 0),
                    )
                except ValueError:
                    logger.warning("skipping incompatible portfolio record: %s", item)
                    continue
                self._holdings[holding.ticker] = holding
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            logger.error("Failed to load portfolio file %s: %s", self._path, exc)

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = [h.model_dump() for h in self._holdings.values()]
            self._path.write_text(
                json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:
            logger.error("Failed to persist portfolio file %s: %s", self._path, exc)
