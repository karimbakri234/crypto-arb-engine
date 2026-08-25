"""Stablecoin depeg arbitrage: USDT/USDC/DAI/FDUSD/TUSD/... vs $1.00.

Scans both CEX stable-stable order books (e.g. USDC/USDT) and DEX stable
pools (Curve especially, via pool reserve ratios) for deviation from
1.00. Small, transient deviations are ordinary arbitrage. A deviation
past `kill_switch_pct` is treated as a possible **solvency event, not a
trading opportunity** -- this module will not emit a tradeable
`Opportunity` for a pair past that threshold; instead it records the
symbol in `triggered_kill_switches` so callers (main.py, risk/manager.py)
can halt related trading and alert a human rather than "buy the dip" into
what might be a de-pegging stablecoin failing outright.
"""

from __future__ import annotations

from config.settings import (
    STABLE_DEPEG_KILL_SWITCH_PCT,
    STABLE_DEPEG_THRESHOLD_PCT,
    TAKER_FEE_FALLBACK,
)
from config.universe import STABLECOINS, build_stable_pairs
from config.venues import taker_fee_for
from core.market_state import MarketState
from strategies.base import Leg, Opportunity, Strategy


class StablecoinDepegStrategy(Strategy):
    name = "stablecoin_depeg"

    def __init__(
        self,
        depeg_threshold_pct: float = STABLE_DEPEG_THRESHOLD_PCT,
        kill_switch_pct: float = STABLE_DEPEG_KILL_SWITCH_PCT,
        max_trade_usd: float = 500.0,
    ) -> None:
        super().__init__(min_profit_pct=depeg_threshold_pct)
        self.kill_switch_pct = kill_switch_pct
        self.max_trade_usd = max_trade_usd
        self.triggered_kill_switches: set[str] = set()

    def scan(self, market_state: MarketState) -> list[Opportunity]:
        self.triggered_kill_switches.clear()
        opportunities: list[Opportunity] = []
        opportunities.extend(self._scan_cex_pairs(market_state))
        opportunities.extend(self._scan_dex_pools(market_state))
        opportunities.sort(key=lambda o: o.net_profit_pct, reverse=True)
        return opportunities

    def _scan_cex_pairs(self, market_state: MarketState) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        for venue_id in market_state.book_store.all_venues():
            listed = {s: {} for s in market_state.book_store.all_symbols() if market_state.book_store.get(venue_id, s) is not None}
            for pair in build_stable_pairs(listed):
                book = market_state.book_store.get(venue_id, pair.symbol)
                if book is None:
                    continue
                state = book.snapshot()
                if state.age_sec > market_state.staleness_sec or state.best_bid <= 0:
                    continue

                mid = (state.best_bid + state.best_ask) / 2.0
                deviation_pct = (mid - 1.0) * 100.0

                if abs(deviation_pct) >= self.kill_switch_pct:
                    self.triggered_kill_switches.add(pair.symbol)
                    continue
                if abs(deviation_pct) < self.min_profit_pct:
                    continue

                fee = taker_fee_for(venue_id, TAKER_FEE_FALLBACK)
                side = "sell" if deviation_pct > 0 else "buy"
                price = state.best_bid if side == "sell" else state.best_ask
                size_usd = min(self.max_trade_usd, state.best_bid_size * state.best_bid)
                if size_usd <= 0:
                    continue

                opportunities.append(
                    Opportunity(
                        strategy=self.name,
                        symbol=pair.symbol,
                        legs=(Leg(venue_id, pair.symbol, side, price, size_usd / price, fee),),
                        gross_profit_pct=abs(deviation_pct),
                        net_profit_pct=abs(deviation_pct) - fee * 100.0,
                        max_size_usd=size_usd,
                        requires_prefunded_inventory=True,
                        is_atomic=False,
                        detail={"venue": venue_id, "mid_price": mid, "deviation_pct": deviation_pct, "source": "cex"},
                    )
                )
        return opportunities

    def _scan_dex_pools(self, market_state: MarketState) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        for pool in market_state.dex_pools.values():
            base, _, quote = pool.symbol.partition("/")
            if base not in STABLECOINS or quote not in STABLECOINS:
                continue
            if pool.reserve_base <= 0:
                continue

            price = pool.reserve_quote / pool.reserve_base
            deviation_pct = (price - 1.0) * 100.0

            if abs(deviation_pct) >= self.kill_switch_pct:
                self.triggered_kill_switches.add(pool.symbol)
                continue
            if abs(deviation_pct) < self.min_profit_pct:
                continue

            size_usd = min(self.max_trade_usd, pool.reserve_base * 0.001)
            side = "sell" if deviation_pct > 0 else "buy"

            opportunities.append(
                Opportunity(
                    strategy=self.name,
                    symbol=pool.symbol,
                    legs=(Leg(pool.dex_id, pool.symbol, side, price, size_usd / max(price, 1e-9), pool.fee),),
                    gross_profit_pct=abs(deviation_pct),
                    net_profit_pct=abs(deviation_pct) - pool.fee * 100.0,
                    max_size_usd=size_usd,
                    requires_prefunded_inventory=True,
                    is_atomic=False,
                    detail={"dex": pool.dex_id, "chain": pool.chain, "price": price, "deviation_pct": deviation_pct, "source": "dex"},
                )
            )
        return opportunities
