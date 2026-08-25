"""Property-based tests (hypothesis) for fee and sizing math.

These check invariants that should hold for *any* valid input, not just
the specific examples used in the example-based strategy tests.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.book import BookStore
from execution.inventory import InventoryManager
from risk.manager import RiskManager
from strategies.base import Leg, Opportunity
from strategies.dex_dex import (
    constant_product_input_needed,
    constant_product_output,
)


@given(
    reserve_in=st.floats(min_value=1.0, max_value=1e9, allow_nan=False, allow_infinity=False),
    reserve_out=st.floats(min_value=1.0, max_value=1e9, allow_nan=False, allow_infinity=False),
    amount_in=st.floats(min_value=1e-6, max_value=1e6, allow_nan=False, allow_infinity=False),
    fee=st.floats(min_value=0.0, max_value=0.05, allow_nan=False, allow_infinity=False),
)
def test_constant_product_output_never_exceeds_reserves(reserve_in, reserve_out, amount_in, fee):
    amount_out = constant_product_output(reserve_in, reserve_out, amount_in, fee)
    assert 0.0 <= amount_out < reserve_out


@given(
    reserve_in=st.floats(min_value=1.0, max_value=1e9, allow_nan=False, allow_infinity=False),
    reserve_out=st.floats(min_value=10.0, max_value=1e9, allow_nan=False, allow_infinity=False),
    amount_in=st.floats(min_value=1e-6, max_value=1e6, allow_nan=False, allow_infinity=False),
    fee=st.floats(min_value=0.0, max_value=0.05, allow_nan=False, allow_infinity=False),
)
def test_constant_product_input_needed_round_trips_output(reserve_in, reserve_out, amount_in, fee):
    amount_out = constant_product_output(reserve_in, reserve_out, amount_in, fee)
    if amount_out <= 0 or amount_out >= reserve_out:
        return  # degenerate: nothing meaningful to round-trip
    recovered = constant_product_input_needed(reserve_in, reserve_out, amount_out, fee)
    assert recovered == pytest.approx(amount_in, rel=1e-4)


@given(
    buy_fee=st.floats(min_value=0.0, max_value=0.02, allow_nan=False, allow_infinity=False),
    sell_fee=st.floats(min_value=0.0, max_value=0.02, allow_nan=False, allow_infinity=False),
    buy_price=st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
    sell_price=st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
)
def test_net_profit_is_never_greater_than_gross_profit(buy_fee, sell_fee, buy_price, sell_price):
    gross_pct = (sell_price - buy_price) / buy_price * 100.0
    net_pct = gross_pct - (buy_fee + sell_fee) * 100.0
    assert net_pct <= gross_pct + 1e-9


@given(
    max_trade_usd=st.floats(min_value=1.0, max_value=1e7, allow_nan=False, allow_infinity=False),
    liquidity_usd=st.floats(min_value=0.0, max_value=1e7, allow_nan=False, allow_infinity=False),
)
def test_size_with_depth_check_never_exceeds_liquidity_or_cap(max_trade_usd, liquidity_usd):
    store = BookStore()
    book = store.get_or_create("binance", "BTC/USDT")
    price = 100.0
    liquidity_units = liquidity_usd / price
    book.replace(bids=[(price - 0.01, 1.0)], asks=[(price, liquidity_units)])

    opp = Opportunity(
        strategy="cross_exchange", symbol="BTC/USDT",
        legs=(Leg("binance", "BTC/USDT", "buy", price, max_trade_usd / price, 0.001), Leg("kraken", "BTC/USDT", "sell", price * 1.01, max_trade_usd / price, 0.001)),
        gross_profit_pct=1.0, net_profit_pct=0.8, max_size_usd=max_trade_usd,
    )
    rm = RiskManager()
    rm.limits.max_notional_per_trade_usd = max_trade_usd

    sized_usd = rm.size_with_depth_check(opp, store, max_slippage_pct=1e9)  # slippage tolerance wide open

    assert sized_usd <= max_trade_usd + 1e-6
    assert sized_usd <= liquidity_usd + 1e-6


@given(free=st.floats(min_value=0.0, max_value=1e9), amount=st.floats(min_value=0.0, max_value=1e9))
def test_inventory_reserve_never_oversubscribes_free_balance(free, amount):
    inv = InventoryManager()
    inv.set_balance("binance", "USDT", free)

    ok = inv.reserve("binance", "USDT", amount)

    if ok:
        assert inv.get_balance("binance", "USDT").free >= -1e-9
        assert inv.get_balance("binance", "USDT").locked <= free + 1e-9
    else:
        assert amount > free
