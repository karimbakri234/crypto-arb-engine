"""Tests for execution.reconciler.Reconciler: one-sided fill detection and unwind."""

from __future__ import annotations

from execution.reconciler import LegResult, Reconciler
from strategies.base import Leg, Opportunity


def _opportunity() -> Opportunity:
    return Opportunity(
        strategy="cross_exchange",
        symbol="BTC/USDT",
        legs=(Leg("binance", "BTC/USDT", "buy", 100.0, 5.0, 0.001), Leg("kraken", "BTC/USDT", "sell", 105.0, 5.0, 0.001)),
        gross_profit_pct=5.0,
        net_profit_pct=4.8,
        max_size_usd=500.0,
    )


def test_one_sided_fill_is_detected_and_unwind_is_proposed():
    opp = _opportunity()
    reconciler = Reconciler()

    plans = reconciler.check(opp, [LegResult(opp.legs[0], success=True), LegResult(opp.legs[1], success=False, error="timeout")])

    assert len(plans) == 1
    assert plans[0].venue_id == "binance"
    assert plans[0].side == "sell"  # opposite of the filled buy leg
    assert plans[0].size == 5.0
    assert reconciler.has_open_incidents is True


def test_fully_successful_execution_needs_no_unwind():
    opp = _opportunity()
    reconciler = Reconciler()

    plans = reconciler.check(opp, [LegResult(opp.legs[0], success=True), LegResult(opp.legs[1], success=True)])

    assert plans == []
    assert reconciler.has_open_incidents is False


def test_fully_failed_execution_needs_no_unwind():
    opp = _opportunity()
    reconciler = Reconciler()

    plans = reconciler.check(
        opp, [LegResult(opp.legs[0], success=False, error="rejected"), LegResult(opp.legs[1], success=False, error="rejected")]
    )

    assert plans == []
    assert reconciler.has_open_incidents is False


def test_resolve_incident_clears_it():
    opp = _opportunity()
    reconciler = Reconciler()
    reconciler.check(opp, [LegResult(opp.legs[0], success=True), LegResult(opp.legs[1], success=False, error="x")])
    assert reconciler.has_open_incidents is True

    reconciler.resolve_incident(opp)

    assert reconciler.has_open_incidents is False
