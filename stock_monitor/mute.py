"""Temporary silence for outgoing alerts, controlled from Telegram.

The problem this solves: on a day when a position is moving, the same stock
fires a price-change alert every cooldown window, and the only way to stop the
stream was to edit the configuration and redeploy. So the practical response to
a noisy alert was to ignore the whole channel — which defeats having alerts.

A mute is a deadline, not a flag: "quiet until 16:00", never "quiet until I
remember to turn it back on". There is a global mute and a per-symbol one, and
the sooner deadline does not win — they are independent, so muting one stock
does not disturb a global mute and vice versa.

State is in memory on purpose. It is small, it is per-deployment, and the one
behaviour a restart changes is that alerts resume — which is the safe
direction to fail in: an alert arriving after a restart is a nuisance, a
missing alert is a missed trade. Every mute message says when it expires, so
nothing here is silently permanent.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

#: The key under which the global mute is stored. Not a legal ticker, so it
#: cannot collide with one.
ALL = "*"

_lock = threading.Lock()
_until: Dict[str, datetime] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def mute(symbol: str = ALL, minutes: int = 60) -> datetime:
    """Silence *symbol* (or everything) for *minutes*. Returns the deadline."""
    deadline = _now() + timedelta(minutes=max(1, int(minutes)))
    with _lock:
        _until[symbol.upper() if symbol != ALL else ALL] = deadline
    return deadline


def mute_until(deadline: datetime, symbol: str = ALL) -> datetime:
    """Silence *symbol* until a specific moment — "until tomorrow morning"."""
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    with _lock:
        _until[symbol.upper() if symbol != ALL else ALL] = deadline
    return deadline


def unmute(symbol: str = ALL) -> None:
    """Lift one mute. ``unmute(ALL)`` lifts the global one, not every mute."""
    with _lock:
        _until.pop(symbol.upper() if symbol != ALL else ALL, None)


def unmute_everything() -> None:
    with _lock:
        _until.clear()


def muted_until(symbol: str = ALL) -> Optional[datetime]:
    """When this symbol's own mute expires, or None if it is not muted."""
    key = symbol.upper() if symbol != ALL else ALL
    with _lock:
        deadline = _until.get(key)
        if deadline is None:
            return None
        if deadline <= _now():
            # Expired: drop it so the registry does not grow without bound.
            del _until[key]
            return None
        return deadline


def is_muted(symbol: str = "") -> bool:
    """True when an alert for *symbol* should not be sent right now."""
    if muted_until(ALL) is not None:
        return True
    return bool(symbol) and muted_until(symbol) is not None


def active() -> Dict[str, datetime]:
    """Every mute still in force, expired entries pruned. Newest deadline last."""
    with _lock:
        keys = list(_until)
    live = {}
    for key in keys:
        deadline = muted_until(key)
        if deadline is not None:
            live[key] = deadline
    return dict(sorted(live.items(), key=lambda kv: kv[1]))
