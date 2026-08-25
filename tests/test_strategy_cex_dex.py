"""Tests for strategies.cex_dex.CexDexStrategy."""

from __future__ import annotations

from core.market_state import MarketState, PoolState
from strategies.cex_dex import CexDexStrategy
from tests.conftest import make_book_store


def _market_state(gas_usd: float) -> MarketState:
    # Pool spot price = 3,000,000 / 1,000 = 3000; CEX bid at 3060 is a clean
    # 2% gap, comfortably above the pool's 0.3% swap fee at small size.
    store = make_book_store({("binance", "ETH/USDT"): {"bid": 3_060.0, "ask": 3_061.0}})
    market_state = MarketState(book_store=store, symbols=["ETH/USDT"])
    market_state.dex_pools["uniswap_v3_eth:ETH/USDT"] = PoolState(
        dex_id="uniswap_v3_eth", chain="ethereum", symbol="ETH/USDT",
        reserve_base=1_000.0, reserve_quote=3_000_000.0, fee=0.003,
    )
    market_state.gas_price_usd["ethereum"] = gas_usd
    return market_state


def test_marginal_opportunity_is_profitable_with_low_gas():
    strategy = CexDexStrategy(min_profit_pct=0.01, probe_size_base=1.0)
    market_state = _market_state(gas_usd=0.01)

    opportunities = strategy.scan(market_state)

    assert any(o.detail["direction"] == "dex_to_cex" for o in opportunities)


def test_gas_and_price_impact_flip_marginal_opportunity_negative():
    strategy = CexDexStrategy(min_profit_pct=0.01, probe_size_base=1.0)

    cheap_gas_state = _market_state(gas_usd=0.01)
    expensive_gas_state = _market_state(gas_usd=500.0)  # dwarfs the spread on a 1-ETH trade

    assert any(o.detail["direction"] == "dex_to_cex" for o in strategy.scan(cheap_gas_state))
    assert not any(o.detail["direction"] == "dex_to_cex" for o in strategy.scan(expensive_gas_state))


def test_large_probe_size_price_impact_erodes_edge():
    # A much larger probe size against the same finite pool reserves walks
    # the AMM curve further, degrading the effective DEX price.
    small = CexDexStrategy(min_profit_pct=0.01, probe_size_base=1.0)
    large = CexDexStrategy(min_profit_pct=0.01, probe_size_base=400.0)  # 40% of pool reserves
    market_state = _market_state(gas_usd=0.01)

    small_opps = [o for o in small.scan(market_state) if o.detail["direction"] == "dex_to_cex"]
    large_opps = [o for o in large.scan(market_state) if o.detail["direction"] == "dex_to_cex"]

    assert small_opps and small_opps[0].net_profit_pct > 0
    assert not large_opps or large_opps[0].net_profit_pct < small_opps[0].net_profit_pct
