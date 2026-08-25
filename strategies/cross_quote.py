"""Cross-quote arbitrage: the same base asset quoted against different
stablecoin quotes on the same venue (BTC/USDT vs BTC/USDC vs BTC/FDUSD).

Since the quote assets are all intended to be ~$1, `BASE/USDT` and
`BASE/USDC` order books on one venue are implicitly two independent
quotes on the same cross rate (`USDT/USDC`). That implied cross rate
frequently diverges more than the stablecoin peg spread alone would
justify -- and because both legs are on the same venue with deep,
correlated books, this is cheap to scan and often overlooked relative to
cross-exchange spatial arbitrage.
"""

from __future__ import annotations

from itertools import combinations

from config.settings import TAKER_FEE_FALLBACK
from config.universe import STABLECOINS
from config.venues import taker_fee_for
from core.market_state import MarketState
from strategies.base import Leg, Opportunity, Strategy


class CrossQuoteStrategy(Strategy):
    name = "cross_quote"

    def __init__(self, min_profit_pct: float = 0.1, max_trade_usd: float = 500.0) -> None:
        super().__init__(min_profit_pct)
        self.max_trade_usd = max_trade_usd

    def scan(self, market_state: MarketState) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        by_base: dict[tuple[str, str], list[str]] = {}
        for symbol in market_state.symbols:
            if "/" not in symbol:
                continue
            base, quote = symbol.split("/")
            if quote not in STABLECOINS:
                continue
            for venue_id in market_state.book_store.venues_for_symbol(symbol):
                by_base.setdefault((venue_id, base), []).append(symbol)

        for (venue_id, base), symbols in by_base.items():
            if len(symbols) < 2:
                continue
            fee = taker_fee_for(venue_id, TAKER_FEE_FALLBACK)
            for symbol_a, symbol_b in combinations(symbols, 2):
                opp = self._compare(market_state, venue_id, base, symbol_a, symbol_b, fee)
                if opp is not None:
                    opportunities.append(opp)
                opp = self._compare(market_state, venue_id, base, symbol_b, symbol_a, fee)
                if opp is not None:
                    opportunities.append(opp)

        opportunities.sort(key=lambda o: o.net_profit_pct, reverse=True)
        return opportunities

    def _compare(
        self, market_state: MarketState, venue_id: str, base: str, buy_symbol: str, sell_symbol: str, fee: float
    ) -> Opportunity | None:
        buy_book = market_state.book_store.get(venue_id, buy_symbol)
        sell_book = market_state.book_store.get(venue_id, sell_symbol)
        if buy_book is None or sell_book is None:
            return None
        buy_state, sell_state = buy_book.snapshot(), sell_book.snapshot()
        if buy_state.age_sec > market_state.staleness_sec or sell_state.age_sec > market_state.staleness_sec:
            return None
        if buy_state.best_ask <= 0 or sell_state.best_bid <= 0:
            return None

        # Buy BASE with quote-of-buy_symbol, sell BASE for quote-of-sell_symbol.
        # Assumes the two quote stablecoins are ~1:1; the divergence this
        # finds is exactly the mispricing beyond what that peg justifies.
        gross_pct = (sell_state.best_bid - buy_state.best_ask) / buy_state.best_ask * 100.0
        net_pct = gross_pct - fee * 2 * 100.0
        if net_pct < self.min_profit_pct:
            return None

        size = min(buy_state.best_ask_size, sell_state.best_bid_size)
        size_usd = min(size * buy_state.best_ask, self.max_trade_usd)
        if size_usd <= 0:
            return None

        legs = (
            Leg(venue_id, buy_symbol, "buy", buy_state.best_ask, size_usd / buy_state.best_ask, fee),
            Leg(venue_id, sell_symbol, "sell", sell_state.best_bid, size_usd / buy_state.best_ask, fee),
        )
        return Opportunity(
            strategy=self.name,
            symbol=f"{base}:{buy_symbol.split('/')[1]}/{sell_symbol.split('/')[1]}",
            legs=legs,
            gross_profit_pct=gross_pct,
            net_profit_pct=net_pct,
            max_size_usd=size_usd,
            requires_prefunded_inventory=False,
            is_atomic=False,
            detail={"venue": venue_id, "buy_symbol": buy_symbol, "sell_symbol": sell_symbol},
        )
