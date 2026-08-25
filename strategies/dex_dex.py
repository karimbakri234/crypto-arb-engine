"""DEX-DEX arbitrage: the same asset priced differently across two AMMs.

Two flavors:

* **Same-chain**: both legs can be bundled into one atomic transaction
  (a single block, no risk of only one leg filling). This module can
  optionally price these as *flash-loan-funded*, meaning no upfront
  capital is required -- the loan, both swaps, and the repayment all
  happen in one transaction. **This requires a deployed smart contract
  that borrows the flash loan, performs the swap sequence, and repays it
  atomically; writing/deploying that contract is out of scope for this
  Python bot.** This module's job stops at detection and constructing the
  transaction calldata for the swap legs -- it does not deploy or own an
  on-chain contract.
* **Cross-chain**: non-atomic. A bridge transfer sits between the two
  legs, so this module prices in bridge fee and bridge latency as
  explicit risk, the same way cross-exchange CEX arbitrage prices in
  pre-funded inventory instead of a transfer.

Local AMM math (constant-product and a simplified concentrated-liquidity
model) is implemented directly here so opportunities can be *priced*
without a network round-trip whenever pool reserves are already cached in
`MarketState.dex_pools` -- only a fresh reserve read needs the network.

Scope note: this module only ever backruns/corrects an existing price
discrepancy between two pools. It never inspects or reacts to a specific
pending transaction in the mempool -- no sandwiching, no frontrunning.
"""

from __future__ import annotations

from itertools import combinations

from config.venues import DEX_VENUES
from core.market_state import MarketState, PoolState
from strategies.base import Leg, Opportunity, Strategy

# Model a same-chain bundle as ~instant (one block); a bridge transfer
# as minutes, with an associated fee -- both configurable per bridge in
# a real deployment. These are conservative placeholders.
BRIDGE_LATENCY_SEC_DEFAULT = 600.0
BRIDGE_FEE_PCT_DEFAULT = 0.05


def constant_product_output(reserve_in: float, reserve_out: float, amount_in: float, fee: float) -> float:
    """Output amount for a constant-product (x*y=k) AMM swap, net of `fee`."""
    if reserve_in <= 0 or reserve_out <= 0 or amount_in <= 0:
        return 0.0
    amount_in_after_fee = amount_in * (1.0 - fee)
    return reserve_out * amount_in_after_fee / (reserve_in + amount_in_after_fee)


def constant_product_input_needed(reserve_in: float, reserve_out: float, amount_out: float, fee: float) -> float:
    """Input amount required to receive exactly `amount_out` from a constant-product AMM.

    The inverse of `constant_product_output`. Needed whenever the trade is
    framed as "how much do I pay to *acquire* this much of the output
    asset" (buying) rather than "how much do I receive for selling this
    much of the input asset" -- the two are not the same price at any
    size above the infinitesimal, since slippage moves against the trader
    in opposite directions in each framing.
    """
    if reserve_in <= 0 or reserve_out <= 0 or amount_out <= 0 or amount_out >= reserve_out:
        return float("inf")
    amount_in_after_fee = reserve_in * amount_out / (reserve_out - amount_out)
    return amount_in_after_fee / (1.0 - fee)


def concentrated_liquidity_output(
    sqrt_price_x96: float,
    liquidity: float,
    amount_in: float,
    fee: float,
    zero_for_one: bool,
) -> float:
    """Approximate output for a Uniswap-v3-style CLMM swap within the *current* tick.

    This is a simplified single-tick model: it assumes the swap is small
    enough not to cross a tick boundary, so liquidity `L` is constant
    across the swap. Real CLMM execution walks across ticks as price
    moves, re-reading liquidity at each boundary -- that tick-crossing
    logic is out of scope here; for swaps that are large relative to
    in-range liquidity this will overestimate output, so treat it as an
    optimistic bound, not an exact quote (prefer an aggregator quote --
    see module docstring in strategies/cex_dex.py -- for real routed sizes).
    """
    if liquidity <= 0 or amount_in <= 0 or sqrt_price_x96 <= 0:
        return 0.0
    amount_in_after_fee = amount_in * (1.0 - fee)
    sqrt_price = sqrt_price_x96 / (2**96)

    if zero_for_one:
        # dy = L * (sqrtP - sqrtP') ; sqrtP' = L*sqrtP / (L + amount_in*sqrtP)
        new_sqrt_price = (liquidity * sqrt_price) / (liquidity + amount_in_after_fee * sqrt_price)
        return liquidity * (sqrt_price - new_sqrt_price)
    else:
        # dx = L * (1/sqrtP' - 1/sqrtP) ; sqrtP' = sqrtP + amount_in / L
        new_sqrt_price = sqrt_price + amount_in_after_fee / liquidity
        return liquidity * (1.0 / sqrt_price - 1.0 / new_sqrt_price)


def quote_pool_output(pool: PoolState, amount_in_base: float) -> float:
    """Quote *selling* `amount_in_base` units of the pool's base asset for quote."""
    if pool.tick_liquidity is not None and pool.sqrt_price_x96 is not None:
        return concentrated_liquidity_output(
            pool.sqrt_price_x96, pool.tick_liquidity, amount_in_base, pool.fee, zero_for_one=True
        )
    return constant_product_output(pool.reserve_base, pool.reserve_quote, amount_in_base, pool.fee)


