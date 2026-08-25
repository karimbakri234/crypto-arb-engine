"""Basis / cash-and-carry arbitrage: dated futures vs. spot.

When a dated future trades above spot (contango, positive basis), buying
spot and shorting the future locks in the basis as a riskless yield until
expiry (the "cash and carry" trade). The reverse (backwardation, negative
basis: short spot / long the future) is also scanned. Every listed
expiry across every venue is compared on an *annualized* basis so a
basis of 0.5% at 30 days and 2% at 180 days are comparable.
"""

from __future__ import annotations

from config.settings import TAKER_FEE_FALLBACK
from config.venues import taker_fee_for
from core.market_state import MarketState
from strategies.base import Leg, Opportunity, Strategy


class BasisCarryStrategy(Strategy):
    name = "basis_carry"

    def __init__(self, min_annualized_pct: float = 3.0, max_trade_usd: float = 500.0) -> None:
        super().__init__(min_profit_pct=min_annualized_pct)
        self.max_trade_usd = max_trade_usd

    def scan(self, market_state: MarketState) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        for (venue_id, symbol, expiry_ts), futures_quote in market_state.futures_quotes.items():
            annualized = futures_quote.annualized_basis_pct
            if abs(annualized) < self.min_profit_pct:
                continue
            if futures_quote.spot_price <= 0:
                continue

            fee = taker_fee_for(venue_id, TAKER_FEE_FALLBACK)
            size_usd = min(self.max_trade_usd, futures_quote.spot_price * 10)
            size_units = size_usd / futures_quote.spot_price

            if annualized > 0:
                # Contango: buy spot, short the future.
                spot_side, future_side = "buy", "sell"
            else:
                # Backwardation: sell/short spot, long the future.
                spot_side, future_side = "sell", "buy"

            legs = (
                Leg(venue_id, symbol, spot_side, futures_quote.spot_price, size_units, fee),
                Leg(venue_id, f"{symbol}-{int(expiry_ts)}", future_side, futures_quote.price, size_units, fee),
            )

            opportunities.append(
                Opportunity(
                    strategy=self.name,
                    symbol=symbol,
                    legs=legs,
                    gross_profit_pct=abs(futures_quote.basis_pct),
                    net_profit_pct=abs(annualized) - (fee * 2 * 100.0),
                    max_size_usd=size_usd,
                    requires_prefunded_inventory=True,
                    is_atomic=False,
                    detail={
                        "venue": venue_id,
                        "expiry_ts": expiry_ts,
                        "days_to_expiry": futures_quote.days_to_expiry,
                        "basis_pct": futures_quote.basis_pct,
                        "annualized_basis_pct": annualized,
                        "note": "Position must be held to expiry (or unwound early) to realize the locked basis.",
                    },
                )
            )

        opportunities.sort(key=lambda o: o.net_profit_pct, reverse=True)
        return opportunities
