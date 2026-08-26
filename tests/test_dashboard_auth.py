"""Tests for dashboard/auth.py's Basic Auth gate.

Confirms the dashboard is wide open with no credentials configured
(matching the localhost-only default) but fully locked -- API routes,
static files, and the websocket handshake alike -- once
`DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` are set.
"""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from analytics.metrics import MetricsRegistry
from analytics.recorder import OpportunityRecorder
from core.book import BookStore
from core.control import ControlState
from core.rest_manager import RestManager
from dashboard.server import create_app
from dashboard.state import Broadcaster
from risk.limits import RiskLimits
from risk.manager import RiskManager


def _basic_auth_header(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def make_client(tmp_path, *, auth_username=None, auth_password=None) -> TestClient:
    control = ControlState()
    risk_manager = RiskManager(RiskLimits())
    recorder = OpportunityRecorder(output_dir=str(tmp_path))
    metrics = MetricsRegistry()
    rest_manager = RestManager([])
    book_store = BookStore()
    broadcaster = Broadcaster()

    app = create_app(
        control, risk_manager, [], recorder, metrics, rest_manager, book_store, broadcaster,
        auth_username=auth_username, auth_password=auth_password,
    )
    return TestClient(app)


def test_no_credentials_configured_leaves_api_open(tmp_path):
    client = make_client(tmp_path)

    resp = client.get("/api/state")

    assert resp.status_code == 200


def test_credentials_configured_reject_unauthenticated_request(tmp_path):
    client = make_client(tmp_path, auth_username="admin", auth_password="secret")

    resp = client.get("/api/state")

    assert resp.status_code == 401
    assert "basic" in resp.headers["www-authenticate"].lower()


def test_credentials_configured_reject_wrong_password(tmp_path):
    client = make_client(tmp_path, auth_username="admin", auth_password="secret")

    resp = client.get("/api/state", headers=_basic_auth_header("admin", "wrong"))

    assert resp.status_code == 401


def test_credentials_configured_accept_correct_login(tmp_path):
    client = make_client(tmp_path, auth_username="admin", auth_password="secret")

    resp = client.get("/api/state", headers=_basic_auth_header("admin", "secret"))

    assert resp.status_code == 200


def test_static_frontend_also_requires_auth(tmp_path):
    client = make_client(tmp_path, auth_username="admin", auth_password="secret")

    resp = client.get("/")

    assert resp.status_code == 401


def test_websocket_without_credentials_is_refused(tmp_path):
    client = make_client(tmp_path, auth_username="admin", auth_password="secret")

    try:
        with client.websocket_connect("/ws"):
            raised = False
    except WebSocketDisconnect:
        raised = True

    assert raised


def test_websocket_with_credentials_connects(tmp_path):
    client = make_client(tmp_path, auth_username="admin", auth_password="secret")

    with client.websocket_connect("/ws", headers=_basic_auth_header("admin", "secret")) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "state"
