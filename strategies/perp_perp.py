"""Perp-perp arbitrage: the same perpetual contract priced differently
across two venues.

Long the cheap venue's perp, short the expensive venue's perp -- a
delta-neutral position (long and short the same underlying in equal
notional) that captures the price spread. Because both legs are perps,
the position also earns (or pays) the *funding-rate differential* between
the two venues for as long as it's held; that differential is surfaced in
`detail` as a bonus context on top of the price-spread edge that gates
the opportunity.

Perp order books are looked up under the `"{SYMBOL}-PERP"` key convention
used throughout this engine (see strategies/funding_rate.py).
"""

from __future__ import annotations

import numpy as np

from config.settings import TAKER_FEE_FALLBACK
from config.venues import taker_fee_for
from core.market_state import MarketState
from strategies.base import Leg, Opportunity, Strategy


class PerpPerpStrategy(Strategy):
    name = "perp_perp"

    def __init__(self, min_profit_pct: float = 0.1, max_trade_usd: float = 500.0) -> None:
        super().__init__(min_profit_pct)
        self.max_trade_usd = max_trade_usd

    def scan(self, market_state: MarketState) -> list[Opportunity]:
        perp_symbols = {s for s in market_state.book_store.all_symbols() if s.endswith("-PERP")}
        opportunities: list[Opportunity] = []
        for perp_symbol in perp_symbols:
            opportunities.extend(self._scan_symbol(market_state, perp_symbol))
        opportunities.sort(key=lambda o: o.net_profit_pct, reverse=True)
        return opportunities

    def _scan_symbol(self, market_state: MarketState, perp_symbol: str) -> list[Opportunity]:
        matrix = market_state.matrix_for_symbol(perp_symbol)
        n = len(matrix.venue_ids)
        if n < 2:
            return []

        fees = np.array([taker_fee_for(v, TAKER_FEE_FALLBACK) for v in matrix.venue_ids])
        ask = matrix.ask_prices[:, None]
        bid = matrix.bid_prices[None, :]
        gross_pct = (bid - ask) / ask * 100.0
        fee_cost_pct = (fees[:, None] + fees[None, :]) * 100.0
        net_pct = gross_pct - fee_cost_pct
        np.fill_diagonal(net_pct, -np.inf)

        long_idx, short_idx = np.where(net_pct >= self.min_profit_pct)
        spot_symbol = perp_symbol.removesuffix("-PERP")

        opportunities: list[Opportunity] = []
        for i, j in zip(long_idx.tolist(), short_idx.tolist()):
            long_venue, short_venue = matrix.venue_ids[i], matrix.venue_ids[j]
            size = min(float(matrix.ask_sizes[i]), float(matrix.bid_sizes[j]))
            size_usd = min(size * float(matrix.ask_prices[i]), self.max_trade_usd)
            if size_usd <= 0:
                continue

            funding_long = market_state.funding_rates.get((long_venue, spot_symbol))
            funding_short = market_state.funding_rates.get((short_venue, spot_symbol))
            funding_diff_annualized = 0.0
            if funding_long and funding_short:
                # Long pays funding when positive; short receives it, so the
                # differential in the long's favor is short's rate minus long's.
                funding_diff_annualized = funding_short.annualized_pct - funding_long.annualized_pct

            opportunities.append(
                Opportunity(
                    strategy=self.name,
                    symbol=perp_symbol,
                    legs=(
                        Leg(long_venue, perp_symbol, "buy", float(matrix.ask_prices[i]), size_usd / float(matrix.ask_prices[i]), float(fees[i])),
                        Leg(short_venue, perp_symbol, "sell", float(matrix.bid_prices[j]), size_usd / float(matrix.ask_prices[i]), float(fees[j])),
                    ),
                    gross_profit_pct=float(gross_pct[i, j]),
                    net_profit_pct=float(net_pct[i, j]),
                    max_size_usd=size_usd,
                    requires_prefunded_inventory=True,
                    is_atomic=False,
                    detail={
                        "long_venue": long_venue,
                        "short_venue": short_venue,
                        "funding_differential_annualized_pct": funding_diff_annualized,
                        "note": "Delta-neutral; funding differential is a bonus on top of the price spread, not the entry gate.",
                    },
                )
            )
        return opportunities
