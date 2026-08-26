"""Tests for the dashboard's /api/venues endpoints (dashboard/server.py).

`RestManager.reconnect` is monkeypatched everywhere here to avoid any real
network I/O (it normally opens a live ccxt connection) -- see README
"Testing": no live network calls. `dashboard.credentials.ENV_FILE_PATH` is
redirected to a temp file so these tests never touch the real `.env`.
"""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

import dashboard.credentials as credentials_module
from analytics.metrics import MetricsRegistry
from analytics.recorder import OpportunityRecorder
from config.venues import CEX_VENUES
from core.book import BookStore
from core.control import ControlState
from core.rest_manager import RestManager
from dashboard.server import create_app
from dashboard.state import Broadcaster
from risk.limits import RiskLimits
from risk.manager import RiskManager


def make_client(tmp_path, monkeypatch) -> tuple[TestClient, RestManager]:
    monkeypatch.setattr(credentials_module, "ENV_FILE_PATH", str(tmp_path / ".env"))

    control = ControlState()
    risk_manager = RiskManager(RiskLimits())
    recorder = OpportunityRecorder(output_dir=str(tmp_path))
    metrics = MetricsRegistry()
    rest_manager = RestManager([])
    book_store = BookStore()
    broadcaster = Broadcaster()

    app = create_app(control, risk_manager, [], recorder, metrics, rest_manager, book_store, broadcaster)
    return TestClient(app), rest_manager


def test_get_venues_lists_all_configured_venues(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)

    resp = client.get("/api/venues")

    assert resp.status_code == 200
    venues = resp.json()
    # Compare against config rather than naming a venue: the venue list is
    # periodically re-tuned, and this test is about the endpoint exposing
    # all of them, not about any particular exchange being present.
    assert {v["id"] for v in venues} == set(CEX_VENUES)
    assert len(venues) >= 15
    sample = venues[0]
    assert sample["connected"] is False
    assert sample["has_credentials"] is False


def test_venue_credentials_never_echoes_the_secret(tmp_path, monkeypatch):
    client, rest_manager = make_client(tmp_path, monkeypatch)

    async def fake_reconnect(venue_id: str) -> bool:
        return True

    monkeypatch.setattr(rest_manager, "reconnect", fake_reconnect)
    # write_credentials sets real process env vars (by design, so a live
    # reconnect picks them up) -- clean those up so this test can't leak
    # a fake credential into the rest of the suite's process environment.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_SECRET", raising=False)

    try:
        resp = client.post("/api/venues/gemini/credentials", json={"api_key": "AKIA_SECRET", "secret": "shh"})

        assert resp.status_code == 200
        body = resp.json()
        assert "AKIA_SECRET" not in str(body)
        assert "shh" not in str(body)
        assert body == {"id": "gemini", "connected": True, "has_credentials": True}
    finally:
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("GEMINI_SECRET", None)


def test_venue_credentials_persisted_to_env_file_and_reflected_in_status(tmp_path, monkeypatch):
    client, rest_manager = make_client(tmp_path, monkeypatch)

    async def fake_reconnect(venue_id: str) -> bool:
        return False  # e.g. bad key -- reconnect attempted but failed

    monkeypatch.setattr(rest_manager, "reconnect", fake_reconnect)

    try:
        resp = client.post("/api/venues/kraken/credentials", json={"api_key": "k1", "secret": "s1"})
        assert resp.status_code == 200
        assert resp.json() == {"id": "kraken", "connected": False, "has_credentials": True}

        env_contents = (tmp_path / ".env").read_text()
        assert "KRAKEN_API_KEY=k1" in env_contents
        assert "KRAKEN_SECRET=s1" in env_contents

        venues = client.get("/api/venues").json()
        kraken = next(v for v in venues if v["id"] == "kraken")
        assert kraken["has_credentials"] is True
    finally:
        os.environ.pop("KRAKEN_API_KEY", None)
        os.environ.pop("KRAKEN_SECRET", None)


def test_unknown_venue_credentials_404s(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)

    resp = client.post("/api/venues/does_not_exist/credentials", json={"api_key": "k", "secret": "s"})

    assert resp.status_code == 404
