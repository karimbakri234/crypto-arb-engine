"""Funding-rate arbitrage: collect perpetual-futures funding, delta-neutral.

Perps pay funding every 1-8h depending on venue. When funding is strongly
positive, shorting the perp and buying an equal-notional amount of spot
is delta-neutral and collects the funding payment; when funding is
strongly negative, the reverse (long perp, short/sell spot) collects it.
All funding rates across all perp venues are ranked by *annualized* rate
so a 0.01%/8h rate and a 0.03%/1h rate are comparable on the same basis.

The position must be held across the funding timestamp to actually
collect the payment -- entering right before and exiting right after
is the whole point, and `detail["seconds_to_funding"]` surfaces how long
that hold is.
"""

from __future__ import annotations

from config.settings import TAKER_FEE_FALLBACK
from config.venues import taker_fee_for
from core.market_state import MarketState
from strategies.base import Leg, Opportunity, Strategy


class FundingRateStrategy(Strategy):
    name = "funding_rate"

    def __init__(self, min_annualized_pct: float = 5.0, max_trade_usd: float = 500.0) -> None:
        super().__init__(min_profit_pct=min_annualized_pct)
        self.max_trade_usd = max_trade_usd

    def scan(self, market_state: MarketState) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        for (venue_id, symbol), funding in market_state.funding_rates.items():
            annualized = funding.annualized_pct
            if abs(annualized) < self.min_profit_pct:
                continue

            spot_book = market_state.book_store.get(venue_id, symbol) or _first_spot_book(market_state, symbol)
            if spot_book is None:
                continue
            spot_state = spot_book.snapshot()
            if spot_state.age_sec > market_state.staleness_sec or spot_state.best_bid <= 0:
                continue

            fee = taker_fee_for(venue_id, TAKER_FEE_FALLBACK)
            size_usd = min(self.max_trade_usd, spot_state.best_bid_size * spot_state.best_bid)
            if size_usd <= 0:
                continue
            size_units = size_usd / spot_state.best_bid

            if annualized > 0:
                # Positive funding: shorts get paid. Short perp, buy spot.
                perp_side, spot_side = "sell", "buy"
            else:
                perp_side, spot_side = "buy", "sell"

            mid_price = (spot_state.best_bid + spot_state.best_ask) / 2.0
            legs = (
                Leg(venue_id, f"{symbol}-PERP", perp_side, mid_price, size_units, fee),
                Leg(venue_id, symbol, spot_side, spot_state.best_bid, size_units, fee),
            )

            opportunities.append(
                Opportunity(
                    strategy=self.name,
                    symbol=symbol,
                    legs=legs,
                    gross_profit_pct=abs(annualized),
                    net_profit_pct=abs(annualized) - (fee * 2 * 100.0),
                    max_size_usd=size_usd,
                    requires_prefunded_inventory=True,
                    is_atomic=False,
                    detail={
                        "venue": venue_id,
                        "raw_rate": funding.rate,
                        "interval_hours": funding.interval_hours,
                        "annualized_pct": annualized,
                        "seconds_to_funding": max(funding.next_funding_ts - spot_state.timestamp, 0.0),
                        "note": "Delta-neutral; must hold the position across the funding timestamp to collect it.",
                    },
                )
            )

        opportunities.sort(key=lambda o: o.net_profit_pct, reverse=True)
        return opportunities


def _first_spot_book(market_state: MarketState, symbol: str):
    books = market_state.book_store.all_for_symbol(symbol)
    return books[0] if books else None
