"""In-memory order book state with lock-free reads.

`OrderBook` keeps bid/ask price and size levels as numpy arrays. Updates
build a *new* pair of arrays and atomically swap a single reference to
them (`self._state = new_state`); reference assignment is atomic in
CPython, so a reader that grabbed `book.snapshot()` a moment earlier
keeps looking at a fully consistent, immutable array pair even while a
writer is mid-update. No explicit lock is needed as long as writers never
mutate the arrays a snapshot points to in place — this module never does.

`BookStore` is the shared, process-wide table of `OrderBook`s that
`feed_manager.py` writes into and every detection strategy reads from.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class BookState:
    """An immutable snapshot of one order book's levels."""

    bid_prices: np.ndarray
    bid_sizes: np.ndarray
    ask_prices: np.ndarray
    ask_sizes: np.ndarray
    timestamp: float

    @property
    def best_bid(self) -> float:
        return float(self.bid_prices[0]) if self.bid_prices.size else 0.0

    @property
    def best_ask(self) -> float:
        return float(self.ask_prices[0]) if self.ask_prices.size else float("inf")

    @property
    def best_bid_size(self) -> float:
        return float(self.bid_sizes[0]) if self.bid_sizes.size else 0.0

    @property
    def best_ask_size(self) -> float:
        return float(self.ask_sizes[0]) if self.ask_sizes.size else 0.0

    @property
    def age_sec(self) -> float:
        return time.time() - self.timestamp

    def vwap_fill_price(self, side: str, size: float) -> tuple[float, float]:
        """Volume-weighted average fill price for `size` units on `side`.

        `side` is "buy" (walks the ask side) or "sell" (walks the bid
        side). Returns `(vwap_price, filled_size)`; `filled_size` is less
        than `size` if the book doesn't have enough displayed depth.
        """
        prices = self.ask_prices if side == "buy" else self.bid_prices
        sizes = self.ask_sizes if side == "buy" else self.bid_sizes

        remaining = size
        cost = 0.0
        filled = 0.0
        for price, level_size in zip(prices, sizes, strict=True):
            take = min(remaining, level_size)
            cost += take * price
            filled += take
            remaining -= take
            if remaining <= 0:
                break

        if filled <= 0:
            return (0.0, 0.0)
        return (cost / filled, filled)


_EMPTY = np.array([], dtype=np.float64)
_EMPTY_STATE = BookState(_EMPTY, _EMPTY, _EMPTY, _EMPTY, 0.0)


class OrderBook:
    """A single symbol/venue order book, updated incrementally from a diff stream."""

    __slots__ = ("venue_id", "symbol", "_state")

    def __init__(self, venue_id: str, symbol: str) -> None:
        self.venue_id = venue_id
        self.symbol = symbol
        self._state: BookState = _EMPTY_STATE

    def snapshot(self) -> BookState:
        """Return the current immutable state. Safe to call from any reader."""
        return self._state

    def replace(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> None:
        """Replace the full book (used for REST snapshots or resync)."""
        bids_sorted = sorted(bids, key=lambda level: -level[0])
        asks_sorted = sorted(asks, key=lambda level: level[0])
        self._state = BookState(
            bid_prices=np.array([p for p, _ in bids_sorted], dtype=np.float64),
            bid_sizes=np.array([s for _, s in bids_sorted], dtype=np.float64),
            ask_prices=np.array([p for p, _ in asks_sorted], dtype=np.float64),
            ask_sizes=np.array([s for _, s in asks_sorted], dtype=np.float64),
            timestamp=time.time(),
        )

    def apply_diff(
        self,
        bid_updates: list[tuple[float, float]],
        ask_updates: list[tuple[float, float]],
    ) -> None:
        """Apply an incremental diff (price -> new size; size 0 removes the level).

        Never re-fetches a full book: merges the diff into a plain-dict
        working copy of the current levels, then rebuilds sorted numpy
        arrays and atomically swaps them in.
        """
        prior = self._state
        bid_map = dict(zip(prior.bid_prices.tolist(), prior.bid_sizes.tolist(), strict=True))
        ask_map = dict(zip(prior.ask_prices.tolist(), prior.ask_sizes.tolist(), strict=True))

        for price, size in bid_updates:
            if size <= 0:
                bid_map.pop(price, None)
            else:
                bid_map[price] = size

        for price, size in ask_updates:
            if size <= 0:
                ask_map.pop(price, None)
            else:
                ask_map[price] = size

        bid_items = sorted(bid_map.items(), key=lambda kv: -kv[0])
        ask_items = sorted(ask_map.items(), key=lambda kv: kv[0])

        self._state = BookState(
            bid_prices=np.array([p for p, _ in bid_items], dtype=np.float64),
            bid_sizes=np.array([s for _, s in bid_items], dtype=np.float64),
            ask_prices=np.array([p for p, _ in ask_items], dtype=np.float64),
            ask_sizes=np.array([s for _, s in ask_items], dtype=np.float64),
            timestamp=time.time(),
        )


class BookStore:
    """Shared table of `OrderBook`s keyed by (venue_id, symbol).

    The feed loop is the sole writer; detection strategies are
    concurrent readers. No lock is required (see module docstring).
    """

    def __init__(self) -> None:
        self._books: dict[tuple[str, str], OrderBook] = {}

    def get_or_create(self, venue_id: str, symbol: str) -> OrderBook:
        key = (venue_id, symbol)
        book = self._books.get(key)
        if book is None:
            book = OrderBook(venue_id, symbol)
            self._books[key] = book
        return book

    def get(self, venue_id: str, symbol: str) -> OrderBook | None:
        return self._books.get((venue_id, symbol))

    def all_for_symbol(self, symbol: str) -> list[OrderBook]:
        return [book for (v, s), book in self._books.items() if s == symbol]

    def venues_for_symbol(self, symbol: str) -> list[str]:
        return [v for (v, s) in self._books if s == symbol]

    def all_symbols(self) -> set[str]:
        return {s for (_v, s) in self._books}

    def all_venues(self) -> set[str]:
        return {v for (v, _s) in self._books}
