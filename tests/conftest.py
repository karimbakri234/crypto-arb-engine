"""Shared test helpers. No live network calls anywhere in this suite."""

from __future__ import annotations

import time

from core.book import BookStore
from core.market_state import MarketState


def make_book_store(entries: dict[tuple[str, str], dict]) -> BookStore:
    """Build a BookStore from `{(venue, symbol): {"bid":, "ask":, "bid_size":, "ask_size":, "age_sec":}}`."""
    store = BookStore()
    for (venue_id, symbol), spec in entries.items():
        book = store.get_or_create(venue_id, symbol)
        bid = spec["bid"]
        ask = spec["ask"]
        bid_size = spec.get("bid_size", 1.0)
        ask_size = spec.get("ask_size", 1.0)
        book.replace(bids=[(bid, bid_size)], asks=[(ask, ask_size)])
        age_sec = spec.get("age_sec", 0.0)
        if age_sec:
            # Backdate the snapshot's timestamp to simulate a stale quote.
            state = book.snapshot()
            object.__setattr__(state, "timestamp", time.time() - age_sec)
    return store


def make_market_state(entries: dict[tuple[str, str], dict], symbols: list[str] | None = None, staleness_sec: float = 3.0) -> MarketState:
    store = make_book_store(entries)
    if symbols is None:
        symbols = sorted({symbol for (_venue, symbol) in entries})
    return MarketState(book_store=store, symbols=symbols, staleness_sec=staleness_sec)
