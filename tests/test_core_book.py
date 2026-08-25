"""Tests for core.book: order book replace/diff and VWAP fill pricing."""

from __future__ import annotations

from core.book import BookStore


def test_replace_sorts_bids_desc_and_asks_asc():
    store = BookStore()
    book = store.get_or_create("binance", "BTC/USDT")
    book.replace(bids=[(99.0, 1.0), (100.0, 2.0)], asks=[(101.0, 1.0), (100.5, 2.0)])

    state = book.snapshot()
    assert state.best_bid == 100.0
    assert state.best_ask == 100.5


def test_apply_diff_updates_and_removes_levels():
    store = BookStore()
    book = store.get_or_create("binance", "BTC/USDT")
    book.replace(bids=[(100.0, 1.0)], asks=[(100.5, 1.0)])

    book.apply_diff(bid_updates=[(100.0, 0.0), (99.5, 3.0)], ask_updates=[(100.5, 5.0)])
    state = book.snapshot()

    assert state.best_bid == 99.5
    assert state.best_bid_size == 3.0
    assert state.best_ask_size == 5.0


def test_vwap_fill_price_walks_the_book():
    store = BookStore()
    book = store.get_or_create("binance", "BTC/USDT")
    book.replace(bids=[], asks=[(100.0, 1.0), (101.0, 1.0)])

    vwap, filled = book.snapshot().vwap_fill_price("buy", size=1.5)

    assert filled == 1.5
    assert vwap == (100.0 * 1.0 + 101.0 * 0.5) / 1.5


def test_vwap_fill_price_partial_when_insufficient_depth():
    store = BookStore()
    book = store.get_or_create("binance", "BTC/USDT")
    book.replace(bids=[], asks=[(100.0, 1.0)])

    vwap, filled = book.snapshot().vwap_fill_price("buy", size=5.0)

    assert filled == 1.0
    assert vwap == 100.0


def test_book_store_lookup_helpers():
    store = BookStore()
    store.get_or_create("binance", "BTC/USDT")
    store.get_or_create("kraken", "BTC/USDT")
    store.get_or_create("binance", "ETH/USDT")

    assert set(store.venues_for_symbol("BTC/USDT")) == {"binance", "kraken"}
    assert store.all_symbols() == {"BTC/USDT", "ETH/USDT"}
    assert store.all_venues() == {"binance", "kraken"}
    assert store.get("nonexistent", "BTC/USDT") is None
