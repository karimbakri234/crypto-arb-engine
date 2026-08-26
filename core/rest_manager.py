"""REST fallback and private-endpoint access via ccxt.async_support.

Websocket feeds (see feed_manager.py) are the primary source of market
data. `RestManager` covers everything websockets don't: connecting to
every venue and loading markets at startup, polling REST order books for
venues/strategies where no websocket subscription exists yet, and all
private (authenticated) calls — balances, order placement, order status —
which ccxt.pro streams don't replace.

Two things happen at connect time that matter a lot on a small host:

* **Market pruning.** `load_markets()` returns *every* market a venue
  lists -- often 2,000+ spot/futures/option entries, each carrying the
  venue's raw JSON in `market["info"]` -- and ccxt holds all of it for
  the life of the client. Across a dozen-plus venues that is the single
  largest memory consumer in the process, and it was a direct cause of
  a real OOM kill. Since this engine only ever polls, sizes, or trades
  symbols inside `config.universe`, everything else is dropped
  immediately after load (`_prune_markets`).
* **Real fee capture.** ccxt reports the actual maker/taker rates that
  apply to the connected account. Those override the hand-written
  fallback table in `config/venues.py` (`register_live_fees`), because
  fee accuracy is what decides whether a spread is genuinely profitable
  -- a 0.1% error is larger than most real spreads.
"""

from __future__ import annotations

import asyncio
import logging
import time

import ccxt.async_support as ccxt

from config.settings import MAX_CONCURRENT_POLLS, TIER_CONFIG, get_credentials
from config.universe import ALL_BASE_ASSETS, QUOTE_ASSETS, STABLECOINS, tier_of
from config.venues import CEX_VENUES, register_live_fees
from core.book import BookStore

logger = logging.getLogger(__name__)

# Fail a venue fast rather than letting one slow exchange stall startup
# for everyone (connect_all gathers them concurrently, but a hung request
# still holds the whole startup path open).
LOAD_MARKETS_TIMEOUT_SEC = 30.0


def _poll_interval_for(symbol: str, default: float) -> float:
    """How often to re-poll `symbol`, from its tier's configured cadence."""
    base = symbol.split("/", 1)[0]
    tier = TIER_CONFIG.get(tier_of(base))
    return tier.poll_interval_sec if tier is not None else default


def universe_symbols() -> frozenset[str]:
    """Every `BASE/QUOTE` string this engine could ever ask a venue for.

    Union of what `build_tradeable_symbols` and `build_stable_pairs`
    (config/universe.py) can produce, so pruning a venue's market map to
    this set never removes a symbol some strategy might later poll.
    """
    bases = tuple(ALL_BASE_ASSETS) + tuple(STABLECOINS)
    quotes = tuple(QUOTE_ASSETS) + tuple(STABLECOINS)
    return frozenset(f"{b}/{q}" for b in bases for q in quotes if b != q)


def _prune_markets(client: ccxt.Exchange, keep: frozenset[str]) -> tuple[int, int]:
    """Drop every market outside `keep` from a loaded ccxt client.

    Rebuilds ccxt's own symbol/id indexes alongside `markets` so they stay
    mutually consistent (order placement resolves a symbol through them).
    Returns `(kept, dropped)`. Pruning is skipped entirely if it would
    leave the venue with no markets at all, so a venue whose symbols
    simply don't overlap this universe is left untouched for the caller
    to notice rather than silently emptied.
    """
    markets = getattr(client, "markets", None)
    if not isinstance(markets, dict) or not markets:
        return (0, 0)

    kept = {symbol: market for symbol, market in markets.items() if symbol in keep}
    if not kept:
        return (0, 0)

    dropped = len(markets) - len(kept)
    client.markets = kept
    client.symbols = sorted(kept)

    kept_ids = {
        market["id"]
        for market in kept.values()
        if isinstance(market, dict) and market.get("id")
    }
    markets_by_id = getattr(client, "markets_by_id", None)
    if isinstance(markets_by_id, dict):
        client.markets_by_id = {mid: v for mid, v in markets_by_id.items() if mid in kept_ids}
    client.ids = sorted(kept_ids)

    return (len(kept), dropped)


def _capture_live_fees(venue_id: str, client: ccxt.Exchange) -> int:
    """Record the venue's real maker/taker rates for later fee lookups.

    Captures a venue-wide default from `client.fees["trading"]` plus any
    per-symbol overrides ccxt reports, both of which beat the static
    fallback table. Returns how many per-symbol rates were recorded.
    """
    fees = getattr(client, "fees", None)
    if isinstance(fees, dict):
        trading = fees.get("trading")
        if isinstance(trading, dict):
            register_live_fees(
                venue_id,
                symbol=None,
                taker=_as_rate(trading.get("taker")),
                maker=_as_rate(trading.get("maker")),
            )

    per_symbol = 0
    for symbol, market in (getattr(client, "markets", None) or {}).items():
        if not isinstance(market, dict):
            continue
        taker, maker = _as_rate(market.get("taker")), _as_rate(market.get("maker"))
        if taker is None and maker is None:
            continue
        register_live_fees(venue_id, symbol=symbol, taker=taker, maker=maker)
        per_symbol += 1
    return per_symbol


