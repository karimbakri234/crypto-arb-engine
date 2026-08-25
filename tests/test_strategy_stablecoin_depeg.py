"""Tests for strategies.stablecoin_depeg.StablecoinDepegStrategy."""

from __future__ import annotations

from core.market_state import MarketState, PoolState
from strategies.stablecoin_depeg import StablecoinDepegStrategy
from tests.conftest import make_book_store


def test_moderate_depeg_is_detected():
    market_state = MarketState(
        book_store=make_book_store({("binance", "USDC/USDT"): {"bid": 0.997, "ask": 0.998}}),
        symbols=["USDC/USDT"],
    )
    strategy = StablecoinDepegStrategy(depeg_threshold_pct=0.1, kill_switch_pct=3.0)

    opportunities = strategy.scan(market_state)

    assert len(opportunities) == 1
    assert opportunities[0].legs[0].side == "buy"  # below peg -> buy
    assert strategy.triggered_kill_switches == set()


def test_severe_depeg_triggers_kill_switch_instead_of_a_trade():
    market_state = MarketState(
        book_store=make_book_store({("binance", "USDC/USDT"): {"bid": 0.90, "ask": 0.91}}),
        symbols=["USDC/USDT"],
    )
    strategy = StablecoinDepegStrategy(depeg_threshold_pct=0.1, kill_switch_pct=3.0)

    opportunities = strategy.scan(market_state)

    assert opportunities == []
    assert "USDC/USDT" in strategy.triggered_kill_switches


def test_tiny_deviation_below_threshold_is_ignored():
    market_state = MarketState(
        book_store=make_book_store({("binance", "USDC/USDT"): {"bid": 0.9997, "ask": 0.9999}}),
        symbols=["USDC/USDT"],
    )
    strategy = StablecoinDepegStrategy(depeg_threshold_pct=0.1, kill_switch_pct=3.0)

    assert strategy.scan(market_state) == []


def test_dex_stable_pool_depeg_is_detected():
    market_state = MarketState(book_store=make_book_store({}), symbols=[])
    market_state.dex_pools["curve:usdc_usdt"] = PoolState(
        dex_id="curve_eth", chain="ethereum", symbol="USDC/USDT", reserve_base=1_000_000, reserve_quote=990_000, fee=0.0004
    )
    strategy = StablecoinDepegStrategy(depeg_threshold_pct=0.1, kill_switch_pct=3.0)

    opportunities = strategy.scan(market_state)

    assert len(opportunities) == 1
    assert opportunities[0].detail["source"] == "dex"
