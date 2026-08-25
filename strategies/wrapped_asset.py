"""Wrapped / liquid-staking asset arbitrage: WBTC vs BTC, stETH vs ETH, etc.

These assets are bounded to their underlying by a *redemption mechanism*,
not a market maker: WETH can be unwrapped instantly and atomically, while
stETH/wstETH, cbETH, and liquid-staked SOL derivatives sit behind a
withdrawal queue that can take hours to days and can be temporarily
closed. This module prices in that redemption path explicitly: the
"fair" deviation band widens with redemption latency (since more can go
wrong, and capital is locked longer, while a mispricing persists or
reverts), and no opportunity is flagged at all when the redemption path
is marked closed, since without it there is no bound on the spread.
"""

from __future__ import annotations

from dataclasses import dataclass

from config.settings import TAKER_FEE_FALLBACK
from config.universe import WRAPPED_UNDERLYING
from config.venues import taker_fee_for
from core.market_state import MarketState
from strategies.base import Leg, Opportunity, Strategy


@dataclass(frozen=True, slots=True)
class RedemptionInfo:
    """Redemption mechanics for one wrapped/liquid-staked asset."""

    instant: bool
    latency_sec: float
    currently_open: bool = True


# Representative redemption mechanics. In a real deployment these should
# be checked live (e.g. Lido/RocketPool withdrawal-queue contract state,
# Coinbase/WBTC merchant status) rather than hardcoded.
REDEMPTION_INFO: dict[str, RedemptionInfo] = {
    "WBTC": RedemptionInfo(instant=False, latency_sec=3600.0),          # merchant custodial mint/burn
    "WETH": RedemptionInfo(instant=True, latency_sec=15.0),             # atomic unwrap
    "STETH": RedemptionInfo(instant=False, latency_sec=2 * 86400.0),    # Lido withdrawal queue
    "WSTETH": RedemptionInfo(instant=False, latency_sec=2 * 86400.0),
    "RETH": RedemptionInfo(instant=True, latency_sec=60.0),             # Rocket Pool instant burn (deposit-pool dependent)
    "CBETH": RedemptionInfo(instant=False, latency_sec=3 * 86400.0),    # Coinbase-managed unstake queue
    "WBNB": RedemptionInfo(instant=True, latency_sec=15.0),
    "WSOL": RedemptionInfo(instant=True, latency_sec=5.0),
    "JITOSOL": RedemptionInfo(instant=False, latency_sec=2 * 86400.0),  # epoch-based unstake
    "MSOL": RedemptionInfo(instant=False, latency_sec=2 * 86400.0),
}

# Base deviation threshold (%) for an instantly-redeemable wrap; queued
# assets get a wider band scaled by this rate per day of latency.
BASE_THRESHOLD_PCT = 0.10
LATENCY_PREMIUM_PCT_PER_DAY = 0.15


def fair_deviation_band_pct(info: RedemptionInfo) -> float:
    """The deviation (%) beyond which a wrap/underlying spread is worth trading."""
    if info.instant:
        return BASE_THRESHOLD_PCT
    days = info.latency_sec / 86400.0
    return BASE_THRESHOLD_PCT + LATENCY_PREMIUM_PCT_PER_DAY * days


class WrappedAssetStrategy(Strategy):
    name = "wrapped_asset"

    def __init__(self, max_trade_usd: float = 500.0) -> None:
        super().__init__(min_profit_pct=0.0)  # threshold is computed per-asset, see fair_deviation_band_pct
        self.max_trade_usd = max_trade_usd

    def scan(self, market_state: MarketState) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        for wrapped, underlying in WRAPPED_UNDERLYING.items():
            info = REDEMPTION_INFO.get(wrapped)
            if info is None or not info.currently_open:
                continue

            for quote in ("USDT", "USDC"):
                wrapped_symbol = f"{wrapped}/{quote}"
                underlying_symbol = f"{underlying}/{quote}"
                opportunities.extend(
                    self._scan_pair(market_state, wrapped, underlying, wrapped_symbol, underlying_symbol, info)
                )

        opportunities.sort(key=lambda o: o.net_profit_pct, reverse=True)
        return opportunities

    def _scan_pair(
        self,
        market_state: MarketState,
        wrapped: str,
        underlying: str,
        wrapped_symbol: str,
        underlying_symbol: str,
        info: RedemptionInfo,
    ) -> list[Opportunity]:
        results: list[Opportunity] = []
        band_pct = fair_deviation_band_pct(info)

        for venue_id in market_state.book_store.venues_for_symbol(wrapped_symbol):
            wrapped_book = market_state.book_store.get(venue_id, wrapped_symbol)
            underlying_book = market_state.book_store.get(venue_id, underlying_symbol) or _best_underlying_book(
                market_state, underlying_symbol
            )
            if wrapped_book is None or underlying_book is None:
                continue

            w_state, u_state = wrapped_book.snapshot(), underlying_book.snapshot()
            if w_state.age_sec > market_state.staleness_sec or u_state.age_sec > market_state.staleness_sec:
                continue
            if w_state.best_bid <= 0 or u_state.best_ask <= 0:
                continue

            fee = taker_fee_for(venue_id, TAKER_FEE_FALLBACK)

            # Wrapped trades rich vs underlying: sell wrapped, buy underlying.
            deviation_pct = (w_state.best_bid - u_state.best_ask) / u_state.best_ask * 100.0
            net_pct = abs(deviation_pct) - band_pct - fee * 2 * 100.0
            if net_pct < 0:
                continue

            size = min(w_state.best_bid_size, u_state.best_ask_size)
            size_usd = min(size * u_state.best_ask, self.max_trade_usd)
            if size_usd <= 0:
                continue

            sell_wrapped = deviation_pct > 0
            legs = (
                Leg(venue_id, wrapped_symbol, "sell" if sell_wrapped else "buy", w_state.best_bid if sell_wrapped else w_state.best_ask, size_usd / u_state.best_ask, fee),
                Leg(venue_id, underlying_symbol, "buy" if sell_wrapped else "sell", u_state.best_ask if sell_wrapped else u_state.best_bid, size_usd / u_state.best_ask, fee),
            )

            results.append(
                Opportunity(
                    strategy=self.name,
                    symbol=f"{wrapped}/{underlying}",
                    legs=legs,
                    gross_profit_pct=abs(deviation_pct),
                    net_profit_pct=net_pct,
                    max_size_usd=size_usd,
                    requires_prefunded_inventory=True,
                    is_atomic=False,
                    detail={
                        "venue": venue_id,
                        "redemption_instant": info.instant,
                        "redemption_latency_sec": info.latency_sec,
                        "fair_band_pct": band_pct,
                        "deviation_pct": deviation_pct,
                        "note": "Bounded by redemption mechanics; unwind via direct redemption if the market spread doesn't revert.",
                    },
                )
            )
        return results


def _best_underlying_book(market_state: MarketState, underlying_symbol: str):
    books = market_state.book_store.all_for_symbol(underlying_symbol)
    return books[0] if books else None
