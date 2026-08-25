"""Currency graph for cycle-based arbitrage path-finding.

Each asset is a node. Each edge represents "convert 1 unit of `src` into
`rate * (1 - fee)` units of `dst`" via one market on one venue. Edge
weight is `-log(rate * (1 - fee))`: a profitable conversion has
`rate * (1 - fee) > 1`, i.e. weight < 0, so a profitable *cycle* (buy
USDT -> BTC -> ETH -> USDT and end up with more USDT than you started
with) is exactly a negative-weight cycle in this graph.

Two complementary search strategies are provided:

* `bellman_ford_negative_cycle` finds *any* negative cycle reachable from
  a source in one pass (classic Bellman-Ford relaxation +
  predecessor-chasing), regardless of length. This is the right tool when
  you just want to know "is there an arbitrage cycle at all".
* `find_cycles` enumerates simple cycles through a start asset up to a
  configurable maximum length via bounded DFS. This is the right tool
  when you specifically want, e.g., "every profitable 3-, 4-, and 5-leg
  cycle" rather than just the first one Bellman-Ford happens to unwind.

With a 50+ coin universe there are thousands of possible triangles;
either formulation finds profitable ones in one graph pass instead of
brute-forcing every combination of markets by hand.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Edge:
    """A directed conversion edge: 1 unit of `src` -> `rate * (1 - fee)` units of `dst`."""

    src: str
    dst: str
    rate: float
    fee: float
    venue_id: str
    symbol: str
    side: str  # "buy" | "sell" -- which side of `symbol`'s book this edge trades

    @property
    def weight(self) -> float:
        effective_rate = self.rate * (1.0 - self.fee)
        if effective_rate <= 0:
            return math.inf
        return -math.log(effective_rate)


@dataclass(frozen=True, slots=True)
class Cycle:
    """A closed path of edges that starts and ends at the same asset."""

    assets: tuple[str, ...]
    edges: tuple[Edge, ...]
    total_weight: float

    @property
    def profit_pct(self) -> float:
        """Net percentage gain from starting with 1 unit and following the cycle."""
        return (math.exp(-self.total_weight) - 1.0) * 100.0

    @property
    def length(self) -> int:
        return len(self.edges)


class CurrencyGraph:
    """A directed multigraph of currency-conversion edges."""

    def __init__(self) -> None:
        self._adjacency: dict[str, list[Edge]] = {}

    def add_edge(self, edge: Edge) -> None:
        self._adjacency.setdefault(edge.src, []).append(edge)
        self._adjacency.setdefault(edge.dst, [])  # ensure the node exists

    def nodes(self) -> list[str]:
        return list(self._adjacency.keys())

    def edges_from(self, asset: str) -> list[Edge]:
        return self._adjacency.get(asset, [])

    def find_cycles(self, start_asset: str, max_length: int) -> list[Cycle]:
        """Enumerate profitable simple cycles through `start_asset`.

        Bounded DFS up to `max_length` hops. A cycle is "profitable" if
        its total weight is negative (equivalently, `profit_pct > 0`).
        Only simple cycles (no repeated intermediate asset) are
        considered, which keeps the search space bounded even on a dense
        graph.
        """
        if start_asset not in self._adjacency:
            return []

        results: list[Cycle] = []
        path_assets: list[str] = [start_asset]
        path_edges: list[Edge] = []
        visited: set[str] = {start_asset}

        def dfs(current: str, total_weight: float) -> None:
            if len(path_edges) >= max_length:
                return
            for edge in self._adjacency.get(current, []):
                if edge.dst == start_asset and len(path_edges) >= 2:
                    new_weight = total_weight + edge.weight
                    if new_weight < 0:
                        results.append(
                            Cycle(
                                assets=tuple(path_assets) + (start_asset,),
                                edges=tuple(path_edges) + (edge,),
                                total_weight=new_weight,
                            )
                        )
                    continue
                if edge.dst in visited:
                    continue
                visited.add(edge.dst)
                path_assets.append(edge.dst)
                path_edges.append(edge)
                dfs(edge.dst, total_weight + edge.weight)
                path_edges.pop()
                path_assets.pop()
                visited.discard(edge.dst)

        dfs(start_asset, 0.0)
        results.sort(key=lambda c: c.total_weight)
        return results

    def bellman_ford_negative_cycle(self, source: str) -> Cycle | None:
        """Find one negative-weight cycle reachable from `source`, if any exists."""
        nodes = self.nodes()
        if source not in nodes:
            return None

        dist: dict[str, float] = dict.fromkeys(nodes, math.inf)
        pred: dict[str, Edge | None] = dict.fromkeys(nodes, None)
        dist[source] = 0.0

        cycle_start: str | None = None
        for i in range(len(nodes)):
            updated = False
            for src, edges in self._adjacency.items():
                if dist[src] == math.inf:
                    continue
                for edge in edges:
                    candidate = dist[src] + edge.weight
                    if candidate < dist[edge.dst] - 1e-12:
                        dist[edge.dst] = candidate
                        pred[edge.dst] = edge
                        updated = True
                        if i == len(nodes) - 1:
                            cycle_start = edge.dst
            if not updated:
                break

        if cycle_start is None:
            return None

        # Walk predecessors `len(nodes)` steps to guarantee landing inside the cycle.
        node = cycle_start
        for _ in range(len(nodes)):
            edge = pred[node]
            if edge is None:
                return None
            node = edge.src

        cycle_edges: list[Edge] = []
        cycle_assets: list[str] = [node]
        current = node
        total_weight = 0.0
        while True:
            edge = pred[current]
            if edge is None:
                return None
            cycle_edges.append(edge)
            total_weight += edge.weight
            current = edge.src
            cycle_assets.append(current)
            if current == node:
                break

        cycle_edges.reverse()
        cycle_assets.reverse()
        return Cycle(assets=tuple(cycle_assets), edges=tuple(cycle_edges), total_weight=total_weight)
