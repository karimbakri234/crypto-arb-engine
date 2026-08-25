"""Tests for strategies.triangular.TriangularStrategy."""

from __future__ import annotations

from strategies.triangular import TriangularStrategy, build_venue_graph
from tests.conftest import make_market_state


def test_known_negative_cycle_is_found():
    # USDT -> BTC -> ETH -> USDT, engineered to be profitable after fees.
    market_state = make_market_state(
        {
            ("binance", "BTC/USDT"): {"bid": 49_950.0, "ask": 50_000.0},
            ("binance", "ETH/BTC"): {"bid": 0.0210, "ask": 0.02105},
            ("binance", "ETH/USDT"): {"bid": 1_100.0, "ask": 1_101.0},
        },
        symbols=["BTC/USDT", "ETH/BTC", "ETH/USDT"],
    )
    strategy = TriangularStrategy(min_profit_pct=0.01, max_cycle_length=4)

    opportunities = strategy.scan(market_state)

    assert len(opportunities) >= 1
    assert all(o.net_profit_pct >= 0.01 for o in opportunities)
    assert all(len(o.legs) == 3 for o in opportunities)


def test_non_cycle_graph_returns_nothing():
    # Only one market listed -- no way to complete a cycle.
    market_state = make_market_state({("binance", "BTC/USDT"): {"bid": 100.0, "ask": 100.1}})
    strategy = TriangularStrategy(min_profit_pct=0.01)

    assert strategy.scan(market_state) == []


def test_build_venue_graph_skips_stale_books():
    market_state = make_market_state(
        {("binance", "BTC/USDT"): {"bid": 100.0, "ask": 100.1, "age_sec": 60.0}},
        staleness_sec=3.0,
    )
    graph = build_venue_graph(market_state, "binance")
    assert graph.nodes() == []
