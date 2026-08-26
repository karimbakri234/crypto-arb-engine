"""FastAPI app: the local dashboard's control-plane API + static frontend.

Every route here mutates or reads the exact same live objects `main.py`'s
detection loop uses (`ControlState`, `RiskManager`, the strategy list) --
there is no separate database, and no simulation. In particular,
`POST /api/mode` with `{"mode": "live"}` genuinely arms the real `live`
execution path in `execution/executor.py`: the next detection tick that
finds a profitable opportunity will fire real market orders against
whatever balances are pre-funded on the connected exchanges. This route
refuses to do that without an explicit `confirm: true`, but that is a
safety rail, not a substitute for reading the README's "Before running
with real money" section first.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from analytics.metrics import MetricsRegistry
from analytics.recorder import OpportunityRecorder
from core.book import BookStore
from core.control import VALID_MODES, ControlState
from core.rest_manager import RestManager
from dashboard.auth import BasicAuthMiddleware
from dashboard.state import Broadcaster, build_snapshot
from risk.manager import RiskManager
from strategies.base import Strategy

logger = logging.getLogger(__name__)

STATIC_DIR = __file__.replace("server.py", "static")


class ModeChangeRequest(BaseModel):
    mode: str
    confirm: bool = False


class RunRequest(BaseModel):
    running: bool


class RiskLimitsRequest(BaseModel):
    max_notional_per_trade_usd: float | None = None
    daily_loss_limit_usd: float | None = None
    max_trades_per_day: int | None = None


class StrategyToggleRequest(BaseModel):
    enabled: bool


LIVE_CONFIRMATION_MESSAGE = (
    "Switching to live requires confirm=true. This arms real market orders "
    "against pre-funded exchange balances on the next opportunity the "
    "detection loop finds -- there is no simulation once this is set. Read "
    "the README's 'Before running with real money' section first."
)


def create_app(
    control: ControlState,
    risk_manager: RiskManager,
    strategies: list[Strategy],
    recorder: OpportunityRecorder,
    metrics: MetricsRegistry,
    rest_manager: RestManager,
    book_store: BookStore,
    broadcaster: Broadcaster,
    auth_username: str | None = None,
    auth_password: str | None = None,
) -> FastAPI:
    """Build the dashboard FastAPI app bound to one running engine's live state.

    When `auth_username`/`auth_password` are both set, every route --
    including the websocket and static frontend -- requires HTTP Basic
    Auth (see `dashboard/auth.py`). Leave them unset only when
    `DASHBOARD_HOST` is `127.0.0.1` (the default); anything reachable
    beyond localhost should always set these.
    """
    app = FastAPI(title="crypto-arb-engine dashboard")
    if auth_username and auth_password:
        app.add_middleware(BasicAuthMiddleware, username=auth_username, password=auth_password)
    strategy_by_name = {s.name: s for s in strategies}
    for s in strategies:
        control.strategy_enabled.setdefault(s.name, True)

    def snapshot() -> dict:
        return build_snapshot(control, risk_manager, strategies, rest_manager, book_store, metrics)

    @app.get("/api/state")
    def get_state() -> dict:
        return snapshot()

    @app.get("/api/opportunities")
    def get_opportunities(limit: int = 50) -> list[dict]:
        return list(reversed(recorder.all_opportunity_records[-limit:]))

    @app.get("/api/report")
    def get_report(target_usd_per_hour: float | None = None) -> dict:
        return {"report": recorder.generate_summary_report(target_usd_per_hour)}

    @app.get("/api/decay_summary")
    def get_decay_summary() -> dict:
        """Capturable fraction by latency bucket -- see analytics/recorder.py."""
        by_delay: dict[float, list[bool]] = defaultdict(list)
        for d in recorder.all_decay_records:
            by_delay[d["delay_sec"]].append(d["survived"])
        return {
            str(delay): (sum(flags) / len(flags) if flags else None) for delay, flags in sorted(by_delay.items())
        }

    @app.post("/api/mode")
    async def set_mode(req: ModeChangeRequest) -> dict:
        if req.mode not in VALID_MODES:
            raise HTTPException(400, f"mode must be one of {VALID_MODES}")
        if req.mode == "live" and control.mode != "live" and not req.confirm:
            raise HTTPException(400, LIVE_CONFIRMATION_MESSAGE)
        logger.warning("Dashboard: mode changed %s -> %s", control.mode, req.mode)
        control.mode = req.mode
        result = snapshot()
        await broadcaster.publish({"type": "state", "data": result})
        return result

    @app.post("/api/run")
    async def set_running(req: RunRequest) -> dict:
        control.running = req.running
        logger.info("Dashboard: running set to %s", req.running)
        result = snapshot()
        await broadcaster.publish({"type": "state", "data": result})
        return result

    @app.post("/api/emergency_stop")
    async def trigger_emergency_stop() -> dict:
        control.emergency_stop = True
        risk_manager.trigger_emergency_stop("Triggered from dashboard")
        result = snapshot()
        await broadcaster.publish({"type": "state", "data": result})
        return result

    @app.post("/api/emergency_stop/clear")
    async def clear_emergency_stop() -> dict:
        control.emergency_stop = False
        risk_manager.clear_emergency_stop()
        result = snapshot()
        await broadcaster.publish({"type": "state", "data": result})
        return result

    @app.post("/api/risk_limits")
    async def update_risk_limits(req: RiskLimitsRequest) -> dict:
        if req.max_notional_per_trade_usd is not None:
            risk_manager.limits.max_notional_per_trade_usd = req.max_notional_per_trade_usd
        if req.daily_loss_limit_usd is not None:
            risk_manager.limits.daily_loss_limit_usd = req.daily_loss_limit_usd
        if req.max_trades_per_day is not None:
            risk_manager.limits.max_trades_per_day = req.max_trades_per_day
        logger.info("Dashboard: risk limits updated: %s", req)
        result = snapshot()
        await broadcaster.publish({"type": "state", "data": result})
        return result

    @app.post("/api/strategy/{name}")
    async def toggle_strategy(name: str, req: StrategyToggleRequest) -> dict:
        if name not in strategy_by_name:
            raise HTTPException(404, f"unknown strategy {name!r}")
        control.strategy_enabled[name] = req.enabled
        logger.info("Dashboard: strategy %s enabled=%s", name, req.enabled)
        result = snapshot()
        await broadcaster.publish({"type": "state", "data": result})
        return result

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        broadcaster.register(websocket)
        try:
            await websocket.send_json({"type": "state", "data": snapshot()})
            while True:
                # Clients don't need to send anything; this just detects disconnects.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            broadcaster.unregister(websocket)

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
