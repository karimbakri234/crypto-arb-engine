"""Shared state-snapshot building and websocket broadcasting.

Kept separate from `dashboard/server.py` so `main.py`'s detection loop
can import `build_snapshot`/`Broadcaster` without importing FastAPI's
route definitions, and so the REST `/api/state` handler and the
websocket's initial push use exactly the same snapshot shape.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from analytics.metrics import MetricsRegistry
from core.book import BookStore
from core.control import ControlState
from core.rest_manager import RestManager
from risk.manager import RiskManager
from strategies.base import Strategy

logger = logging.getLogger(__name__)


def build_snapshot(
    control: ControlState,
    risk_manager: RiskManager,
    strategies: list[Strategy],
    rest_manager: RestManager,
    book_store: BookStore,
    metrics: MetricsRegistry,
) -> dict[str, Any]:
    """Build the full dashboard state snapshot served over REST and websocket."""
    return {
        "mode": control.mode,
        "running": control.running,
        "emergency_stop": control.emergency_stop,
        "uptime_sec": time.time() - control.started_at,
        "risk_limits": {
            "max_notional_per_trade_usd": risk_manager.limits.max_notional_per_trade_usd,
            "daily_loss_limit_usd": risk_manager.limits.daily_loss_limit_usd,
            "max_trades_per_day": risk_manager.limits.max_trades_per_day,
        },
        "daily_pnl_usd": risk_manager.daily_pnl_usd,
        "daily_trade_count": risk_manager.daily_trade_count,
        "consecutive_failures": risk_manager.consecutive_failures,
        "connected_venues": sorted(rest_manager.clients.keys()),
        "symbols_tracked": len(book_store.all_symbols()),
        "strategies": [
            {"name": s.name, "enabled": control.strategy_enabled.get(s.name, True)} for s in strategies
        ],
        "metrics": metrics.snapshot(),
    }


class Broadcaster:
    """Fan-out of JSON events to every connected `/ws` client.

    A dead/closed connection is dropped on the next publish rather than
    raising -- the detection loop's tick must never be blocked or
    crashed by a client that closed its tab mid-broadcast.
    """

    def __init__(self) -> None:
        self._clients: list[Any] = []

    def register(self, websocket: Any) -> None:
        self._clients.append(websocket)

    def unregister(self, websocket: Any) -> None:
        if websocket in self._clients:
            self._clients.remove(websocket)

    async def publish(self, event: dict[str, Any]) -> None:
        if not self._clients:
            return
        dead = []
        for websocket in list(self._clients):
            try:
                await websocket.send_json(event)
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            self.unregister(websocket)
