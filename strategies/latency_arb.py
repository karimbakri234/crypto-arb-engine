"""Latency / stale-quote arbitrage: trade a venue whose book hasn't
caught up to a move another venue's book already reflects.

Detected here via a per-venue *update-recency gap*: for a buy/sell venue
pair that already clears the ordinary cross-exchange profit bar (see
strategies/cross_exchange.py), this additionally requires the venue
you'd buy on to have a strictly older last-update timestamp than the
venue you'd sell on by at least `min_lag_sec` -- i.e. the cheap side
looks cheap because it is still quoting a stale price, not because of a
persistent structural spread. A real implementation would cross-correlate
tick-level price update *events* per venue to measure this lag directly;
this snapshot-level timestamp-gap heuristic is a reasonable approximation
given this engine's per-tick `MarketState` model.

**Be realistic about this category.** It is the single most
latency-competitive strategy in this engine. Firms colocated at exchange
data centers detect and act on exactly this kind of lag in
low-single-digit milliseconds. A Python bot polling or even streaming
over the public internet is very unlikely to win this race consistently
-- by the time you observe the lag, faster participants have usually
already closed it. Treat any opportunities this module surfaces as
evidence for the decay-curve analytics (see analytics/recorder.py), not
as opportunities you should expect to actually capture past `monitor`
mode.
"""

from __future__ import annotations

import numpy as np

from config.settings import TAKER_FEE_FALLBACK
from config.venues import taker_fee_for
from core.market_state import MarketState
from strategies.base import Leg, Opportunity, Strategy


class LatencyArbStrategy(Strategy):
    name = "latency_arb"

    def __init__(self, min_profit_pct: float = 0.1, min_lag_sec: float = 0.25, max_trade_usd: float = 500.0) -> None:
        super().__init__(min_profit_pct)
        self.min_lag_sec = min_lag_sec
        self.max_trade_usd = max_trade_usd

    def scan(self, market_state: MarketState) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        for symbol in market_state.symbols:
            opportunities.extend(self._scan_symbol(market_state, symbol))
        opportunities.sort(key=lambda o: o.net_profit_pct, reverse=True)
        return opportunities

    def _scan_symbol(self, market_state: MarketState, symbol: str) -> list[Opportunity]:
        matrix = market_state.matrix_for_symbol(symbol)
        n = len(matrix.venue_ids)
        if n < 2:
            return []

        fees = np.array([taker_fee_for(v, TAKER_FEE_FALLBACK) for v in matrix.venue_ids])
        ask = matrix.ask_prices[:, None]
        bid = matrix.bid_prices[None, :]
        gross_pct = (bid - ask) / ask * 100.0
        net_pct = gross_pct - (fees[:, None] + fees[None, :]) * 100.0
        np.fill_diagonal(net_pct, -np.inf)

        # Positive gap[i, j] means the sell venue (j) updated more recently
        # than the buy venue (i) -- the buy side's low ask looks stale.
        gap = matrix.timestamps[None, :] - matrix.timestamps[:, None]

        buy_idx, sell_idx = np.where((net_pct >= self.min_profit_pct) & (gap >= self.min_lag_sec))

        opportunities: list[Opportunity] = []
        for i, j in zip(buy_idx.tolist(), sell_idx.tolist()):
            buy_venue, sell_venue = matrix.venue_ids[i], matrix.venue_ids[j]
            size = min(float(matrix.ask_sizes[i]), float(matrix.bid_sizes[j]))
            size_usd = min(size * float(matrix.ask_prices[i]), self.max_trade_usd)
            if size_usd <= 0:
                continue

            opportunities.append(
                Opportunity(
                    strategy=self.name,
                    symbol=symbol,
                    legs=(
                        Leg(buy_venue, symbol, "buy", float(matrix.ask_prices[i]), size_usd / float(matrix.ask_prices[i]), float(fees[i])),
                        Leg(sell_venue, symbol, "sell", float(matrix.bid_prices[j]), size_usd / float(matrix.ask_prices[i]), float(fees[j])),
                    ),
                    gross_profit_pct=float(gross_pct[i, j]),
                    net_profit_pct=float(net_pct[i, j]),
                    max_size_usd=size_usd,
                    requires_prefunded_inventory=True,
                    is_atomic=False,
                    detail={
                        "buy_venue": buy_venue,
                        "sell_venue": sell_venue,
                        "update_lag_sec": float(gap[i, j]),
                        "warning": "Most latency-competitive category in this engine; see module docstring.",
                    },
                )
            )
        return opportunities
