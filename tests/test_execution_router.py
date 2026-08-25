"""Tests for execution.router.Router: best-EV selection and no double-commit."""

from __future__ import annotations

from execution.inventory import InventoryManager
from execution.router import Router
from strategies.base import Leg, Opportunity


def _opp(strategy: str, buy_venue: str, sell_venue: str, net_pct: float, size_usd: float) -> Opportunity:
    return Opportunity(
        strategy=strategy,
        symbol="BTC/USDT",
        legs=(Leg(buy_venue, "BTC/USDT", "buy", 100.0, size_usd / 100.0, 0.001), Leg(sell_venue, "BTC/USDT", "sell", 105.0, size_usd / 100.0, 0.001)),
        gross_profit_pct=net_pct + 0.2,
        net_profit_pct=net_pct,
        max_size_usd=size_usd,
    )


def test_selects_opportunity_when_inventory_available():
    inv = InventoryManager()
    inv.set_balance("binance", "USDT", 1000.0)
    inv.set_balance("kraken", "BTC", 100.0)
    router = Router(inv)

    selected = router.select([_opp("cross_exchange", "binance", "kraken", 4.8, 500.0)])

    assert len(selected) == 1


def test_rejects_opportunity_without_enough_inventory():
    inv = InventoryManager()  # no balances set anywhere
    router = Router(inv)

    selected = router.select([_opp("cross_exchange", "binance", "kraken", 4.8, 500.0)])

    assert selected == []


def test_does_not_double_commit_same_inventory_to_two_opportunities():
    inv = InventoryManager()
    inv.set_balance("binance", "USDT", 600.0)  # enough for only one 500 USD trade
    inv.set_balance("kraken", "BTC", 100.0)
    router = Router(inv)

    opp_high = _opp("triangular", "binance", "kraken", 5.0, 500.0)
    opp_low = _opp("cross_exchange", "binance", "kraken", 1.0, 500.0)

    selected = router.select([opp_low, opp_high])

    assert len(selected) == 1
    assert selected[0].strategy == "triangular"  # higher expected value wins the shared inventory
