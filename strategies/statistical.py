"""Statistical / pairs arbitrage: cointegration-based mean reversion.

**This is NOT true arbitrage.** Unlike every other strategy in this
engine, this one carries directional risk: it bets that a historically
cointegrated spread between two correlated assets (e.g. ETH vs a basket
proxy, or two similar L1 tokens) will revert to its mean, and that bet
can simply be wrong -- the spread can keep diverging and this strategy
can lose money. It is included because it is a common real-world source
of "arbitrage-adjacent" signal generation, not because it shares the
risk-free profile of the other 14 strategies.

Mechanics: maintain a rolling window of the price spread between two
assets (using an OLS hedge ratio, not a naive 1:1 difference), run an
Augmented Dickey-Fuller test each tick to confirm the spread is still
behaving like a mean-reverting (stationary) series, and trade its
z-score: enter beyond `entry_z`, exit back toward the mean at `exit_z`,
and hard stop-loss (exit regardless of the ADF result) if divergence
keeps widening past `stop_loss_z`.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
from statsmodels.tsa.stattools import adfuller

from config.settings import (
    STAT_ARB_ADF_PVALUE_MAX,
    STAT_ARB_ENTRY_Z,
    STAT_ARB_EXIT_Z,
    STAT_ARB_LOOKBACK,
    STAT_ARB_STOP_LOSS_Z,
)
from core.market_state import MarketState
from strategies.base import Leg, Opportunity, Strategy


@dataclass(slots=True)
class _PairState:
    history_a: deque[float]
    history_b: deque[float]
    position_open: bool = False
    direction: int = 0  # +1 = long A / short B, -1 = short A / long B


class StatisticalArbStrategy(Strategy):
    name = "statistical"

    def __init__(
        self,
        pairs: list[tuple[str, str]],
        lookback: int = STAT_ARB_LOOKBACK,
        entry_z: float = STAT_ARB_ENTRY_Z,
        exit_z: float = STAT_ARB_EXIT_Z,
        stop_loss_z: float = STAT_ARB_STOP_LOSS_Z,
        adf_pvalue_max: float = STAT_ARB_ADF_PVALUE_MAX,
    ) -> None:
        super().__init__(min_profit_pct=0.0)
        self.pairs = pairs
        self.lookback = lookback
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.stop_loss_z = stop_loss_z
        self.adf_pvalue_max = adf_pvalue_max
        self._state: dict[tuple[str, str], _PairState] = {
            pair: _PairState(deque(maxlen=lookback), deque(maxlen=lookback)) for pair in pairs
        }

    def scan(self, market_state: MarketState) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        for pair in self.pairs:
            symbol_a, symbol_b = pair
            price_a = _mid_price(market_state, symbol_a)
            price_b = _mid_price(market_state, symbol_b)
            if price_a is None or price_b is None:
                continue

            state = self._state[pair]
            state.history_a.append(price_a)
            state.history_b.append(price_b)
            if len(state.history_a) < self.lookback:
                continue

            opp = self._evaluate_pair(pair, state, price_a, price_b)
            if opp is not None:
                opportunities.append(opp)

        return opportunities

    def _evaluate_pair(
        self, pair: tuple[str, str], state: _PairState, price_a: float, price_b: float
    ) -> Opportunity | None:
        symbol_a, symbol_b = pair
        series_a = np.array(state.history_a)
        series_b = np.array(state.history_b)

        # A degenerate (constant) hedge-asset series over the lookback window
        # (e.g. a thin book with no trades) makes the OLS fit meaningless and
        # can hand adfuller a degenerate series; treat it as "not evaluable"
        # rather than letting a numeric edge case crash the detection loop.
        if series_b.std() <= 0 or series_a.std() <= 0:
            return None

        hedge_ratio = float(np.polyfit(series_b, series_a, 1)[0])
        spread_series = series_a - hedge_ratio * series_b
        spread_now = spread_series[-1]
        mean, std = float(spread_series.mean()), float(spread_series.std())
        if not np.isfinite(std) or std <= 0:
            return None
        z = (spread_now - mean) / std

        try:
            adf_pvalue = float(adfuller(spread_series)[1])
        except (ValueError, np.linalg.LinAlgError):
            adf_pvalue = 1.0  # treat as "not cointegrated" rather than crashing
        is_cointegrated = adf_pvalue <= self.adf_pvalue_max

        if state.position_open:
            if abs(z) >= self.stop_loss_z:
                return self._make_signal(pair, state, price_a, price_b, hedge_ratio, z, adf_pvalue, "stop_loss_exit", close=True)
            if abs(z) <= self.exit_z:
                return self._make_signal(pair, state, price_a, price_b, hedge_ratio, z, adf_pvalue, "mean_reversion_exit", close=True)
            return None

        if not is_cointegrated:
            return None
        if abs(z) >= self.stop_loss_z:
            return None  # too extreme to treat as a reversion entry
        if abs(z) < self.entry_z:
            return None

        direction = -1 if z > 0 else 1  # spread rich (z>0) -> short A / long B
        state.position_open = True
        state.direction = direction
        return self._make_signal(pair, state, price_a, price_b, hedge_ratio, z, adf_pvalue, "entry", close=False)

    def _make_signal(
        self,
        pair: tuple[str, str],
        state: _PairState,
        price_a: float,
        price_b: float,
        hedge_ratio: float,
        z: float,
        adf_pvalue: float,
        signal_type: str,
        close: bool,
    ) -> Opportunity:
        symbol_a, symbol_b = pair
        direction = state.direction if not close else -state.direction
        if close:
            state.position_open = False
            state.direction = 0

        side_a = "sell" if direction > 0 else "buy"
        side_b = "buy" if direction > 0 else "sell"

        legs = (
            Leg("stat_arb_venue_a", symbol_a, side_a, price_a, 1.0, 0.0),
            Leg("stat_arb_venue_b", symbol_b, side_b, price_b, hedge_ratio, 0.0),
        )
        return Opportunity(
            strategy=self.name,
            symbol=f"{symbol_a}~{symbol_b}",
            legs=legs,
            gross_profit_pct=abs(z),
            net_profit_pct=abs(z),
            max_size_usd=0.0,
            requires_prefunded_inventory=True,
            is_atomic=False,
            detail={
                "signal_type": signal_type,
                "z_score": z,
                "hedge_ratio": hedge_ratio,
                "adf_pvalue": adf_pvalue,
                "warning": "NOT true arbitrage: directional mean-reversion bet, can lose money if the spread doesn't revert.",
            },
        )


def _mid_price(market_state: MarketState, symbol: str) -> float | None:
    books = market_state.book_store.all_for_symbol(symbol)
    if not books:
        return None
    state = books[0].snapshot()
    if state.best_bid <= 0 or state.best_ask <= 0:
        return None
    return (state.best_bid + state.best_ask) / 2.0
