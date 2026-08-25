"""Tests for strategies.perp_perp.PerpPerpStrategy."""

from __future__ import annotations

import time

from core.types import FundingRate
from strategies.perp_perp import PerpPerpStrategy
from tests.conftest import make_market_state


def test_clean_perp_spread_is_detected():
    market_state = make_market_state(
        {
            ("binance", "BTC/USDT-PERP"): {"bid": 50_000.0, "ask": 50_010.0},
            ("kraken", "BTC/USDT-PERP"): {"bid": 50_500.0, "ask": 50_510.0},
        },
        symbols=[],
    )
    strategy = PerpPerpStrategy(min_profit_pct=0.1)

    opportunities = strategy.scan(market_state)

    assert len(opportunities) == 1
    long_leg, short_leg = opportunities[0].legs
    assert long_leg.side == "buy" and long_leg.venue_id == "binance"
    assert short_leg.side == "sell" and short_leg.venue_id == "kraken"


def test_funding_differential_is_surfaced_as_bonus_context():
    market_state = make_market_state(
        {
            ("binance", "BTC/USDT-PERP"): {"bid": 50_000.0, "ask": 50_010.0},
            ("kraken", "BTC/USDT-PERP"): {"bid": 50_500.0, "ask": 50_510.0},
        },
        symbols=[],
    )
    market_state.funding_rates[("binance", "BTC/USDT")] = FundingRate("binance", "BTC/USDT", 0.0001, 8.0, time.time() + 3600)
    market_state.funding_rates[("kraken", "BTC/USDT")] = FundingRate("kraken", "BTC/USDT", 0.0005, 8.0, time.time() + 3600)

    strategy = PerpPerpStrategy(min_profit_pct=0.1)
    opportunities = strategy.scan(market_state)

    assert opportunities[0].detail["funding_differential_annualized_pct"] > 0


def test_no_spread_yields_no_opportunity():
    market_state = make_market_state(
        {
            ("binance", "BTC/USDT-PERP"): {"bid": 50_000.0, "ask": 50_010.0},
            ("kraken", "BTC/USDT-PERP"): {"bid": 50_005.0, "ask": 50_015.0},
        },
        symbols=[],
    )
    strategy = PerpPerpStrategy(min_profit_pct=0.5)

    assert strategy.scan(market_state) == []
