"""Tests for risk.manager.RiskManager: every circuit breaker halts trading."""

from __future__ import annotations

from core.book import BookStore
from risk.limits import RiskLimits
from risk.manager import RiskManager
from strategies.base import Leg, Opportunity


def _opportunity(strategy: str = "cross_exchange", size_usd: float = 100.0) -> Opportunity:
    return Opportunity(
        strategy=strategy,
        symbol="BTC/USDT",
        legs=(Leg("binance", "BTC/USDT", "buy", 100.0, 1.0, 0.001), Leg("kraken", "BTC/USDT", "sell", 102.0, 1.0, 0.001)),
        gross_profit_pct=2.0,
        net_profit_pct=1.8,
        max_size_usd=size_usd,
    )


def test_daily_loss_limit_halts_trading():
    rm = RiskManager(RiskLimits(daily_loss_limit_usd=50.0))
    opp = _opportunity()
    assert rm.can_trade(opp) is True

    rm.record_result(opp, pnl_usd=-60.0, success=True)

    assert rm.can_trade(opp) is False


def test_max_trades_per_day_circuit_breaker_halts_trading():
    rm = RiskManager(RiskLimits(max_trades_per_day=3, default_strategy_capital_usd=1e9, default_venue_exposure_usd=1e9))
    opp = _opportunity()

    for _ in range(3):
        rm.record_result(opp, pnl_usd=1.0, success=True)

    assert rm.can_trade(opp) is False


def test_max_consecutive_failures_kill_switch_halts_trading():
    rm = RiskManager(RiskLimits(max_consecutive_failures=2, default_strategy_capital_usd=1e9, default_venue_exposure_usd=1e9))
    opp = _opportunity()

    rm.record_result(opp, pnl_usd=0.0, success=False)
    rm.record_result(opp, pnl_usd=0.0, success=False)

    assert rm.can_trade(opp) is False


def test_consecutive_failures_reset_on_success():
    rm = RiskManager(RiskLimits(max_consecutive_failures=2, default_strategy_capital_usd=1e9, default_venue_exposure_usd=1e9))
    opp = _opportunity()

    rm.record_result(opp, pnl_usd=0.0, success=False)
    rm.record_result(opp, pnl_usd=1.0, success=True)

    assert rm.consecutive_failures == 0
    assert rm.can_trade(opp) is True


def test_emergency_stop_halts_trading():
    rm = RiskManager()
    opp = _opportunity()
    assert rm.can_trade(opp) is True

    rm.trigger_emergency_stop("manual test halt")
    assert rm.can_trade(opp) is False

    rm.clear_emergency_stop()
    assert rm.can_trade(opp) is True


def test_strategy_capital_cap_blocks_oversized_deployment():
    rm = RiskManager(RiskLimits(strategy_capital_usd={"cross_exchange": 150.0}, default_venue_exposure_usd=1e9))
    small_opp = _opportunity(size_usd=100.0)
    assert rm.can_trade(small_opp) is True
    rm.record_result(small_opp, pnl_usd=1.0, success=True)

    # Another 100 USD would push total deployed to 200 > 150 cap.
    assert rm.can_trade(_opportunity(size_usd=100.0)) is False


def test_venue_exposure_cap_blocks_oversized_deployment():
    rm = RiskManager(RiskLimits(venue_exposure_usd={"binance": 150.0}, default_strategy_capital_usd=1e9))
    opp = _opportunity(size_usd=100.0)
    assert rm.can_trade(opp) is True
    rm.record_result(opp, pnl_usd=1.0, success=True)

    assert rm.can_trade(_opportunity(size_usd=100.0)) is False


def test_size_with_depth_check_rejects_excessive_slippage():
    rm = RiskManager()
    store = BookStore()
    book = store.get_or_create("binance", "BTC/USDT")
    # Thin book: only 0.1 units at the top before the price jumps a lot.
    book.replace(bids=[(99.9, 1.0)], asks=[(100.0, 0.1), (110.0, 10.0)])
    opp = _opportunity(size_usd=1000.0)  # would need ~10 units at price 100

    size_usd = rm.size_with_depth_check(opp, store, max_slippage_pct=0.5)

    assert size_usd == 0.0


def test_size_with_depth_check_accepts_within_slippage_tolerance():
    rm = RiskManager()
    store = BookStore()
    book = store.get_or_create("binance", "BTC/USDT")
    book.replace(bids=[(99.9, 5.0)], asks=[(100.0, 5.0)])
    opp = _opportunity(size_usd=100.0)

    size_usd = rm.size_with_depth_check(opp, store, max_slippage_pct=1.0)

    assert size_usd > 0.0


def test_reverify_profitability_confirms_edge_still_there():
    rm = RiskManager()
    store = BookStore()
    store.get_or_create("binance", "BTC/USDT").replace(bids=[(99.9, 5.0)], asks=[(100.0, 5.0)])
    store.get_or_create("kraken", "BTC/USDT").replace(bids=[(102.0, 5.0)], asks=[(102.1, 5.0)])
    opp = _opportunity(size_usd=100.0)

    still_profitable, net_profit_pct = rm.reverify_profitability(opp, store, size_usd=100.0)

    assert still_profitable is True
    assert net_profit_pct > 0.0


def test_reverify_profitability_rejects_edge_that_vanished():
    rm = RiskManager()
    store = BookStore()
    # Same book on both venues now (post-detection, the spread closed).
    store.get_or_create("binance", "BTC/USDT").replace(bids=[(99.9, 5.0)], asks=[(100.0, 5.0)])
    store.get_or_create("kraken", "BTC/USDT").replace(bids=[(99.9, 5.0)], asks=[(100.0, 5.0)])
    opp = _opportunity(size_usd=100.0)

    still_profitable, net_profit_pct = rm.reverify_profitability(opp, store, size_usd=100.0)

    assert still_profitable is False
    assert net_profit_pct <= 0.0


def test_reverify_profitability_missing_book_falls_back_to_detection_price():
    rm = RiskManager()
    store = BookStore()  # no books at all
    opp = _opportunity(size_usd=100.0)  # detection-time legs: buy@100, sell@102, 0.1% fees each

    still_profitable, net_profit_pct = rm.reverify_profitability(opp, store, size_usd=100.0)

    assert still_profitable is True
    assert net_profit_pct > 0.0
