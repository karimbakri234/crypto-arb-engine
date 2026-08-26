"""Tests for core.graph: cycle detection via DFS enumeration and Bellman-Ford."""

from __future__ import annotations

from core.graph import CurrencyGraph, Edge


def _profitable_triangle() -> CurrencyGraph:
    graph = CurrencyGraph()
    graph.add_edge(Edge("USDT", "BTC", rate=1 / 50_000, fee=0.001, venue_id="x", symbol="BTC/USDT", side="buy"))
    graph.add_edge(Edge("BTC", "ETH", rate=17.0, fee=0.001, venue_id="x", symbol="ETH/BTC", side="sell"))
    graph.add_edge(Edge("ETH", "USDT", rate=3_100.0, fee=0.001, venue_id="x", symbol="ETH/USDT", side="sell"))
    return graph


def test_find_cycles_detects_a_known_negative_cycle():
    graph = _profitable_triangle()

    cycles = graph.find_cycles("USDT", max_length=5)

    assert len(cycles) == 1
    assert cycles[0].assets == ("USDT", "BTC", "ETH", "USDT")
    assert cycles[0].profit_pct > 0


def test_bellman_ford_detects_the_same_negative_cycle():
    graph = _profitable_triangle()

    cycle = graph.bellman_ford_negative_cycle("USDT")

    assert cycle is not None
    assert set(cycle.assets) == {"USDT", "BTC", "ETH"}
    assert cycle.profit_pct > 0


def test_non_cycle_graph_returns_nothing():
    graph = CurrencyGraph()
    graph.add_edge(Edge("USDT", "BTC", rate=1 / 50_000, fee=0.001, venue_id="x", symbol="BTC/USDT", side="buy"))
    # No edge back from BTC to USDT (or anywhere) -- there is no cycle at all.

    assert graph.find_cycles("USDT", max_length=5) == []
    assert graph.bellman_ford_negative_cycle("USDT") is None


def test_unprofitable_triangle_yields_no_cycles():
    graph = CurrencyGraph()
    # Fees eat the entire round-trip; no negative-weight cycle exists.
    graph.add_edge(Edge("USDT", "BTC", rate=1 / 50_000, fee=0.01, venue_id="x", symbol="BTC/USDT", side="buy"))
    graph.add_edge(Edge("BTC", "ETH", rate=16.0, fee=0.01, venue_id="x", symbol="ETH/BTC", side="sell"))
    graph.add_edge(Edge("ETH", "USDT", rate=3_100.0, fee=0.01, venue_id="x", symbol="ETH/USDT", side="sell"))

    assert graph.find_cycles("USDT", max_length=5) == []


def test_max_length_excludes_longer_cycles():
    graph = CurrencyGraph()
    # A 4-leg profitable cycle: USDT -> A -> B -> C -> USDT.
    graph.add_edge(Edge("USDT", "A", rate=2.0, fee=0.0, venue_id="x", symbol="A/USDT", side="buy"))
    graph.add_edge(Edge("A", "B", rate=2.0, fee=0.0, venue_id="x", symbol="B/A", side="buy"))
    graph.add_edge(Edge("B", "C", rate=2.0, fee=0.0, venue_id="x", symbol="C/B", side="buy"))
    graph.add_edge(Edge("C", "USDT", rate=2.0, fee=0.0, venue_id="x", symbol="C/USDT", side="sell"))

    assert graph.find_cycles("USDT", max_length=3) == []
    cycles = graph.find_cycles("USDT", max_length=4)
    assert len(cycles) == 1
    assert cycles[0].length == 4


def test_max_expansions_bounds_a_dense_graph_search():
    """A real deployment hit this: adding more venues made the graph dense
    enough that an uncapped search took state_to_detect from ~60ms to ~28s.
    `max_expansions` must stop the search well before it exhaustively
    enumerates every one of many independent profitable cycles."""
    graph = CurrencyGraph()
    for i in range(30):
        b, c = f"B{i}", f"C{i}"
        graph.add_edge(Edge("USDT", b, rate=1.05, fee=0.0, venue_id="x", symbol=f"{b}/USDT", side="buy"))
        graph.add_edge(Edge(b, c, rate=1.05, fee=0.0, venue_id="x", symbol=f"{c}/{b}", side="buy"))
        graph.add_edge(Edge(c, "USDT", rate=1.05, fee=0.0, venue_id="x", symbol=f"{c}/USDT", side="sell"))

    unbounded = graph.find_cycles("USDT", max_length=4)
    bounded = graph.find_cycles("USDT", max_length=4, max_expansions=10)

    assert len(unbounded) == 30
    assert 0 < len(bounded) < len(unbounded)
