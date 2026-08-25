"""Tests for strategies.statistical.StatisticalArbStrategy.

This strategy is explicitly NOT true arbitrage -- these tests check the
mechanics (entry/exit/stop-loss on z-score, cointegration gating), not
profitability. Synthetic series are built as a shared random-walk factor
plus a mean-reverting AR(1) residual, which is what a real cointegrated
pair's spread actually looks like -- a literal repeating oscillation (the
simplest "divergent" fixture one might reach for) is degenerate for both
OLS hedge-ratio fitting and the ADF test, so it's deliberately avoided.
"""

from __future__ import annotations

import numpy as np

from core.book import BookStore
from core.market_state import MarketState
from strategies.statistical import StatisticalArbStrategy


def _cointegrated_series(seed: int, n: int, base_a: float = 100.0, base_b: float = 50.0) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    common = np.cumsum(rng.normal(0, 0.3, n))
    residual = np.zeros(n)
    for i in range(1, n):
        residual[i] = 0.7 * residual[i - 1] + rng.normal(0, 0.3)
    return base_a + common + residual, base_b + common


def _feed(strategy: StatisticalArbStrategy, price_a: float, price_b: float) -> list:
    store = BookStore()
    store.get_or_create("x", "A/USDT").replace(bids=[(price_a - 0.01, 10)], asks=[(price_a + 0.01, 10)])
    store.get_or_create("x", "B/USDT").replace(bids=[(price_b - 0.01, 10)], asks=[(price_b + 0.01, 10)])
    market_state = MarketState(book_store=store, symbols=["A/USDT", "B/USDT"])
    return strategy.scan(market_state)


def test_entry_signal_on_extreme_zscore_with_cointegrated_series():
    lookback = 79
    series_a, series_b = _cointegrated_series(seed=7, n=lookback + 1)
    series_a[-1] += 2.0  # inject a divergence shock on the last tick

    strategy = StatisticalArbStrategy(pairs=[("A/USDT", "B/USDT")], lookback=lookback, entry_z=2.0, exit_z=0.3, stop_loss_z=6.0)

    signals = []
    for price_a, price_b in zip(series_a, series_b, strict=True):
        signals.extend(_feed(strategy, float(price_a), float(price_b)))

    assert any(s.detail["signal_type"] == "entry" for s in signals)


def test_stop_loss_exits_on_continued_divergence_after_entry():
    lookback = 79
    series_a, series_b = _cointegrated_series(seed=7, n=lookback + 1)
    series_a[-1] += 2.0

    strategy = StatisticalArbStrategy(pairs=[("A/USDT", "B/USDT")], lookback=lookback, entry_z=2.0, exit_z=0.3, stop_loss_z=6.0)

    signals = []
    for price_a, price_b in zip(series_a, series_b, strict=True):
        signals.extend(_feed(strategy, float(price_a), float(price_b)))
    assert any(s.detail["signal_type"] == "entry" for s in signals)

    # Keep diverging further -- should trip the stop-loss rather than revert.
    signals.extend(_feed(strategy, float(series_a[-1]) + 6.0, float(series_b[-1])))

    assert any(s.detail["signal_type"] == "stop_loss_exit" for s in signals)


def test_no_signal_without_enough_lookback_history():
    strategy = StatisticalArbStrategy(pairs=[("A/USDT", "B/USDT")], lookback=50)

    signals = _feed(strategy, 106.0, 50.0)

    assert signals == []


def test_not_cointegrated_series_does_not_enter_even_on_extreme_zscore():
    # Two independent random walks are not cointegrated; a large spread
    # move should still be gated out by the ADF check.
    rng = np.random.default_rng(3)
    n = 79
    series_a = 100.0 + np.cumsum(rng.normal(0, 1.0, n))
    series_b = 50.0 + np.cumsum(rng.normal(0, 1.0, n))

    strategy = StatisticalArbStrategy(pairs=[("A/USDT", "B/USDT")], lookback=n, entry_z=0.5, exit_z=0.1, stop_loss_z=10.0)

    signals = []
    for price_a, price_b in zip(series_a, series_b, strict=True):
        signals.extend(_feed(strategy, float(price_a), float(price_b)))

    assert not any(s.detail["signal_type"] == "entry" for s in signals)
