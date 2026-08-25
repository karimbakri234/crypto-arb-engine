"""Cross-exchange (spatial) arbitrage: buy low on venue A, sell high on venue B.

The baseline strategy. Detection is fully vectorized: rather than a
Python double loop over venue pairs, the full buy-ask x sell-bid spread
matrix for a symbol is computed in one numpy pass, and profitable cells
are extracted with `np.where`.

Like every non-atomic, non-transfer strategy in this engine, both legs
must trade against capital that is *already* sitting on both venues —
see the module docstring in execution/executor.py for why "buy on A,
transfer, sell on B" does not work at arbitrage timescales.
"""

from __future__ import annotations

import numpy as np

from config.settings import TAKER_FEE_FALLBACK
from config.venues import taker_fee_for
from core.market_state import MarketState
from strategies.base import Leg, Opportunity, Strategy


class CrossExchangeStrategy(Strategy):
    name = "cross_exchange"

    def __init__(self, min_profit_pct: float = 0.1, max_trade_usd: float = 500.0) -> None:
        super().__init__(min_profit_pct)
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

        # profit_pct[i, j] = profit of buying ask on venue i, selling bid on venue j
        ask = matrix.ask_prices[:, None]  # (n, 1) -- buy venue on rows
        bid = matrix.bid_prices[None, :]  # (1, n) -- sell venue on columns
        gross_pct = (bid - ask) / ask * 100.0
        fee_cost_pct = (fees[:, None] + fees[None, :]) * 100.0
        net_pct = gross_pct - fee_cost_pct

        np.fill_diagonal(net_pct, -np.inf)  # never match a venue against itself
        buy_idx, sell_idx = np.where(net_pct >= self.min_profit_pct)

        opportunities: list[Opportunity] = []
        for i, j in zip(buy_idx.tolist(), sell_idx.tolist()):
            buy_venue, sell_venue = matrix.venue_ids[i], matrix.venue_ids[j]
            buy_price, sell_price = float(matrix.ask_prices[i]), float(matrix.bid_prices[j])
            size_units = min(float(matrix.ask_sizes[i]), float(matrix.bid_sizes[j]))
            size_usd = min(size_units * buy_price, self.max_trade_usd)
            if size_usd <= 0:
                continue

            opportunities.append(
                Opportunity(
                    strategy=self.name,
                    symbol=symbol,
                    legs=(
                        Leg(buy_venue, symbol, "buy", buy_price, size_usd / buy_price, float(fees[i])),
                        Leg(sell_venue, symbol, "sell", sell_price, size_usd / buy_price, float(fees[j])),
                    ),
                    gross_profit_pct=float(gross_pct[i, j]),
                    net_profit_pct=float(net_pct[i, j]),
                    max_size_usd=size_usd,
                    requires_prefunded_inventory=True,
                    is_atomic=False,
                    detail={"buy_venue": buy_venue, "sell_venue": sell_venue},
                )
            )
        return opportunities
