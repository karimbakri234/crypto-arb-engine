"""Tests for strategies.cross_quote.CrossQuoteStrategy."""

from __future__ import annotations

from strategies.cross_quote import CrossQuoteStrategy
from tests.conftest import make_market_state


def test_divergent_cross_quote_is_detected():
    market_state = make_market_state(
        {
            ("binance", "BTC/USDT"): {"bid": 50_000.0, "ask": 50_010.0},
            ("binance", "BTC/USDC"): {"bid": 50_300.0, "ask": 50_310.0},
        }
    )
    strategy = CrossQuoteStrategy(min_profit_pct=0.1)

    opportunities = strategy.scan(market_state)

    assert len(opportunities) >= 1
    assert opportunities[0].requires_prefunded_inventory is False


def test_matching_cross_quote_prices_yield_no_opportunity():
    market_state = make_market_state(
        {
            ("binance", "BTC/USDT"): {"bid": 50_000.0, "ask": 50_010.0},
            ("binance", "BTC/USDC"): {"bid": 50_000.5, "ask": 50_010.5},
        }
    )
    strategy = CrossQuoteStrategy(min_profit_pct=0.1)

    assert strategy.scan(market_state) == []


def test_non_stablecoin_quote_is_ignored():
    market_state = make_market_state(
        {
            ("binance", "BTC/USDT"): {"bid": 50_000.0, "ask": 50_010.0},
            ("binance", "BTC/ETH"): {"bid": 16.0, "ask": 16.01},
        }
    )
    strategy = CrossQuoteStrategy(min_profit_pct=0.01)

    assert strategy.scan(market_state) == []
