"""Tests for strategies.wrapped_asset.WrappedAssetStrategy."""

from __future__ import annotations

from strategies.wrapped_asset import REDEMPTION_INFO, WrappedAssetStrategy, fair_deviation_band_pct
from tests.conftest import make_market_state


def test_instant_redemption_has_a_tighter_band_than_queued():
    instant = fair_deviation_band_pct(REDEMPTION_INFO["WETH"])
    queued = fair_deviation_band_pct(REDEMPTION_INFO["STETH"])
    assert instant < queued


def test_large_deviation_beyond_band_is_detected():
    market_state = make_market_state(
        {
            ("binance", "WETH/USDT"): {"bid": 3_200.0, "ask": 3_201.0},
            ("binance", "ETH/USDT"): {"bid": 3_000.0, "ask": 3_001.0},
        }
    )
    strategy = WrappedAssetStrategy()

    opportunities = strategy.scan(market_state)

    assert len(opportunities) >= 1
    assert opportunities[0].legs[0].side == "sell"  # wrapped trades rich -> sell it


def test_deviation_within_fair_band_is_not_flagged():
    market_state = make_market_state(
        {
            ("binance", "WETH/USDT"): {"bid": 3_001.0, "ask": 3_002.0},
            ("binance", "ETH/USDT"): {"bid": 3_000.0, "ask": 3_001.0},
        }
    )
    strategy = WrappedAssetStrategy()

    assert strategy.scan(market_state) == []
