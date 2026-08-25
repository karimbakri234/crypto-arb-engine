"""Tests for strategies.basis_carry.BasisCarryStrategy."""

from __future__ import annotations

import time

from core.book import BookStore
from core.market_state import MarketState
from core.types import FuturesQuote
from strategies.basis_carry import BasisCarryStrategy


def test_contango_buys_spot_and_shorts_future():
    market_state = MarketState(book_store=BookStore(), symbols=[])
    market_state.futures_quotes[("binance", "BTC/USDT", time.time() + 30 * 86400)] = FuturesQuote(
        "binance", "BTC/USDT", time.time() + 30 * 86400, price=51_000.0, spot_price=50_000.0
    )
    strategy = BasisCarryStrategy(min_annualized_pct=1.0)

    opportunities = strategy.scan(market_state)

    assert len(opportunities) == 1
    spot_leg, future_leg = opportunities[0].legs
    assert spot_leg.side == "buy"
    assert future_leg.side == "sell"


def test_backwardation_reverses_direction():
    market_state = MarketState(book_store=BookStore(), symbols=[])
    market_state.futures_quotes[("binance", "BTC/USDT", time.time() + 30 * 86400)] = FuturesQuote(
        "binance", "BTC/USDT", time.time() + 30 * 86400, price=49_000.0, spot_price=50_000.0
    )
    strategy = BasisCarryStrategy(min_annualized_pct=1.0)

    opportunities = strategy.scan(market_state)

    assert len(opportunities) == 1
    spot_leg, future_leg = opportunities[0].legs
    assert spot_leg.side == "sell"
    assert future_leg.side == "buy"


def test_tiny_basis_below_threshold_is_rejected():
    market_state = MarketState(book_store=BookStore(), symbols=[])
    market_state.futures_quotes[("binance", "BTC/USDT", time.time() + 30 * 86400)] = FuturesQuote(
        "binance", "BTC/USDT", time.time() + 30 * 86400, price=50_005.0, spot_price=50_000.0
    )
    strategy = BasisCarryStrategy(min_annualized_pct=5.0)

    assert strategy.scan(market_state) == []
