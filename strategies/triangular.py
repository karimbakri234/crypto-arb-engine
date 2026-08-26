"""Triangular arbitrage: profitable conversion cycles within one venue.

Each venue is modeled as a directed `CurrencyGraph`: every listed market
`BASE/QUOTE` contributes two edges (buy `BASE` with `QUOTE` at the ask,
sell `BASE` for `QUOTE` at the bid), each weighted `-log(rate * (1 -
fee))`. A profitable cycle -- e.g. USDT -> BTC -> ETH -> USDT -- is
exactly a negative-weight cycle in this graph. With a 50+ coin universe
there are thousands of possible triangles per venue; the graph
formulation finds all of them (up to `max_cycle_length` legs) in one
pass instead of brute-forcing every combination.
"""

from __future__ import annotations

from config.settings import MAX_CYCLE_LENGTH, TAKER_FEE_FALLBACK
from config.venues import taker_fee_for
from core.graph import CurrencyGraph, Edge
from core.market_state import MarketState
from strategies.base import Leg, Opportunity, Strategy

# Hard ceiling on edges traversed per find_cycles call (see core/graph.py).
# Bounded DFS is still exponential in branching factor: a venue that lists
# many pairs over a few hub assets produces a dense graph, and without a
# cap one dense venue turns an otherwise ~1ms tick into a multi-hundred-
# millisecond one. Capping trades exhaustive enumeration -- which nothing
# downstream needs, since only the best few cycles are ever executed --
# for a bounded, predictable per-tick cost.
MAX_CYCLE_SEARCH_EXPANSIONS = 2000

# A cycle is discovered once per node it can be entered from, and the
# rotations are thrown away by the de-duplication pass at the end of
# `_scan_venue` -- so searching from every asset does length-many times
# the work to produce the same set. Every tradeable pair is quoted in one
# of these hubs, so a real triangular cycle passes through at least one of
# them and is still found. Restricting start nodes this way was ~97% of
# the detection tick's cost at production universe size.
HUB_ASSETS: frozenset[str] = frozenset({"USDT", "USDC", "BTC", "ETH"})


def build_venue_graph(market_state: MarketState, venue_id: str) -> CurrencyGraph:
    """Build the currency graph for one venue from its current order books."""
    graph = CurrencyGraph()
    fee = taker_fee_for(venue_id, TAKER_FEE_FALLBACK)

    for symbol in market_state.symbols:
        book = market_state.book_store.get(venue_id, symbol)
        if book is None or "/" not in symbol:
            continue
        state = book.snapshot()
        if state.age_sec > market_state.staleness_sec:
            continue
        if state.best_bid <= 0 or state.best_ask <= 0 or state.best_ask == float("inf"):
            continue

        base, quote = symbol.split("/")
        # Buy BASE with QUOTE at the ask: 1 QUOTE -> 1/ask BASE.
        graph.add_edge(Edge(quote, base, rate=1.0 / state.best_ask, fee=fee, venue_id=venue_id, symbol=symbol, side="buy"))
        # Sell BASE for QUOTE at the bid: 1 BASE -> bid QUOTE.
        graph.add_edge(Edge(base, quote, rate=state.best_bid, fee=fee, venue_id=venue_id, symbol=symbol, side="sell"))

    return graph


class TriangularStrategy(Strategy):
    name = "triangular"

    def __init__(
        self,
        min_profit_pct: float = 0.1,
        max_cycle_length: int = MAX_CYCLE_LENGTH,
        max_trade_usd: float = 500.0,
    ) -> None:
        super().__init__(min_profit_pct)
        self.max_cycle_length = max_cycle_length
        self.max_trade_usd = max_trade_usd

    def scan(self, market_state: MarketState) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        for venue_id in market_state.book_store.all_venues():
            opportunities.extend(self._scan_venue(market_state, venue_id))
        opportunities.sort(key=lambda o: o.net_profit_pct, reverse=True)
        return opportunities

    def _scan_venue(self, market_state: MarketState, venue_id: str) -> list[Opportunity]:
        graph = build_venue_graph(market_state, venue_id)
        opportunities: list[Opportunity] = []

        hub_starts = [asset for asset in graph.nodes() if asset in HUB_ASSETS]
        for start_asset in hub_starts:
            for cycle in graph.find_cycles(
                start_asset, self.max_cycle_length, max_expansions=MAX_CYCLE_SEARCH_EXPANSIONS
            ):
                if cycle.profit_pct < self.min_profit_pct:
                    continue

                legs = tuple(
                    Leg(venue_id=edge.venue_id, symbol=edge.symbol, side=edge.side, price=1.0 / edge.rate if edge.side == "buy" else edge.rate, size=0.0, fee=edge.fee)
                    for edge in cycle.edges
                )
                opportunities.append(
                    Opportunity(
                        strategy=self.name,
                        symbol=f"{venue_id}:{'-'.join(cycle.assets)}",
                        legs=legs,
                        gross_profit_pct=cycle.profit_pct,  # fees are already inside the edge weights
                        net_profit_pct=cycle.profit_pct,
                        max_size_usd=self.max_trade_usd,
                        requires_prefunded_inventory=False,  # single venue, sequential fills
                        is_atomic=False,
                        detail={"venue": venue_id, "cycle": cycle.assets, "length": cycle.length},
                    )
                )

        # A given cycle is found once per starting node it's rotated through;
        # de-duplicate by the frozenset of edges used.
        seen: set[frozenset] = set()
        deduped: list[Opportunity] = []
        for opp in opportunities:
            key = frozenset((leg.venue_id, leg.symbol, leg.side) for leg in opp.legs)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(opp)
        return deduped