def quote_pool_input_for_base_out(pool: PoolState, amount_out_base: float) -> float:
    """Quote the quote-asset cost to *buy* `amount_out_base` units of base from the pool.

    This is the correct direction for "how much would it cost me to
    acquire this much base from the DEX" -- using `quote_pool_output`
    (the sell-side formula) for that question gets the slippage direction
    backwards at any non-trivial size. Concentrated-liquidity pools are
    not yet supported in this direction; callers should treat `inf` as
    "cannot price this on this pool type" rather than a real cost.
    """
    if pool.tick_liquidity is not None and pool.sqrt_price_x96 is not None:
        return float("inf")
    return constant_product_input_needed(pool.reserve_quote, pool.reserve_base, amount_out_base, pool.fee)


class DexDexStrategy(Strategy):
    name = "dex_dex"

    def __init__(
        self,
        min_profit_pct: float = 0.2,
        probe_size_base: float = 1.0,
        allow_flash_loan_atomic: bool = True,
    ) -> None:
        super().__init__(min_profit_pct)
        self.probe_size_base = probe_size_base
        self.allow_flash_loan_atomic = allow_flash_loan_atomic

    def scan(self, market_state: MarketState) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        pools_by_symbol: dict[str, list[PoolState]] = {}
        for pool in market_state.dex_pools.values():
            pools_by_symbol.setdefault(pool.symbol, []).append(pool)

        for symbol, pools in pools_by_symbol.items():
            for pool_a, pool_b in combinations(pools, 2):
                opp = self._compare_pools(symbol, pool_a, pool_b)
                if opp is not None:
                    opportunities.append(opp)

        opportunities.sort(key=lambda o: o.net_profit_pct, reverse=True)
        return opportunities

    def _compare_pools(self, symbol: str, pool_a: PoolState, pool_b: PoolState) -> Opportunity | None:
        # Try buying base on pool_a and selling it on pool_b, and vice versa.
        for buy_pool, sell_pool in ((pool_a, pool_b), (pool_b, pool_a)):
            quote_out = quote_pool_output(buy_pool, self.probe_size_base)
            if quote_out <= 0:
                continue
            # Selling `quote_out` worth of quote back through sell_pool's
            # inverse price approximated via spot ratio (probe-size accurate
            # enough for detection; execution should re-quote before firing).
            sell_price = sell_pool.reserve_quote / sell_pool.reserve_base if sell_pool.reserve_base else 0.0
            buy_price = buy_pool.reserve_quote / buy_pool.reserve_base if buy_pool.reserve_base else 0.0
            if buy_price <= 0 or sell_price <= 0:
                continue

            gross_pct = (sell_price - buy_price) / buy_price * 100.0
            same_chain = buy_pool.chain == sell_pool.chain

            if same_chain:
                fee_cost_pct = (buy_pool.fee + sell_pool.fee) * 100.0
                net_pct = gross_pct - fee_cost_pct
                is_atomic = self.allow_flash_loan_atomic
                detail = {
                    "buy_dex": buy_pool.dex_id,
                    "sell_dex": sell_pool.dex_id,
                    "chain": buy_pool.chain,
                    "flash_loan_funded": is_atomic,
                    "note": (
                        "Same-chain: legs can be bundled atomically. Flash-loan funding "
                        "requires a deployed repay-in-one-tx contract (out of scope here); "
                        "this bot only detects and constructs the swap calldata."
                        if is_atomic
                        else "Same-chain, capital-funded (no flash loan)."
                    ),
                }
            else:
                bridge_fee_pct = BRIDGE_FEE_PCT_DEFAULT
                fee_cost_pct = (buy_pool.fee + sell_pool.fee + bridge_fee_pct / 100.0) * 100.0
                net_pct = gross_pct - fee_cost_pct
                is_atomic = False
                detail = {
                    "buy_dex": buy_pool.dex_id,
                    "sell_dex": sell_pool.dex_id,
                    "buy_chain": buy_pool.chain,
                    "sell_chain": sell_pool.chain,
                    "bridge_latency_sec": BRIDGE_LATENCY_SEC_DEFAULT,
                    "bridge_fee_pct": bridge_fee_pct,
                    "note": "Cross-chain: non-atomic, priced in bridge fee/latency as risk.",
                }

            if net_pct < self.min_profit_pct:
                continue

            venue_name_buy = DEX_VENUES.get(buy_pool.dex_id)
            venue_name_sell = DEX_VENUES.get(sell_pool.dex_id)
            legs = (
                Leg(buy_pool.dex_id, symbol, "buy", buy_price, self.probe_size_base, buy_pool.fee),
                Leg(sell_pool.dex_id, symbol, "sell", sell_price, self.probe_size_base, sell_pool.fee),
            )
            return Opportunity(
                strategy=self.name,
                symbol=symbol,
                legs=legs,
                gross_profit_pct=gross_pct,
                net_profit_pct=net_pct,
                max_size_usd=self.probe_size_base * buy_price,
                requires_prefunded_inventory=not is_atomic,
                is_atomic=is_atomic,
                detail={**detail, "venues": (venue_name_buy, venue_name_sell)},
            )
        return None
