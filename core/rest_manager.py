"""REST fallback and private-endpoint access via ccxt.async_support.

Websocket feeds (see feed_manager.py) are the primary source of market
data. `RestManager` covers everything websockets don't: connecting to
every venue and loading markets at startup, polling REST order books for
venues/strategies where no websocket subscription exists yet, and all
private (authenticated) calls — balances, order placement, order status —
which ccxt.pro streams don't replace.
"""

from __future__ import annotations

import asyncio
import logging
import time

import ccxt.async_support as ccxt

from config.settings import get_credentials
from config.venues import CEX_VENUES
from core.book import BookStore

logger = logging.getLogger(__name__)


class RestManager:
    """Owns one ccxt async client per CEX venue and polls/executes over REST."""

    def __init__(self, venue_ids: list[str] | None = None) -> None:
        self.venue_ids = venue_ids or list(CEX_VENUES.keys())
        self.clients: dict[str, ccxt.Exchange] = {}

    async def connect_all(self) -> list[str]:
        """Connect to every venue concurrently; failures are logged and skipped."""
        results = await asyncio.gather(
            *(self._connect_one(venue_id) for venue_id in self.venue_ids),
            return_exceptions=True,
        )
        connected: list[str] = []
        for venue_id, result in zip(self.venue_ids, results, strict=True):
            if isinstance(result, Exception):
                logger.warning("Failed to connect to %s: %s", venue_id, result)
                continue
            connected.append(venue_id)
        logger.info("RestManager connected to %d/%d venues", len(connected), len(self.venue_ids))
        return connected

    async def _connect_one(self, venue_id: str) -> None:
        if not hasattr(ccxt, venue_id):
            raise ValueError(f"ccxt has no exchange named {venue_id!r}")
        exchange_class = getattr(ccxt, venue_id)
        creds = get_credentials(venue_id)
        client_config = {"enableRateLimit": True}
        client_config.update({k: v for k, v in creds.items() if v})

        client = exchange_class(client_config)
        try:
            await client.load_markets()
        except Exception:
            await client.close()
            raise
        self.clients[venue_id] = client

    async def poll_order_book(self, venue_id: str, symbol: str, book_store: BookStore, depth: int = 20) -> None:
        """Fetch one REST order book snapshot and write it into `book_store`."""
        client = self.clients.get(venue_id)
        if client is None or symbol not in client.markets:
            return
        raw = await client.fetch_order_book(symbol, limit=depth)
        book = book_store.get_or_create(venue_id, symbol)
        book.replace(bids=raw.get("bids", []), asks=raw.get("asks", []))

    async def poll_loop(
        self,
        book_store: BookStore,
        symbols: list[str],
        interval_sec: float,
        stop_event: asyncio.Event,
    ) -> None:
        """Continuously poll REST order books for `symbols` on every connected venue.

        Intended as the fallback path for venues without an active
        websocket subscription. Runs until `stop_event` is set.
        """
        while not stop_event.is_set():
            start = time.perf_counter()
            tasks = [
                self.poll_order_book(venue_id, symbol, book_store)
                for venue_id in self.clients
                for symbol in symbols
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
            elapsed = time.perf_counter() - start
            await asyncio.sleep(max(0.0, interval_sec - elapsed))

    async def fetch_balance(self, venue_id: str) -> dict:
        """Fetch account balances from `venue_id` (requires credentials)."""
        client = self.clients[venue_id]
        return await client.fetch_balance()

    async def create_market_order(self, venue_id: str, symbol: str, side: str, amount: float) -> dict:
        """Place a live market order. `side` is 'buy' or 'sell'."""
        client = self.clients[venue_id]
        if side == "buy":
            return await client.create_market_buy_order(symbol, amount)
        return await client.create_market_sell_order(symbol, amount)

    async def close_all(self) -> None:
        """Cleanly close every open client session."""
        await asyncio.gather(*(c.close() for c in self.clients.values()), return_exceptions=True)
