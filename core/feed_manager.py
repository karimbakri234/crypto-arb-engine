"""Websocket feed multiplexer: the primary market-data path.

Feeds write into a shared `BookStore`; detection strategies read
snapshots from it. This separation is deliberate — a slow detection pass
must never block ingestion, so `FeedManager` never awaits a strategy and
strategies never await a feed; they only touch `BookStore`, whose reads
are lock-free (see core/book.py).

On ccxt.pro (`watch_order_book`) vs. REST polling
--------------------------------------------------
`ccxt.pro` — the module that gives ccxt real websocket streaming
(`watch_order_book`, `watch_ticker`, etc.) across 100+ exchanges — is a
paid, separately licensed package (`ccxtpro`), not part of the
open-source `ccxt` on PyPI used here. This module is written against
that interface: if `ccxtpro` is importable and licensed in your
environment, `FeedManager` uses it and gets genuine incremental,
push-based order book updates via `OrderBook.apply_diff`. If it is not
installed, `FeedManager` transparently falls back to `RestManager`'s
polling loop, which calls `OrderBook.replace` on each poll instead.

This matters for latency (see README "Before running with real money",
point 6): the REST-polling fallback is fundamentally slower and coarser
than a real websocket diff stream, and most of what makes this engine
"HFT-adjacent" architecturally (lock-free snapshots, vectorized
detection, latency histograms) only pays off once you're actually
feeding it from real websocket streams.
"""

from __future__ import annotations

import asyncio
import importlib
import logging

from core.book import BookStore
from core.rest_manager import RestManager

logger = logging.getLogger(__name__)

try:
    ccxtpro = importlib.import_module("ccxtpro")
    HAS_CCXT_PRO = True
except ImportError:
    ccxtpro = None
    HAS_CCXT_PRO = False


class FeedManager:
    """Multiplexes order book feeds across venues into a shared `BookStore`.

    Uses `ccxt.pro` websocket streams when available; otherwise delegates
    to `RestManager.poll_loop` as a fallback. Either way, callers just get
    a continuously updated `BookStore` — they never need to know which
    transport is underneath.
    """

    def __init__(self, book_store: BookStore, rest_manager: RestManager) -> None:
        self.book_store = book_store
        self.rest_manager = rest_manager
        self._ws_clients: dict[str, object] = {}
        self._tasks: list[asyncio.Task] = []
        self._stop_event = asyncio.Event()

    async def start(self, venue_ids: list[str], symbols: list[str], rest_poll_interval_sec: float) -> None:
        """Start feeding `symbols` for each of `venue_ids` into `book_store`."""
        if HAS_CCXT_PRO:
            logger.info("ccxt.pro detected: using native websocket streams")
            for venue_id in venue_ids:
                if not hasattr(ccxtpro, venue_id):
                    continue
                client = getattr(ccxtpro, venue_id)({"enableRateLimit": True})
                self._ws_clients[venue_id] = client
                for symbol in symbols:
                    self._tasks.append(asyncio.create_task(self._watch_loop(venue_id, client, symbol)))
        else:
            logger.warning(
                "ccxt.pro not installed: falling back to REST polling every %.1fs. "
                "This is materially slower than a real websocket feed -- see "
                "README 'Before running with real money', point 6 (latency).",
                rest_poll_interval_sec,
            )
            self._tasks.append(
                asyncio.create_task(
                    self.rest_manager.poll_loop(self.book_store, symbols, rest_poll_interval_sec, self._stop_event)
                )
            )

    async def _watch_loop(self, venue_id: str, client: object, symbol: str) -> None:
        """Continuously pull incremental order book diffs for one venue/symbol."""
        book = self.book_store.get_or_create(venue_id, symbol)
        while not self._stop_event.is_set():
            try:
                update = await client.watch_order_book(symbol)  # type: ignore[attr-defined]
            except Exception as exc:
                logger.warning("Websocket error on %s/%s: %s", venue_id, symbol, exc)
                await asyncio.sleep(1.0)
                continue
            book.replace(bids=update.get("bids", []), asks=update.get("asks", []))

    async def stop(self) -> None:
        """Stop all feed tasks and close any websocket clients."""
        self._stop_event.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for client in self._ws_clients.values():
            close = getattr(client, "close", None)
            if close is not None:
                await close()
