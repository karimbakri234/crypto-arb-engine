"""Maker-rebate arbitrage: cross-exchange spreads that only clear net of
fees when at least one leg fills passively (as a maker) rather than as a
taker.

Some venues pay a *negative* maker fee (a rebate) to add liquidity. A
cross-exchange spread that's unprofitable taker-taker (see
strategies/cross_exchange.py) can still be profitable maker-taker or
maker-maker once one or both legs earn a rebate instead of paying a
taker fee. This module prices all three fill-path combinations
(taker-taker, maker-taker, maker-maker) and flags opportunities that only
clear under a maker-involving path -- which comes with real **fill
risk**: a passive (maker) order might not fill at all before the price
moves, unlike a taker order's near-certain fill. `detail["fill_risk"]`
flags this explicitly on every opportunity this module emits.
"""

from __future__ import annotations

import numpy as np

from config.settings import MAKER_FEE_FALLBACK, TAKER_FEE_FALLBACK
from config.venues import maker_fee_for, taker_fee_for
from core.market_state import MarketState
from strategies.base import Leg, Opportunity, Strategy


class MakerRebateStrategy(Strategy):
    name = "maker_rebate"

    def __init__(self, min_profit_pct: float = 0.05, max_trade_usd: float = 500.0) -> None:
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

        taker = np.array([taker_fee_for(v, TAKER_FEE_FALLBACK) for v in matrix.venue_ids])
        maker = np.array([maker_fee_for(v, MAKER_FEE_FALLBACK) for v in matrix.venue_ids])

        ask = matrix.ask_prices[:, None]
        bid = matrix.bid_prices[None, :]
        gross_pct = (bid - ask) / ask * 100.0

        taker_taker_pct = gross_pct - (taker[:, None] + taker[None, :]) * 100.0
        maker_taker_pct = gross_pct - (maker[:, None] + taker[None, :]) * 100.0
        taker_maker_pct = gross_pct - (taker[:, None] + maker[None, :]) * 100.0
        maker_maker_pct = gross_pct - (maker[:, None] + maker[None, :]) * 100.0

        best_pct = np.maximum.reduce([maker_taker_pct, taker_maker_pct, maker_maker_pct])
        np.fill_diagonal(best_pct, -np.inf)
        np.fill_diagonal(taker_taker_pct, -np.inf)

        # Only interesting when a maker-involving path clears the bar but
        # taker-taker alone would not have.
        buy_idx, sell_idx = np.where((best_pct >= self.min_profit_pct) & (taker_taker_pct < self.min_profit_pct))

        opportunities: list[Opportunity] = []
        for i, j in zip(buy_idx.tolist(), sell_idx.tolist(), strict=True):
            buy_venue, sell_venue = matrix.venue_ids[i], matrix.venue_ids[j]
            paths = {
                "maker_taker": float(maker_taker_pct[i, j]),
                "taker_maker": float(taker_maker_pct[i, j]),
                "maker_maker": float(maker_maker_pct[i, j]),
            }
            best_path_name = max(paths, key=paths.get)
            best_path_pct = paths[best_path_name]

            buy_fee = maker[i] if best_path_name == "maker_taker" or best_path_name == "maker_maker" else taker[i]
            sell_fee = maker[j] if best_path_name in ("taker_maker", "maker_maker") else taker[j]

            size = min(float(matrix.ask_sizes[i]), float(matrix.bid_sizes[j]))
            size_usd = min(size * float(matrix.ask_prices[i]), self.max_trade_usd)
            if size_usd <= 0:
                continue

            opportunities.append(
                Opportunity(
                    strategy=self.name,
                    symbol=symbol,
                    legs=(
                        Leg(buy_venue, symbol, "buy", float(matrix.ask_prices[i]), size_usd / float(matrix.ask_prices[i]), float(buy_fee)),
                        Leg(sell_venue, symbol, "sell", float(matrix.bid_prices[j]), size_usd / float(matrix.ask_prices[i]), float(sell_fee)),
                    ),
                    gross_profit_pct=float(gross_pct[i, j]),
                    net_profit_pct=best_path_pct,
                    max_size_usd=size_usd,
                    requires_prefunded_inventory=True,
                    is_atomic=False,
                    detail={
                        "buy_venue": buy_venue,
                        "sell_venue": sell_venue,
                        "fill_path": best_path_name,
                        "taker_taker_pct": float(taker_taker_pct[i, j]),
                        "fill_risk": "This path requires a passive (maker) leg, which may not fill before the price moves.",
                    },
                )
            )
        return opportunities
