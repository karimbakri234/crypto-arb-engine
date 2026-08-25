"""Tests for strategies.calendar_spread.CalendarSpreadStrategy."""

from __future__ import annotations

import time

from core.book import BookStore
from core.market_state import MarketState
from core.types import FuturesQuote
from strategies.calendar_spread import CalendarSpreadStrategy, marginal_forward_rate_pct


def test_marginal_forward_rate_positive_for_upward_sloping_curve():
    now = time.time()
    near = FuturesQuote("x", "BTC/USDT", now + 30 * 86400, price=51_000.0, spot_price=50_000.0)
    far = FuturesQuote("x", "BTC/USDT", now + 90 * 86400, price=53_000.0, spot_price=50_000.0)

    assert marginal_forward_rate_pct(near, far) > 0


def test_outlier_expiry_is_flagged_as_calendar_spread():
    now = time.time()
    market_state = MarketState(book_store=BookStore(), symbols=[])
    # A smooth curve at ~30, 60 days, then a sharp jump at 180 days.
    market_state.futures_quotes[("binance", "BTC/USDT", now + 30 * 86400)] = FuturesQuote("binance", "BTC/USDT", now + 30 * 86400, price=50_500.0, spot_price=50_000.0)
    market_state.futures_quotes[("binance", "BTC/USDT", now + 60 * 86400)] = FuturesQuote("binance", "BTC/USDT", now + 60 * 86400, price=51_000.0, spot_price=50_000.0)
    market_state.futures_quotes[("binance", "BTC/USDT", now + 180 * 86400)] = FuturesQuote("binance", "BTC/USDT", now + 180 * 86400, price=60_000.0, spot_price=50_000.0)

    strategy = CalendarSpreadStrategy(min_divergence_pct=1.0)
    opportunities = strategy.scan(market_state)

    assert len(opportunities) >= 1


def test_flat_curve_yields_no_opportunities():
    now = time.time()
    market_state = MarketState(book_store=BookStore(), symbols=[])
    for days, price in ((30, 50_500.0), (60, 51_000.0), (90, 51_500.0)):
        market_state.futures_quotes[("binance", "BTC/USDT", now + days * 86400)] = FuturesQuote(
            "binance", "BTC/USDT", now + days * 86400, price=price, spot_price=50_000.0
        )

    strategy = CalendarSpreadStrategy(min_divergence_pct=1.0)
    assert strategy.scan(market_state) == []
