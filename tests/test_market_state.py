"""Tests for core.market_state.MarketState's per-tick matrix cache.

Several strategies (cross_exchange, latency_arb, maker_rebate, perp_perp)
call `matrix_for_symbol` for the same symbol against the same `MarketState`
instance within one detection tick. Caching per instance means that work
happens once per symbol per tick instead of once per (symbol, strategy).
"""

from __future__ import annotations

from core.book import BookStore
from core.market_state import MarketState


def test_matrix_for_symbol_is_cached_within_one_market_state():
    store = BookStore()
    store.get_or_create("binance", "BTC/USDT").replace(bids=[(99.0, 1.0)], asks=[(100.0, 1.0)])
    market_state = MarketState(book_store=store, symbols=["BTC/USDT"])

    first = market_state.matrix_for_symbol("BTC/USDT")
    second = market_state.matrix_for_symbol("BTC/USDT")

    assert first is second


def test_matrix_cache_does_not_leak_across_market_state_instances():
    store = BookStore()
    store.get_or_create("binance", "BTC/USDT").replace(bids=[(99.0, 1.0)], asks=[(100.0, 1.0)])

    first_tick = MarketState(book_store=store, symbols=["BTC/USDT"])
    first_tick.matrix_for_symbol("BTC/USDT")

    # A new book update happens between ticks (a new MarketState per tick).
    store.get_or_create("binance", "BTC/USDT").replace(bids=[(199.0, 1.0)], asks=[(200.0, 1.0)])
    second_tick = MarketState(book_store=store, symbols=["BTC/USDT"])

    matrix = second_tick.matrix_for_symbol("BTC/USDT")

    assert matrix.ask_prices[0] == 200.0
