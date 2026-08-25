"""The shared, point-in-time market snapshot every strategy scans.

`MarketState` bundles the current `BookStore`, plus the auxiliary data
non-spot strategies need (funding rates, dated-futures quotes, DEX pool
reserves, gas prices) and precomputed numpy matrices for vectorized
cross-exchange comparisons. One `MarketState` is built per detection
tick and handed to every strategy running that tick — strategies never
touch the feed layer directly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from core.book import BookStore
from core.types import FundingRate, FuturesQuote


@dataclass(slots=True)
class PoolState:
    """Cached AMM pool reserves, used for local constant-product/CLMM math
    without a network round-trip (see strategies/dex_dex.py)."""

    dex_id: str
    chain: str
    symbol: str  # e.g. "ETH/USDC"
    reserve_base: float
    reserve_quote: float
    fee: float
    timestamp: float = field(default_factory=time.time)

    # Concentrated-liquidity pools (Uniswap v3 style) additionally carry an
    # active-tick liquidity figure; None for constant-product (v2) pools.
    tick_liquidity: float | None = None
    sqrt_price_x96: float | None = None


@dataclass(slots=True)
class SymbolMatrix:
    """Vectorized top-of-book view of one symbol across venues."""

    venue_ids: list[str]
    bid_prices: np.ndarray
    ask_prices: np.ndarray
    bid_sizes: np.ndarray
    ask_sizes: np.ndarray
    timestamps: np.ndarray


@dataclass(slots=True)
class MarketState:
    """Point-in-time snapshot handed to every strategy's `scan`."""

    book_store: BookStore
    symbols: list[str]
    taken_at: float = field(default_factory=time.time)
    funding_rates: dict[tuple[str, str], FundingRate] = field(default_factory=dict)
    futures_quotes: dict[tuple[str, str, float], FuturesQuote] = field(default_factory=dict)
    dex_pools: dict[str, PoolState] = field(default_factory=dict)
    gas_price_usd: dict[str, float] = field(default_factory=dict)
    staleness_sec: float = 3.0

    def matrix_for_symbol(self, symbol: str) -> SymbolMatrix:
        """Build venue x price/size numpy arrays for `symbol` in one pass.

        This is what lets cross-exchange detection be a matrix operation
        instead of a Python double loop over venue pairs.
        """
        books = self.book_store.all_for_symbol(symbol)
        venue_ids: list[str] = []
        bids: list[float] = []
        asks: list[float] = []
        bid_sizes: list[float] = []
        ask_sizes: list[float] = []
        timestamps: list[float] = []

        now = time.time()
        for book in books:
            state = book.snapshot()
            if state.timestamp == 0.0 or (now - state.timestamp) > self.staleness_sec:
                continue
            if state.bid_prices.size == 0 or state.ask_prices.size == 0:
                continue
            venue_ids.append(book.venue_id)
            bids.append(state.best_bid)
            asks.append(state.best_ask)
            bid_sizes.append(state.best_bid_size)
            ask_sizes.append(state.best_ask_size)
            timestamps.append(state.timestamp)

        return SymbolMatrix(
            venue_ids=venue_ids,
            bid_prices=np.array(bids, dtype=np.float64),
            ask_prices=np.array(asks, dtype=np.float64),
            bid_sizes=np.array(bid_sizes, dtype=np.float64),
            ask_sizes=np.array(ask_sizes, dtype=np.float64),
            timestamps=np.array(timestamps, dtype=np.float64),
        )
