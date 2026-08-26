"""Tests for RestManager.poll_loop's tier-aware scheduling and concurrency cap.

No network: `poll_order_book` is replaced with a recorder that also
tracks peak in-flight concurrency.

Both behaviours exist because the naive version -- every venue x every
symbol, every cycle -- issued thousands of simultaneous requests at a
full universe. That blows past every exchange's rate limit (so majors go
stale queued behind long-tail alts) and holds thousands of connection
buffers open on the host at once.
"""

from __future__ import annotations

import asyncio

from config.settings import TIER_CONFIG
from core.book import BookStore
from core.rest_manager import RestManager, _poll_interval_for


def test_poll_interval_follows_the_symbol_tier():
    # Majors are polled far more often than long-tail alts.
    assert _poll_interval_for("BTC/USDT", 2.0) == TIER_CONFIG["tier1"].poll_interval_sec
    assert _poll_interval_for("PENDLE/USDT", 2.0) == TIER_CONFIG["tier3"].poll_interval_sec
    assert _poll_interval_for("USDC/USDT", 2.0) == TIER_CONFIG["stable"].poll_interval_sec
    assert _poll_interval_for("WBTC/USDT", 2.0) == TIER_CONFIG["wrapped"].poll_interval_sec
    assert _poll_interval_for("BTC/USDT", 2.0) < _poll_interval_for("PENDLE/USDT", 2.0)


async def _run_poll_loop(manager: RestManager, symbols: list[str], run_for: float, **kwargs) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(
        manager.poll_loop(BookStore(), symbols, interval_sec=2.0, stop_event=stop, **kwargs)
    )
    await asyncio.sleep(run_for)
    stop.set()
    await asyncio.wait_for(task, timeout=5.0)


async def test_tier1_symbols_are_polled_more_often_than_tier3(monkeypatch):
    manager = RestManager([])
    manager.clients = {"kraken": object()}
    calls: list[str] = []

    async def fake_poll(venue_id, symbol, book_store, depth=20):
        calls.append(symbol)

    monkeypatch.setattr(manager, "poll_order_book", fake_poll)

    await _run_poll_loop(manager, ["BTC/USDT", "PENDLE/USDT"], run_for=2.2)

    btc = calls.count("BTC/USDT")
    pendle = calls.count("PENDLE/USDT")
    assert btc > pendle, f"expected majors polled more often (BTC={btc}, PENDLE={pendle})"


async def test_concurrency_is_capped(monkeypatch):
    """Peak in-flight requests must stay within the cap no matter how many
    venue/symbol combinations come due in one pass."""
    manager = RestManager([])
    manager.clients = {f"venue{i}": object() for i in range(10)}

    in_flight = 0
    peak = 0

    async def fake_poll(venue_id, symbol, book_store, depth=20):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)  # hold the slot so overlap is observable
        in_flight -= 1

    monkeypatch.setattr(manager, "poll_order_book", fake_poll)

    # 10 venues x 6 tier-1 symbols = 60 combinations due at once, cap of 5.
    symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT", "DOGE/USDT"]

    await _run_poll_loop(manager, symbols, run_for=0.6, max_concurrent=5)

    assert peak <= 5, f"peak in-flight {peak} exceeded the cap of 5"
    assert peak > 1, "expected some real concurrency, not fully serialized"


async def test_empty_symbol_list_returns_immediately():
    manager = RestManager([])
    stop = asyncio.Event()

    await asyncio.wait_for(
        manager.poll_loop(BookStore(), [], interval_sec=2.0, stop_event=stop), timeout=1.0
    )
