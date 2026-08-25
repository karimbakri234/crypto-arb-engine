"""Tests for the local dashboard's control-plane API (dashboard/server.py).

Uses FastAPI's TestClient (synchronous, in-process, no real network) --
this exercises the exact same route handlers a real browser would hit,
against a real (test) ControlState/RiskManager/strategy list, just
without an actual detection loop running behind them.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from analytics.metrics import MetricsRegistry
from analytics.recorder import OpportunityRecorder
from core.book import BookStore
from core.control import ControlState
from core.market_state import MarketState
from core.rest_manager import RestManager
from dashboard.server import create_app
from dashboard.state import Broadcaster
from risk.limits import RiskLimits
from risk.manager import RiskManager
from strategies.base import Opportunity, Strategy


class FakeStrategy(Strategy):
    name = "fake_strategy"

    def scan(self, market_state: MarketState) -> list[Opportunity]:
        return []


def make_client(tmp_path) -> tuple[TestClient, ControlState, RiskManager]:
    control = ControlState()
    risk_manager = RiskManager(RiskLimits())
    strategies = [FakeStrategy()]
    recorder = OpportunityRecorder(output_dir=str(tmp_path))
    metrics = MetricsRegistry()
    rest_manager = RestManager([])
    book_store = BookStore()
    broadcaster = Broadcaster()

    app = create_app(control, risk_manager, strategies, recorder, metrics, rest_manager, book_store, broadcaster)
    return TestClient(app), control, risk_manager


def test_get_state_reflects_defaults(tmp_path):
    client, control, _ = make_client(tmp_path)

    resp = client.get("/api/state")

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "monitor"
    assert body["running"] is True
    assert body["strategies"] == [{"name": "fake_strategy", "enabled": True}]


def test_switching_to_live_without_confirm_is_rejected(tmp_path):
    client, control, _ = make_client(tmp_path)

    resp = client.post("/api/mode", json={"mode": "live"})

    assert resp.status_code == 400
    assert "confirm" in resp.json()["detail"].lower()
    assert control.mode == "monitor"  # unchanged


def test_switching_to_live_with_confirm_succeeds(tmp_path):
    client, control, _ = make_client(tmp_path)

    resp = client.post("/api/mode", json={"mode": "live", "confirm": True})

    assert resp.status_code == 200
    assert resp.json()["mode"] == "live"
    assert control.mode == "live"


def test_switching_back_from_live_to_paper_needs_no_confirm(tmp_path):
    client, control, _ = make_client(tmp_path)
    client.post("/api/mode", json={"mode": "live", "confirm": True})

    resp = client.post("/api/mode", json={"mode": "paper"})

    assert resp.status_code == 200
    assert control.mode == "paper"


def test_invalid_mode_is_rejected(tmp_path):
    client, _, _ = make_client(tmp_path)

    resp = client.post("/api/mode", json={"mode": "yolo"})

    assert resp.status_code == 400


def test_run_toggle(tmp_path):
    client, control, _ = make_client(tmp_path)

    resp = client.post("/api/run", json={"running": False})

    assert resp.status_code == 200
    assert control.running is False


def test_emergency_stop_and_clear(tmp_path):
    client, control, risk_manager = make_client(tmp_path)

    resp = client.post("/api/emergency_stop")
    assert resp.status_code == 200
    assert control.emergency_stop is True
    assert risk_manager.limits.emergency_stop is True

    resp = client.post("/api/emergency_stop/clear")
    assert resp.status_code == 200
    assert control.emergency_stop is False
    assert risk_manager.limits.emergency_stop is False


def test_update_risk_limits(tmp_path):
    client, _, risk_manager = make_client(tmp_path)

    resp = client.post("/api/risk_limits", json={"daily_loss_limit_usd": 42.0})

    assert resp.status_code == 200
    assert risk_manager.limits.daily_loss_limit_usd == 42.0


def test_toggle_unknown_strategy_404s(tmp_path):
    client, _, _ = make_client(tmp_path)

    resp = client.post("/api/strategy/does_not_exist", json={"enabled": False})

    assert resp.status_code == 404


def test_toggle_known_strategy(tmp_path):
    client, control, _ = make_client(tmp_path)

    resp = client.post("/api/strategy/fake_strategy", json={"enabled": False})

    assert resp.status_code == 200
    assert control.strategy_enabled["fake_strategy"] is False


def test_websocket_receives_initial_state(tmp_path):
    client, _, _ = make_client(tmp_path)

    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "state"
        assert msg["data"]["mode"] == "monitor"
