"""Tests for strategies.funding_rate.FundingRateStrategy."""

from __future__ import annotations

import time

from core.market_state import MarketState
from core.types import FundingRate
from strategies.funding_rate import FundingRateStrategy
from tests.conftest import make_book_store


def _market_state(rate: float) -> MarketState:
    store = make_book_store({("binance", "BTC/USDT"): {"bid": 50_000.0, "ask": 50_010.0}})
    market_state = MarketState(book_store=store, symbols=["BTC/USDT"])
    market_state.funding_rates[("binance", "BTC/USDT")] = FundingRate(
        "binance", "BTC/USDT", rate=rate, interval_hours=8.0, next_funding_ts=time.time() + 3600
    )
    return market_state


def test_strongly_positive_funding_shorts_perp_and_buys_spot():
    market_state = _market_state(rate=0.003)  # ~4x/day * 365 => very high annualized
    strategy = FundingRateStrategy(min_annualized_pct=5.0)

    opportunities = strategy.scan(market_state)

    assert len(opportunities) == 1
    perp_leg, spot_leg = opportunities[0].legs
    assert perp_leg.side == "sell"
    assert spot_leg.side == "buy"


def test_strongly_negative_funding_reverses_direction():
    market_state = _market_state(rate=-0.003)
    strategy = FundingRateStrategy(min_annualized_pct=5.0)

    opportunities = strategy.scan(market_state)

    assert len(opportunities) == 1
    perp_leg, spot_leg = opportunities[0].legs
    assert perp_leg.side == "buy"
    assert spot_leg.side == "sell"


def test_small_funding_rate_below_threshold_is_rejected():
    market_state = _market_state(rate=0.00001)
    strategy = FundingRateStrategy(min_annualized_pct=5.0)

    assert strategy.scan(market_state) == []
