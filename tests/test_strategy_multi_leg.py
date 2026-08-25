"""Tests for strategies.multi_leg.MultiLegStrategy."""

from __future__ import annotations

from strategies.multi_leg import MultiLegStrategy, build_cross_venue_graph
from tests.conftest import make_market_state


def test_composite_route_using_a_transfer_edge_is_detected():
    market_state = make_market_state(
        {
            ("binance", "BTC/USDT"): {"bid": 100.0, "ask": 100.1},
            ("kraken", "BTC/USDT"): {"bid": 106.0, "ask": 106.1},
        }
    )
    strategy = MultiLegStrategy(min_profit_pct=0.1, max_path_length=4)

    opportunities = strategy.scan(market_state)

    assert len(opportunities) >= 1
    assert any(leg.side == "transfer" for leg in opportunities[0].legs)
    assert opportunities[0].requires_prefunded_inventory is True


def test_graph_includes_transfer_edges_between_venues():
    market_state = make_market_state(
        {
            ("binance", "BTC/USDT"): {"bid": 100.0, "ask": 100.1},
            ("kraken", "BTC/USDT"): {"bid": 106.0, "ask": 106.1},
        }
    )
    graph, _prices = build_cross_venue_graph(market_state, reference_trade_usd=500.0)

    transfer_edges = [e for node in graph.nodes() for e in graph.edges_from(node) if e.side == "transfer"]
    assert len(transfer_edges) > 0


def test_single_venue_with_no_transfer_partner_finds_nothing():
    market_state = make_market_state({("binance", "BTC/USDT"): {"bid": 100.0, "ask": 100.1}})
    strategy = MultiLegStrategy(min_profit_pct=0.01, max_path_length=4)

    assert strategy.scan(market_state) == []
