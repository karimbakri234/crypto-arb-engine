"""Tests for execution.inventory.InventoryManager."""

from __future__ import annotations

from execution.inventory import InventoryManager


def test_reserve_and_release_round_trip():
    inv = InventoryManager()
    inv.set_balance("binance", "USDT", 1000.0)

    assert inv.reserve("binance", "USDT", 400.0) is True
    assert inv.get_balance("binance", "USDT").free == 600.0
    assert inv.get_balance("binance", "USDT").locked == 400.0

    inv.release("binance", "USDT", 400.0)
    assert inv.get_balance("binance", "USDT").free == 1000.0
    assert inv.get_balance("binance", "USDT").locked == 0.0


def test_reserve_fails_when_insufficient_free_balance():
    inv = InventoryManager()
    inv.set_balance("binance", "USDT", 100.0)

    assert inv.reserve("binance", "USDT", 400.0) is False
    assert inv.get_balance("binance", "USDT").free == 100.0


def test_imbalance_report_flags_lopsided_venue():
    inv = InventoryManager()
    inv.set_balance("binance", "BTC", 10.0)
    inv.set_balance("kraken", "BTC", 0.01)

    signals = inv.imbalance_report("BTC", venues=["binance", "kraken"])

    assert len(signals) == 1
    assert signals[0].underfunded_venue == "kraken"
    assert signals[0].suggested_source_venue == "binance"


def test_imbalance_report_empty_when_balanced():
    inv = InventoryManager()
    inv.set_balance("binance", "BTC", 5.0)
    inv.set_balance("kraken", "BTC", 5.0)

    assert inv.imbalance_report("BTC", venues=["binance", "kraken"]) == []


def test_estimate_transfer_uses_venue_withdrawal_fee():
    estimate = InventoryManager.estimate_transfer("binance", "kraken", "BTC")
    assert estimate.fee_units == 0.0002  # from config.venues CEX_VENUES["binance"]
    assert estimate.latency_sec > 0
