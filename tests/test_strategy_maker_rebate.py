"""Tests for strategies.maker_rebate.MakerRebateStrategy."""

from __future__ import annotations

from strategies.maker_rebate import MakerRebateStrategy
from tests.conftest import make_market_state


def test_maker_rebate_clears_a_bar_taker_taker_cannot(monkeypatch):
    import strategies.maker_rebate as maker_rebate

    # binance: 0.1% taker / 0.05% maker. woo: 0.1% taker / -0.05% rebate maker.
    def fake_taker(venue_id, fallback):
        return 0.001

    def fake_maker(venue_id, fallback):
        return -0.0005 if venue_id == "woo" else 0.0005

    monkeypatch.setattr(maker_rebate, "taker_fee_for", fake_taker)
    monkeypatch.setattr(maker_rebate, "maker_fee_for", fake_maker)

    # Gross spread of 0.15%: taker-taker net = 0.15 - 0.2 = -0.05% (loses).
    # maker(binance, 0.05%)-taker(woo, 0.1%) net = 0.15 - 0.15 = 0% (still not enough)
    # taker(binance,0.1%)-maker(woo,-0.05%) net = 0.15 - 0.05 = 0.10% (clears a low bar).
    market_state = make_market_state(
        {
            ("binance", "BTC/USDT"): {"bid": 100.0, "ask": 100.0},
            ("woo", "BTC/USDT"): {"bid": 100.15, "ask": 100.15},
        }
    )
    strategy = MakerRebateStrategy(min_profit_pct=0.08)

    opportunities = strategy.scan(market_state)

    assert len(opportunities) == 1
    assert opportunities[0].detail["fill_path"] in ("taker_maker", "maker_taker", "maker_maker")
    assert opportunities[0].detail["taker_taker_pct"] < strategy.min_profit_pct


def test_when_taker_taker_already_clears_bar_it_is_not_flagged_here():
    # A large spread is already profitable taker-taker -- not this
    # strategy's job to report it (that's cross_exchange.py).
    market_state = make_market_state(
        {
            ("binance", "BTC/USDT"): {"bid": 100.0, "ask": 100.0},
            ("kraken", "BTC/USDT"): {"bid": 110.0, "ask": 110.0},
        }
    )
    strategy = MakerRebateStrategy(min_profit_pct=0.05)

    assert strategy.scan(market_state) == []
