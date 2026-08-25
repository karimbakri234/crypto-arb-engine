"""Multi-leg / composite routing: combine legs across strategy types.

A meta-strategy: sometimes a *composite* path beats any single strategy's
best opportunity -- e.g. triangular conversion on venue A to acquire an
asset cheaply, then a cross-exchange transfer-and-sell on venue B. This
is modeled as one currency graph spanning every venue, where nodes are
`"{VENUE}:{ASSET}"` (so the same asset on two different venues is two
distinct nodes) and there are two edge types:

* **Conversion edges** (same venue): identical to triangular.py's
  buy/sell edges, just namespaced per venue.
* **Transfer edges** (same asset, different venue): weighted by the
  venue's withdrawal fee (converted to a fraction of a reference trade
  size) and tagged with a transfer latency. A composite route that uses
  a transfer edge is, by construction, not atomic -- the position is
  exposed for that transfer's latency, which is why `requires_prefunded_
  inventory` is always True here and `detail["total_transfer_latency_sec"]`
  is surfaced so the caller can judge whether the route is even plausible
  to hold through.
"""

from __future__ import annotations

from config.settings import MAX_CYCLE_LENGTH, TAKER_FEE_FALLBACK
from config.venues import CEX_VENUES, taker_fee_for
from core.graph import CurrencyGraph, Edge
from core.market_state import MarketState
from strategies.base import Leg, Opportunity, Strategy

DEFAULT_TRANSFER_LATENCY_SEC = 900.0  # 15 minutes, representative CEX withdrawal + deposit confirmation
DEFAULT_WITHDRAWAL_FEE_FRACTION = 0.001


def _node(venue_id: str, asset: str) -> str:
    return f"{venue_id}:{asset}"


def build_cross_venue_graph(
    market_state: MarketState,
    reference_trade_usd: float,
    transfer_latency_sec: float = DEFAULT_TRANSFER_LATENCY_SEC,
) -> tuple[CurrencyGraph, dict[tuple[str, str], float]]:
    """Build the cross-venue graph plus a `(venue, asset) -> USD price` map."""
    graph = CurrencyGraph()
    prices: dict[tuple[str, str], float] = {}

    venues = list(market_state.book_store.all_venues())

    for venue_id in venues:
        fee = taker_fee_for(venue_id, TAKER_FEE_FALLBACK)
        for symbol in market_state.symbols:
            book = market_state.book_store.get(venue_id, symbol)
            if book is None or "/" not in symbol:
                continue
            state = book.snapshot()
            if state.age_sec > market_state.staleness_sec or state.best_bid <= 0 or state.best_ask in (0, float("inf")):
                continue
            base, quote = symbol.split("/")
            graph.add_edge(Edge(_node(venue_id, quote), _node(venue_id, base), rate=1.0 / state.best_ask, fee=fee, venue_id=venue_id, symbol=symbol, side="buy"))
            graph.add_edge(Edge(_node(venue_id, base), _node(venue_id, quote), rate=state.best_bid, fee=fee, venue_id=venue_id, symbol=symbol, side="sell"))
            prices[(venue_id, base)] = state.best_bid
            prices[(venue_id, quote)] = 1.0 if quote in ("USDT", "USDC") else prices.get((venue_id, quote), 1.0)

    # Transfer edges: same asset, any two venues that both quote it.
    assets_by_venue: dict[str, set[str]] = {}
    for (venue_id, asset) in prices:
        assets_by_venue.setdefault(venue_id, set()).add(asset)

    for venue_a in venues:
        for venue_b in venues:
            if venue_a == venue_b:
                continue
            common_assets = assets_by_venue.get(venue_a, set()) & assets_by_venue.get(venue_b, set())
            for asset in common_assets:
                price = prices.get((venue_a, asset), 1.0)
                withdrawal_fee_units = CEX_VENUES.get(venue_a, None)
                flat_fee = withdrawal_fee_units.withdrawal_fees.get(asset, 0.0) if withdrawal_fee_units else 0.0
                notional_units = reference_trade_usd / price if price > 0 else 0.0
                fee_fraction = (flat_fee / notional_units) if notional_units > 0 else DEFAULT_WITHDRAWAL_FEE_FRACTION
                fee_fraction = min(fee_fraction, 0.5)  # sanity cap

                graph.add_edge(
                    Edge(
                        _node(venue_a, asset), _node(venue_b, asset),
                        rate=1.0, fee=fee_fraction,
                        venue_id=f"{venue_a}->{venue_b}", symbol="TRANSFER", side="transfer",
                    )
                )

    return graph, prices


class MultiLegStrategy(Strategy):
    name = "multi_leg"

    def __init__(
        self,
        min_profit_pct: float = 0.2,
        max_path_length: int = MAX_CYCLE_LENGTH,
        max_trade_usd: float = 500.0,
        transfer_latency_sec: float = DEFAULT_TRANSFER_LATENCY_SEC,
    ) -> None:
        super().__init__(min_profit_pct)
        self.max_path_length = max_path_length
        self.max_trade_usd = max_trade_usd
        self.transfer_latency_sec = transfer_latency_sec

    def scan(self, market_state: MarketState) -> list[Opportunity]:
        graph, _prices = build_cross_venue_graph(market_state, self.max_trade_usd, self.transfer_latency_sec)

        opportunities: list[Opportunity] = []
        seen: set[frozenset] = set()
        for start_node in graph.nodes():
            for cycle in graph.find_cycles(start_node, self.max_path_length):
                # Only routes using at least one transfer edge are genuinely
                # "multi-leg composite" -- pure single-venue cycles are just
                # triangular arbitrage and are already covered there.
                if not any(edge.side == "transfer" for edge in cycle.edges):
                    continue
                if cycle.profit_pct < self.min_profit_pct:
                    continue

                key = frozenset((e.venue_id, e.symbol, e.side) for e in cycle.edges)
                if key in seen:
                    continue
                seen.add(key)

                total_latency = sum(
                    self.transfer_latency_sec for edge in cycle.edges if edge.side == "transfer"
                )
                legs = tuple(
                    Leg(
                        venue_id=edge.venue_id,
                        symbol=edge.symbol,
                        side=edge.side,
                        price=(1.0 / edge.rate if edge.side == "buy" else edge.rate) if edge.rate else 0.0,
                        size=0.0,
                        fee=edge.fee,
                    )
                    for edge in cycle.edges
                )

                opportunities.append(
                    Opportunity(
                        strategy=self.name,
                        symbol=f"multi:{'-'.join(cycle.assets)}",
                        legs=legs,
                        gross_profit_pct=cycle.profit_pct,
                        net_profit_pct=cycle.profit_pct,
                        max_size_usd=self.max_trade_usd,
                        requires_prefunded_inventory=True,
                        is_atomic=False,
                        detail={
                            "path": cycle.assets,
                            "length": cycle.length,
                            "total_transfer_latency_sec": total_latency,
                            "num_transfers": sum(1 for e in cycle.edges if e.side == "transfer"),
                        },
                    )
                )

        opportunities.sort(key=lambda o: o.net_profit_pct, reverse=True)
        return opportunities
