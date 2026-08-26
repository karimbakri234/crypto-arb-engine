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
from config.settings import get_credentials
from config.venues import CEX_VENUES
from core.book import BookStore
from core.control import VALID_MODES, ControlState
from core.rest_manager import RestManager
from dashboard.auth import BasicAuthMiddleware, WsTicketStore
from dashboard.credentials import write_credentials
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


class VenueCredentialsRequest(BaseModel):
    api_key: str
    secret: str
    passphrase: str | None = None


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
):
    """Build the dashboard FastAPI app bound to one running engine's live state.

    When `auth_username`/`auth_password` are both set, every route --
    including the websocket and static frontend -- requires HTTP Basic
    Auth (see `dashboard/auth.py`). Leave them unset only when
    `DASHBOARD_HOST` is `127.0.0.1` (the default); anything reachable
    beyond localhost should always set these.
    """
    app = FastAPI(title="crypto-arb-engine dashboard")
    ticket_store = WsTicketStore()
    strategy_by_name = {s.name: s for s in strategies}
    for s in strategies:
        control.strategy_enabled.setdefault(s.name, True)

    def snapshot() -> dict:
        return build_snapshot(control, risk_manager, strategies, rest_manager, book_store, metrics)

    @app.get("/api/state")
    def get_state() -> dict:
        return snapshot()

    @app.get("/api/ws_ticket")
    def get_ws_ticket() -> dict:
        """A short-lived ticket the frontend swaps for `/ws` access.

        See `dashboard.auth.WsTicketStore` for why: some browsers don't
        reliably resend cached HTTP Basic Auth credentials on a raw
        WebSocket handshake, so the page fetches this over a normal
        (reliably authenticated) request first instead.
        """
        return {"ticket": ticket_store.issue()}

    @app.get("/api/opportunities")
    def get_opportunities(limit: int = 50) -> list[dict]:
        # all_opportunity_records is a deque (bounded rolling history, see
        # analytics/recorder.py), which doesn't support slicing directly.
        return list(reversed(list(recorder.all_opportunity_records)[-limit:]))

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

    @app.get("/api/venues")
    def get_venues() -> list[dict]:
        """Every configured CEX venue's connection + credential status.

        `has_credentials` never returns the key/secret itself -- only
        whether one is set -- so this is safe to expose without leaking
        anything back out over the wire.
        """
        venues = []
        for venue_id, venue in CEX_VENUES.items():
            creds = get_credentials(venue_id)
            venues.append(
                {
                    "id": venue_id,
                    "name": venue.name,
                    "connected": venue_id in rest_manager.clients,
                    "has_credentials": bool(creds.get("apiKey")),
                    "is_derivatives": venue.is_derivatives,
                }
            )
        return venues

    @app.post("/api/venues/{venue_id}/credentials")
    async def set_venue_credentials(venue_id: str, req: VenueCredentialsRequest) -> dict:
        """Save API credentials for `venue_id` and reconnect it immediately.

        Persists to `.env` (see dashboard/credentials.py) so a restart
        doesn't lose them, then tears down and recreates that venue's ccxt
        client so it can be used for `live` order placement in this same
        run without needing to restart the process. See that module's
        docstring for the plain-HTTP transport caveat.
        """
        if venue_id not in CEX_VENUES:
            raise HTTPException(404, f"unknown venue {venue_id!r}")
        write_credentials(venue_id, req.api_key, req.secret, req.passphrase)
        reconnected = await rest_manager.reconnect(venue_id)
        logger.warning("Dashboard: credentials updated for venue=%s reconnected=%s", venue_id, reconnected)
        result = snapshot()
        await broadcaster.publish({"type": "state", "data": result})
        return {"id": venue_id, "connected": reconnected, "has_credentials": True}

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

    if auth_username and auth_password:
        # Wraps the fully-built app rather than app.add_middleware(...) so
        # this function can hand the same ticket_store instance the
        # /api/ws_ticket route above already issues from -- add_middleware
        # only stores a (class, kwargs) pair and builds the instance
        # later, with no way to get a handle back on it.
        return BasicAuthMiddleware(app, auth_username, auth_password, ticket_store=ticket_store)
    return app
