"""Tests for strategies.cross_exchange.CrossExchangeStrategy."""

from __future__ import annotations

from strategies.cross_exchange import CrossExchangeStrategy
from tests.conftest import make_market_state


def test_clean_profitable_spread_is_detected():
    market_state = make_market_state(
        {
            ("binance", "BTC/USDT"): {"bid": 100.0, "ask": 100.1},
            ("kraken", "BTC/USDT"): {"bid": 105.0, "ask": 105.1},
        }
    )
    strategy = CrossExchangeStrategy(min_profit_pct=0.5)

    opportunities = strategy.scan(market_state)

    assert len(opportunities) == 1
    opp = opportunities[0]
    assert opp.legs[0].venue_id == "binance" and opp.legs[0].side == "buy"
    assert opp.legs[1].venue_id == "kraken" and opp.legs[1].side == "sell"
    assert opp.net_profit_pct > 0.5


def test_profitable_gross_but_unprofitable_after_fees_is_rejected(monkeypatch):
    import strategies.cross_exchange as cross_exchange

    monkeypatch.setattr(cross_exchange, "taker_fee_for", lambda venue_id, fallback: 0.01)  # 1% each leg
    market_state = make_market_state(
        {
            ("binance", "BTC/USDT"): {"bid": 100.0, "ask": 100.05},
            ("kraken", "BTC/USDT"): {"bid": 100.10, "ask": 100.15},
        }
    )
    strategy = CrossExchangeStrategy(min_profit_pct=0.5)

    assert strategy.scan(market_state) == []


def test_stale_quotes_are_filtered_out():
    market_state = make_market_state(
        {
            ("binance", "BTC/USDT"): {"bid": 100.0, "ask": 100.1, "age_sec": 0.0},
            ("kraken", "BTC/USDT"): {"bid": 110.0, "ask": 110.1, "age_sec": 60.0},
        },
        staleness_sec=3.0,
    )
    strategy = CrossExchangeStrategy(min_profit_pct=0.1)

    assert strategy.scan(market_state) == []


def test_same_exchange_pairs_are_never_matched():
    # Two different symbols on the same venue should never be cross-matched;
    # simulate by only ever having one venue for the symbol.
    market_state = make_market_state({("binance", "BTC/USDT"): {"bid": 100.0, "ask": 100.1}})
    strategy = CrossExchangeStrategy(min_profit_pct=0.0)

    assert strategy.scan(market_state) == []


def test_size_bounded_by_smaller_top_of_book_volume():
    market_state = make_market_state(
        {
            ("binance", "BTC/USDT"): {"bid": 100.0, "ask": 100.1, "ask_size": 0.5},
            ("kraken", "BTC/USDT"): {"bid": 105.0, "ask": 105.1, "bid_size": 2.0},
        }
    )
    strategy = CrossExchangeStrategy(min_profit_pct=0.5, max_trade_usd=1_000_000.0)

    opportunities = strategy.scan(market_state)

    assert len(opportunities) == 1
    assert opportunities[0].max_size_usd == 0.5 * 100.1


def test_size_bounded_by_max_trade_usd():
    market_state = make_market_state(
        {
            ("binance", "BTC/USDT"): {"bid": 100.0, "ask": 100.1, "ask_size": 1000.0},
            ("kraken", "BTC/USDT"): {"bid": 105.0, "ask": 105.1, "bid_size": 1000.0},
        }
    )
    strategy = CrossExchangeStrategy(min_profit_pct=0.5, max_trade_usd=50.0)

    opportunities = strategy.scan(market_state)

    assert opportunities[0].max_size_usd == 50.0
