"""Tests for strategies.dex_dex: AMM math and same/cross-chain detection."""

from __future__ import annotations

from core.book import BookStore
from core.market_state import MarketState, PoolState
from strategies.dex_dex import (
    DexDexStrategy,
    concentrated_liquidity_output,
    constant_product_input_needed,
    constant_product_output,
)


def test_constant_product_output_matches_xy_k():
    reserve_in, reserve_out = 1_000.0, 2_000_000.0
    amount_out = constant_product_output(reserve_in, reserve_out, amount_in=10.0, fee=0.0)
    # No fee: (reserve_in + amount_in) * (reserve_out - amount_out) == reserve_in * reserve_out
    assert abs((reserve_in + 10.0) * (reserve_out - amount_out) - reserve_in * reserve_out) < 1e-6


def test_constant_product_input_needed_is_inverse_of_output():
    reserve_in, reserve_out = 1_000.0, 2_000_000.0
    amount_out = constant_product_output(reserve_in, reserve_out, amount_in=10.0, fee=0.003)
    recovered_input = constant_product_input_needed(reserve_in, reserve_out, amount_out, fee=0.003)
    assert abs(recovered_input - 10.0) < 1e-6


def test_constant_product_buy_side_slippage_increases_with_size():
    reserve_in, reserve_out = 1_000_000.0, 1_000.0
    small = constant_product_input_needed(reserve_in, reserve_out, amount_out=1.0, fee=0.003) / 1.0
    large = constant_product_input_needed(reserve_in, reserve_out, amount_out=400.0, fee=0.003) / 400.0
    assert large > small  # buying more costs a worse average price


def test_concentrated_liquidity_output_is_positive_and_bounded():
    out = concentrated_liquidity_output(sqrt_price_x96=(2**96), liquidity=1_000_000.0, amount_in=100.0, fee=0.0005, zero_for_one=True)
    assert 0.0 < out < 100.0  # price is 1.0, so output is close to but below input (nonzero fee/impact)


def test_dex_dex_same_chain_arbitrage_detected():
    market_state = MarketState(book_store=BookStore(), symbols=[])
    market_state.dex_pools["a"] = PoolState("uniswap_v3_eth", "ethereum", "ETH/USDT", reserve_base=1_000, reserve_quote=3_000_000, fee=0.003)
    market_state.dex_pools["b"] = PoolState("sushiswap_eth", "ethereum", "ETH/USDT", reserve_base=1_000, reserve_quote=3_100_000, fee=0.003)

    strategy = DexDexStrategy(min_profit_pct=0.1, probe_size_base=1.0)
    opportunities = strategy.scan(market_state)

    assert len(opportunities) == 1
    assert opportunities[0].is_atomic is True
    assert opportunities[0].requires_prefunded_inventory is False


def test_dex_dex_cross_chain_is_not_atomic_and_prices_bridge_cost():
    market_state = MarketState(book_store=BookStore(), symbols=[])
    market_state.dex_pools["a"] = PoolState("uniswap_v3_eth", "ethereum", "ETH/USDT", reserve_base=1_000, reserve_quote=3_000_000, fee=0.003)
    market_state.dex_pools["b"] = PoolState("camelot_arb", "arbitrum", "ETH/USDT", reserve_base=1_000, reserve_quote=3_100_000, fee=0.003)

    strategy = DexDexStrategy(min_profit_pct=0.1, probe_size_base=1.0)
    opportunities = strategy.scan(market_state)

    assert len(opportunities) == 1
    assert opportunities[0].is_atomic is False
    assert opportunities[0].requires_prefunded_inventory is True
    assert "bridge_latency_sec" in opportunities[0].detail


def test_dex_dex_below_threshold_is_rejected():
    market_state = MarketState(book_store=BookStore(), symbols=[])
    market_state.dex_pools["a"] = PoolState("uniswap_v3_eth", "ethereum", "ETH/USDT", reserve_base=1_000, reserve_quote=3_000_000, fee=0.003)
    market_state.dex_pools["b"] = PoolState("sushiswap_eth", "ethereum", "ETH/USDT", reserve_base=1_000, reserve_quote=3_000_100, fee=0.003)

    strategy = DexDexStrategy(min_profit_pct=0.5, probe_size_base=1.0)
    assert strategy.scan(market_state) == []