def _as_rate(value: object) -> float | None:
    """Coerce a ccxt fee field to a plausible fractional rate, else None.

    ccxt fee fields are usually floats but can be None, a string, or
    absent. Anything outside a sane fractional band (allowing small
    negative maker rebates) is rejected rather than trusted, since a
    misparsed fee silently corrupts every profitability calculation
    downstream.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if not (-0.01 <= rate <= 0.05):
        return None
    return rate


class RestManager:
    """Owns one ccxt async client per CEX venue and polls/executes over REST."""

    def __init__(self, venue_ids: list[str] | None = None) -> None:
        self.venue_ids = venue_ids or list(CEX_VENUES.keys())
        self.clients: dict[str, ccxt.Exchange] = {}
        # Computed once: pruning runs per venue and this set never changes.
        self._universe = universe_symbols()

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

    async def reconnect(self, venue_id: str) -> bool:
        """Close and recreate one venue's ccxt client, picking up fresh
        credentials from the environment (see `dashboard/credentials.py`).

        Used when API keys are set or changed for a venue while the bot is
        already running, so the dashboard's "connected venues" panel can
        arm a venue for trading without needing a full process restart.
        Returns whether the reconnect succeeded.
        """
        old_client = self.clients.pop(venue_id, None)
        if old_client is not None:
            await old_client.close()
        try:
            await self._connect_one(venue_id)
            return True
        except Exception as exc:
            logger.warning("Reconnect failed for %s: %s", venue_id, exc)
            return False

    async def _connect_one(self, venue_id: str) -> None:
        if not hasattr(ccxt, venue_id):
            raise ValueError(f"ccxt has no exchange named {venue_id!r}")
        exchange_class = getattr(ccxt, venue_id)
        creds = get_credentials(venue_id)
        client_config = {
            "enableRateLimit": True,
            "timeout": int(LOAD_MARKETS_TIMEOUT_SEC * 1000),
            # This engine trades spot books only. Asking for spot up front
            # keeps venues that key off this option from loading (and, on
            # some, failing against) their futures/contracts endpoints.
            "options": {"defaultType": "spot"},
        }
        client_config.update({k: v for k, v in creds.items() if v})

        client = exchange_class(client_config)
        try:
            async with asyncio.timeout(LOAD_MARKETS_TIMEOUT_SEC):
                await client.load_markets()
        except Exception:
            await client.close()
            raise

        kept, dropped = _prune_markets(client, self._universe)
        per_symbol_fees = _capture_live_fees(venue_id, client)
        logger.info(
            "Connected %s: kept %d market(s), dropped %d off-universe, %d live fee rate(s)",
            venue_id, kept, dropped, per_symbol_fees,
        )
        self.clients[venue_id] = client

    async def poll_order_book(self, venue_id: str, symbol: str, book_store: BookStore, depth: int = 20) -> None:
        """Fetch one REST order book snapshot and write it into `book_store`."""
        client = self.clients.get(venue_id)
        if client is None or symbol not in client.markets:
            return
        raw = await client.fetch_order_book(symbol, limit=depth)
        book = book_store.get_or_create(venue_id, symbol)
        book.replace(bids=raw.get("bids", []), asks=raw.get("asks", []))

    async def _poll_guarded(
        self,
        semaphore: asyncio.Semaphore,
        venue_id: str,
        symbol: str,
        book_store: BookStore,
    ) -> None:
        async with semaphore:
            await self.poll_order_book(venue_id, symbol, book_store)

    async def poll_loop(
        self,
        book_store: BookStore,
        symbols: list[str],
        interval_sec: float,
        stop_event: asyncio.Event,
        max_concurrent: int = MAX_CONCURRENT_POLLS,
    ) -> None:
        """Continuously poll REST order books for `symbols` on every connected venue.

        Intended as the fallback path for venues without an active
        websocket subscription. Runs until `stop_event` is set.

        Two things keep this from overwhelming both the exchanges and the
        host, neither of which the naive "every venue x every symbol,
        every cycle" version did:

        * **Tier-aware scheduling.** Each symbol is polled at its own
          tier's `poll_interval_sec` (`config.settings.TIER_CONFIG`)
          rather than all symbols every cycle. That config already
          existed and expressed exactly this intent -- majors polled
          often, long-tail alts rarely -- but nothing read it. Polling a
          full universe every cycle meant thousands of requests per
          venue queued behind ccxt's per-client rate limiter, so a
          "2 second" cycle really took minutes and the majors that
          actually matter went stale behind a queue of tier-3 alts.
        * **Bounded concurrency.** At a full universe the naive version
          created venues x symbols (thousands) of in-flight requests at
          once, each holding connection and buffer state. The semaphore
          caps that regardless of universe size.
        """
        if not symbols:
            return

        semaphore = asyncio.Semaphore(max(1, max_concurrent))
        interval_by_symbol = {s: _poll_interval_for(s, interval_sec) for s in symbols}
        # Wake often enough to service the shortest tier interval.
        tick = min(interval_by_symbol.values())
        next_due: dict[str, float] = dict.fromkeys(symbols, 0.0)

        while not stop_event.is_set():
            start = time.perf_counter()
            now = time.monotonic()

            due = [s for s in symbols if next_due[s] <= now]
            for symbol in due:
                next_due[symbol] = now + interval_by_symbol[symbol]

            if due:
                await asyncio.gather(
                    *(
                        self._poll_guarded(semaphore, venue_id, symbol, book_store)
                        for venue_id in list(self.clients)
                        for symbol in due
                    ),
                    return_exceptions=True,
                )

            elapsed = time.perf_counter() - start
            await asyncio.sleep(max(0.0, tick - elapsed))

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
