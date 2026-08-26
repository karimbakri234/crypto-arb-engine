"""Tests for execution.executor.Executor's as-of-now profitability gate.

`Opportunity.net_profit_pct` is stamped at detection time. These tests
confirm the executor refuses to record a paper trade (and would refuse to
fire a live one) when the book has moved between detection and execution
such that the edge is no longer actually there -- see
`RiskManager.reverify_profitability`.
"""

from __future__ import annotations

import pytest

from core.book import BookStore
from core.types import Mode
from execution.executor import Executor
from execution.reconciler import Reconciler
from risk.manager import RiskManager
from strategies.base import Leg, Opportunity


def _opportunity(size_usd: float = 100.0) -> Opportunity:
    return Opportunity(
        strategy="cross_exchange",
        symbol="BTC/USDT",
        legs=(
            Leg("binance", "BTC/USDT", "buy", 100.0, 1.0, 0.001),
            Leg("kraken", "BTC/USDT", "sell", 102.0, 1.0, 0.001),
        ),
        gross_profit_pct=2.0,
        net_profit_pct=1.8,
        max_size_usd=size_usd,
    )


def _executor(mode: str) -> tuple[Executor, BookStore]:
    store = BookStore()
    executor = Executor(
        rest_manager=None,
        risk_manager=RiskManager(),
        reconciler=Reconciler(),
        book_store=store,
        mode=mode,
    )
    return executor, store


@pytest.mark.asyncio
async def test_paper_trade_executes_when_edge_still_there():
    executor, store = _executor(Mode.PAPER.value)
    store.get_or_create("binance", "BTC/USDT").replace(bids=[(99.9, 5.0)], asks=[(100.0, 5.0)])
    store.get_or_create("kraken", "BTC/USDT").replace(bids=[(102.0, 5.0)], asks=[(102.1, 5.0)])

    await executor.handle(_opportunity())

    assert len(executor.trade_log) == 1
    assert executor.trade_log[0].pnl_usd > 0.0


@pytest.mark.asyncio
async def test_paper_trade_skipped_when_edge_vanished_before_execution():
    executor, store = _executor(Mode.PAPER.value)
    # Both venues now quote the same book -- the spread that justified
    # detection has closed by the time execution would happen.
    store.get_or_create("binance", "BTC/USDT").replace(bids=[(99.9, 5.0)], asks=[(100.0, 5.0)])
    store.get_or_create("kraken", "BTC/USDT").replace(bids=[(99.9, 5.0)], asks=[(100.0, 5.0)])

    await executor.handle(_opportunity())

    assert executor.trade_log == []


@pytest.mark.asyncio
async def test_live_trade_never_fires_orders_when_edge_vanished():
    executor, store = _executor(Mode.LIVE.value)
    store.get_or_create("binance", "BTC/USDT").replace(bids=[(99.9, 5.0)], asks=[(100.0, 5.0)])
    store.get_or_create("kraken", "BTC/USDT").replace(bids=[(99.9, 5.0)], asks=[(100.0, 5.0)])

    # rest_manager is None; if the executor tried to place a real order
    # here it would raise (AttributeError on None), so a clean return
    # with an empty trade log proves the gate stopped it before that point.
    await executor.handle(_opportunity())

    assert executor.trade_log == []
