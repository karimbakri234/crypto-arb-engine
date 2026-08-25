"""Calendar spread arbitrage: mispricing between two expiries, same venue.

For each venue/symbol with 2+ listed expiries, this computes the implied
*marginal forward rate* between every consecutive pair of expiries
(the annualized rate of return for holding from expiry T1 to expiry T2,
derived from their prices) and compares it against the average marginal
rate across that venue's whole curve for the symbol. A pair whose
marginal rate diverges materially from the rest of the curve is a
calendar-spread opportunity: buy the relatively cheap leg, sell the
relatively rich one, and unwind when the curve re-normalizes (or hold
both to their respective expiries).
"""

from __future__ import annotations

import math
from collections import defaultdict

from config.settings import TAKER_FEE_FALLBACK
from config.venues import taker_fee_for
from core.market_state import MarketState
from core.types import FuturesQuote
from strategies.base import Leg, Opportunity, Strategy


def marginal_forward_rate_pct(near: FuturesQuote, far: FuturesQuote) -> float:
    """Annualized implied forward rate for holding from `near`'s expiry to `far`'s."""
    day_diff = far.days_to_expiry - near.days_to_expiry
    if day_diff <= 0 or near.price <= 0:
        return 0.0
    return math.log(far.price / near.price) * (365.0 / day_diff) * 100.0


class CalendarSpreadStrategy(Strategy):
    name = "calendar_spread"

    def __init__(self, min_divergence_pct: float = 2.0, max_trade_usd: float = 500.0) -> None:
        super().__init__(min_profit_pct=min_divergence_pct)
        self.max_trade_usd = max_trade_usd

    def scan(self, market_state: MarketState) -> list[Opportunity]:
        by_venue_symbol: dict[tuple[str, str], list[FuturesQuote]] = defaultdict(list)
        for (venue_id, symbol, _expiry), fq in market_state.futures_quotes.items():
            by_venue_symbol[(venue_id, symbol)].append(fq)

        opportunities: list[Opportunity] = []
        for (venue_id, symbol), quotes in by_venue_symbol.items():
            if len(quotes) < 2:
                continue
            quotes.sort(key=lambda q: q.expiry_ts)
            pairs = list(zip(quotes, quotes[1:]))
            marginal_rates = [marginal_forward_rate_pct(near, far) for near, far in pairs]
            curve_mean = sum(marginal_rates) / len(marginal_rates)

            fee = taker_fee_for(venue_id, TAKER_FEE_FALLBACK)
            for (near, far), rate in zip(pairs, marginal_rates):
                divergence = rate - curve_mean
                if abs(divergence) < self.min_profit_pct:
                    continue

                size_usd = min(self.max_trade_usd, near.price * 10)
                size_units = size_usd / near.price if near.price else 0.0

                # Forward rate too high relative to the curve -> far leg rich -> sell far, buy near.
                near_side, far_side = ("buy", "sell") if divergence > 0 else ("sell", "buy")
                legs = (
                    Leg(venue_id, f"{symbol}-{int(near.expiry_ts)}", near_side, near.price, size_units, fee),
                    Leg(venue_id, f"{symbol}-{int(far.expiry_ts)}", far_side, far.price, size_units, fee),
                )

                opportunities.append(
                    Opportunity(
                        strategy=self.name,
                        symbol=symbol,
                        legs=legs,
                        gross_profit_pct=abs(divergence),
                        net_profit_pct=abs(divergence) - fee * 2 * 100.0,
                        max_size_usd=size_usd,
                        requires_prefunded_inventory=True,
                        is_atomic=False,
                        detail={
                            "venue": venue_id,
                            "near_expiry_ts": near.expiry_ts,
                            "far_expiry_ts": far.expiry_ts,
                            "marginal_forward_rate_pct": rate,
                            "curve_mean_pct": curve_mean,
                            "divergence_pct": divergence,
                        },
                    )
                )

        opportunities.sort(key=lambda o: o.net_profit_pct, reverse=True)
        return opportunities
