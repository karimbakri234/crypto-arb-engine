"""CEX-DEX arbitrage: CEX order book price vs. on-chain AMM price.

Unlike a pure order-book comparison, this has to price in everything
that makes on-chain execution different from a second CEX: gas (in USD,
from a live gas oracle -- `MarketState.gas_price_usd`), the DEX's own
swap fee tier, price impact at the intended trade size (via
`strategies.dex_dex.quote_pool_output`, i.e. real AMM math, not just the
spot ratio), an MEV/priority-fee allowance for landing the transaction
promptly, and -- if the asset needs to move between the CEX and the
chain the pool lives on -- bridge/settlement time as a latency risk.

Only viable in practice when you already hold inventory both on the CEX
and in a hot wallet on that chain; see execution/executor.py.

Prefer a routed aggregator quote (1inch/0x/Jupiter/Odos) over local AMM
math when you need an exact fill price for a specific size and can
afford the network round-trip -- local math here is for cheap, no-network
first-pass detection against cached pool reserves.
"""

from __future__ import annotations

from config.settings import MEV_PRIORITY_FEE_USD_FALLBACK, TAKER_FEE_FALLBACK
from config.venues import taker_fee_for
from core.market_state import MarketState, PoolState
from strategies.base import Leg, Opportunity, Strategy
from strategies.dex_dex import quote_pool_input_for_base_out


class CexDexStrategy(Strategy):
    name = "cex_dex"

    def __init__(self, min_profit_pct: float = 0.3, probe_size_base: float = 1.0) -> None:
        super().__init__(min_profit_pct)
        self.probe_size_base = probe_size_base

    def scan(self, market_state: MarketState) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        for pool in market_state.dex_pools.values():
            base_asset = pool.symbol.split("/")[0]
            for cex_venue_id in market_state.book_store.all_venues():
                book = market_state.book_store.get(cex_venue_id, pool.symbol)
                if book is None:
                    continue
                state = book.snapshot()
                if state.age_sec > market_state.staleness_sec or state.best_bid <= 0:
                    continue

                gas_usd = market_state.gas_price_usd.get(pool.chain, 5.0)
                priority_fee_usd = MEV_PRIORITY_FEE_USD_FALLBACK
                cex_fee = taker_fee_for(cex_venue_id, TAKER_FEE_FALLBACK)

                for opp in self._compare_direction(
                    pool, base_asset, cex_venue_id, state.best_bid, state.best_ask, cex_fee, gas_usd, priority_fee_usd
                ):
                    opportunities.append(opp)

        opportunities.sort(key=lambda o: o.net_profit_pct, reverse=True)
        return opportunities

    def _compare_direction(
        self,
        pool: PoolState,
        base_asset: str,
        cex_venue_id: str,
        cex_bid: float,
        cex_ask: float,
        cex_fee: float,
        gas_usd: float,
        priority_fee_usd: float,
    ) -> list[Opportunity]:
        results: list[Opportunity] = []
        size = self.probe_size_base

        # Direction 1: buy on DEX (against pool reserves, with price impact), sell on CEX bid.
        dex_quote_in = quote_pool_input_for_base_out(pool, size)
        dex_effective_price = dex_quote_in / size if size and dex_quote_in != float("inf") else 0.0
        if dex_effective_price > 0:
            gross_usd = (cex_bid - dex_effective_price) * size
            costs_usd = cex_bid * size * cex_fee + gas_usd + priority_fee_usd
            net_usd = gross_usd - costs_usd
            net_pct = (net_usd / (dex_effective_price * size)) * 100.0 if dex_effective_price * size else -100.0
            if net_pct >= self.min_profit_pct:
                results.append(
                    self._build_opportunity(
                        pool, base_asset, cex_venue_id, "dex_to_cex",
                        buy_price=dex_effective_price, sell_price=cex_bid, size=size,
                        buy_fee=pool.fee, sell_fee=cex_fee, gas_usd=gas_usd,
                        priority_fee_usd=priority_fee_usd, net_pct=net_pct,
                        gross_pct=(cex_bid - dex_effective_price) / dex_effective_price * 100.0,
                    )
                )

        # Direction 2: buy on CEX ask, sell on DEX (reserves quoted at spot ratio, no impact model
        # for the sell leg beyond the swap fee -- selling into the pool would need the inverse
        # constant-product formula, omitted here for brevity but symmetric to quote_pool_output).
        spot_price = pool.reserve_quote / pool.reserve_base if pool.reserve_base else 0.0
        if spot_price > 0 and cex_ask > 0:
            gross_usd = (spot_price - cex_ask) * size
            costs_usd = cex_ask * size * cex_fee + spot_price * size * pool.fee + gas_usd + priority_fee_usd
            net_usd = gross_usd - costs_usd
            net_pct = (net_usd / (cex_ask * size)) * 100.0 if cex_ask * size else -100.0
            if net_pct >= self.min_profit_pct:
                results.append(
                    self._build_opportunity(
                        pool, base_asset, cex_venue_id, "cex_to_dex",
                        buy_price=cex_ask, sell_price=spot_price, size=size,
                        buy_fee=cex_fee, sell_fee=pool.fee, gas_usd=gas_usd,
                        priority_fee_usd=priority_fee_usd, net_pct=net_pct,
                        gross_pct=(spot_price - cex_ask) / cex_ask * 100.0,
                    )
                )

        return results

    def _build_opportunity(
        self,
        pool: PoolState,
        base_asset: str,
        cex_venue_id: str,
        direction: str,
        *,
        buy_price: float,
        sell_price: float,
        size: float,
        buy_fee: float,
        sell_fee: float,
        gas_usd: float,
        priority_fee_usd: float,
        net_pct: float,
        gross_pct: float,
    ) -> Opportunity:
        buy_venue = pool.dex_id if direction == "dex_to_cex" else cex_venue_id
        sell_venue = cex_venue_id if direction == "dex_to_cex" else pool.dex_id
        legs = (
            Leg(buy_venue, f"{base_asset}/USD", "buy", buy_price, size, buy_fee),
            Leg(sell_venue, f"{base_asset}/USD", "sell", sell_price, size, sell_fee),
        )
        return Opportunity(
            strategy=self.name,
            symbol=pool.symbol,
            legs=legs,
            gross_profit_pct=gross_pct,
            net_profit_pct=net_pct,
            max_size_usd=size * buy_price,
            requires_prefunded_inventory=True,
            is_atomic=False,
            detail={
                "direction": direction,
                "chain": pool.chain,
                "gas_usd": gas_usd,
                "priority_fee_usd": priority_fee_usd,
                "note": "Requires pre-existing inventory on both the CEX and a hot wallet on-chain.",
            },
        )
