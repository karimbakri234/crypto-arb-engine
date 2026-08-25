"""Tests for strategies.latency_arb.LatencyArbStrategy."""

from __future__ import annotations

import time

from strategies.latency_arb import LatencyArbStrategy
from tests.conftest import make_market_state


def test_stale_laggard_with_sufficient_lag_is_detected():
    market_state = make_market_state(
        {
            ("binance", "BTC/USDT"): {"bid": 100.0, "ask": 100.1},
            ("kraken", "BTC/USDT"): {"bid": 102.0, "ask": 102.1},
        }
    )
    # Force binance's book to look older than kraken's by a full second.
    binance_state = market_state.book_store.get("binance", "BTC/USDT").snapshot()
    object.__setattr__(binance_state, "timestamp", time.time() - 1.0)

    strategy = LatencyArbStrategy(min_profit_pct=0.1, min_lag_sec=0.5)
    opportunities = strategy.scan(market_state)

    assert len(opportunities) == 1
    assert opportunities[0].legs[0].venue_id == "binance"  # the laggard is the buy side


def test_insufficient_lag_is_not_flagged():
    market_state = make_market_state(
        {
            ("binance", "BTC/USDT"): {"bid": 100.0, "ask": 100.1},
            ("kraken", "BTC/USDT"): {"bid": 102.0, "ask": 102.1},
        }
    )
    strategy = LatencyArbStrategy(min_profit_pct=0.1, min_lag_sec=5.0)

    assert strategy.scan(market_state) == []
