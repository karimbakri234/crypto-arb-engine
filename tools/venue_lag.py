"""Is a venue actually cheaper, or is its order book just late?

The engine reported a persistent ~0.16% edge on SOL/USDT with htx always
on the buy side, and `latency_arb` -- which only fires when the buy
venue's book is staler than the sell venue's -- fired on every one. That
is either a real structural basis worth trading, or an artifact of our
own polling, and the two are indistinguishable from the engine's data:
`latency_arb` measures staleness with *our* receive timestamps, so a
venue we simply poll less often looks stale whether or not it is.

This removes that confound. It fetches every venue's book in a single
`asyncio.gather` -- one instant, no schedule skew -- and reports, per
round:

  * the cross-venue edge as the engine would compute it
  * each venue's own reported book timestamp, and how far behind the
    freshest venue in the round it is

Read it like this:

  Edge survives simultaneous fetches, exchange timestamps close together
      -> a real basis. Worth pursuing (mind the transfer costs to reset
         inventory -- a persistent basis is a one-way trade, not a
         round trip).

  Edge disappears when fetched simultaneously
      -> our polling schedule manufactured it. Nothing there.

  Edge survives but one venue's timestamps are consistently seconds old
      -> that venue is publishing stale books. The price is real in the
         sense that it is what they published; it is not a price you can
         trade against, because by the time an order arrives the true
         book has moved.

Usage:
    python -m tools.venue_lag                      # SOL/USDT, default venues
    python -m tools.venue_lag --symbol BTC/USDT --rounds 40
    python -m tools.venue_lag --venues htx,bingx,bitget,mexc --interval 2
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass

import ccxt.async_support as ccxt

from config.settings import TAKER_FEE_FALLBACK
from config.venues import taker_fee_for

DEFAULT_VENUES = ["htx", "bingx", "bitget", "mexc"]
DEFAULT_SYMBOL = "SOL/USDT"


@dataclass
class Quote:
    venue_id: str
    bid: float
    ask: float
    exchange_ts: float | None  # seconds, as reported by the exchange
    fetched_at: float  # seconds, our clock, immediately after the response
    latency_ms: float
    market_type: str  # "spot", "swap", ... -- see _market_type_of


def _market_type_of(client, symbol: str) -> str:
    """Which market a venue actually resolved `symbol` to.

    This is not a formality. A perpetual swap trades at a premium or
    discount to spot as a matter of course -- funding rates exist to
    manage exactly that gap. If one venue in a comparison is quoting
    perps and another spot, the "arbitrage" between them is the basis,
    which is a real thing you can trade but is emphatically not the
    riskless spot spread the engine believes it found.
    """
    try:
        return str(client.market(symbol).get("type") or "unknown")
    except Exception:  # noqa: BLE001 - diagnostics must not die on metadata
        return "unknown"


async def _fetch(client, venue_id: str, symbol: str) -> Quote | None:
    started = time.time()
    try:
        raw = await client.fetch_order_book(symbol, limit=5)
    except Exception as exc:  # noqa: BLE001 - a venue failing is data, not fatal
        print(f"  {venue_id:<10} fetch failed: {type(exc).__name__}: {exc}")
        return None

    finished = time.time()
    bids, asks = raw.get("bids") or [], raw.get("asks") or []
    if not bids or not asks:
        return None

    ts = raw.get("timestamp")
    return Quote(
        venue_id=venue_id,
        bid=float(bids[0][0]),
        ask=float(asks[0][0]),
        exchange_ts=float(ts) / 1000.0 if ts else None,
        fetched_at=finished,
        latency_ms=(finished - started) * 1000.0,
        market_type=_market_type_of(client, symbol),
    )


def _report_round(quotes: list[Quote], symbol: str) -> float | None:
    """Print one round; return the best net edge found, if any."""
    if len(quotes) < 2:
        print("  (fewer than two venues responded)")
        return None

    newest = max((q.exchange_ts for q in quotes if q.exchange_ts), default=None)

    for q in sorted(quotes, key=lambda x: x.ask):
        # "n/a" where the venue reports no book timestamp of its own.
        staleness = f"{(newest - q.exchange_ts):+6.2f}s" if q.exchange_ts and newest else "   n/a"
        print(
            f"  {q.venue_id:<10} [{q.market_type:<7}] bid={q.bid:<12.4f} ask={q.ask:<12.4f}"
            f"  book_age_vs_freshest={staleness}  rtt={q.latency_ms:6.0f}ms"
        )

    types = {q.market_type for q in quotes}
    if len(types) > 1:
        print(f"  !! comparing different market types across venues: {sorted(types)} -- see _market_type_of")

    best_edge = None
    best_pair = None
    for buy in quotes:
        for sell in quotes:
            if buy.venue_id == sell.venue_id:
                continue
            gross = (sell.bid - buy.ask) / buy.ask * 100.0
            fees = (
                taker_fee_for(buy.venue_id, TAKER_FEE_FALLBACK)
                + taker_fee_for(sell.venue_id, TAKER_FEE_FALLBACK)
            ) * 100.0
            net = gross - fees
            if best_edge is None or net > best_edge:
                best_edge, best_pair = net, (buy.venue_id, sell.venue_id)

    if best_edge is not None and best_pair:
        verdict = "EDGE" if best_edge > 0 else "none"
        print(f"  -> best net edge on {symbol}: {best_edge:+.4f}% ({best_pair[0]} -> {best_pair[1]}) [{verdict}]")
    return best_edge


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--venues", default=",".join(DEFAULT_VENUES))
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--interval", type=float, default=3.0, help="seconds between rounds")
    args = parser.parse_args()

    venue_ids = [v.strip() for v in args.venues.split(",") if v.strip()]
    clients = {}
    for venue_id in venue_ids:
        if not hasattr(ccxt, venue_id):
            print(f"skipping unknown ccxt venue: {venue_id}")
            continue
        clients[venue_id] = getattr(ccxt, venue_id)({"enableRateLimit": True, "options": {"defaultType": "spot"}})

    print(f"\n{args.symbol}: fetching {len(clients)} venues simultaneously, {args.rounds} rounds\n")

    edges: list[float] = []
    staleness_by_venue: dict[str, list[float]] = {v: [] for v in clients}

    try:
        for round_no in range(1, args.rounds + 1):
            print(f"round {round_no}/{args.rounds}  ({time.strftime('%H:%M:%S')})")
            results = await asyncio.gather(
                *(_fetch(c, v, args.symbol) for v, c in clients.items()), return_exceptions=False
            )
            quotes = [q for q in results if q is not None]

            newest = max((q.exchange_ts for q in quotes if q.exchange_ts), default=None)
            for q in quotes:
                if q.exchange_ts and newest:
                    staleness_by_venue[q.venue_id].append(newest - q.exchange_ts)

            edge = _report_round(quotes, args.symbol)
            if edge is not None:
                edges.append(edge)
            print()

            if round_no < args.rounds:
                await asyncio.sleep(args.interval)
    finally:
        await asyncio.gather(*(c.close() for c in clients.values()), return_exceptions=True)

    print("=" * 72)
    if edges:
        positive = [e for e in edges if e > 0]
        print(f"rounds with a positive net edge: {len(positive)}/{len(edges)}")
        print(f"median net edge: {statistics.median(edges):+.4f}%   max: {max(edges):+.4f}%")
    print("\nbook staleness vs the freshest venue in each round (median):")
    for venue_id, samples in staleness_by_venue.items():
        if samples:
            print(f"  {venue_id:<10} {statistics.median(samples):+.2f}s   (n={len(samples)})")
        else:
            print(f"  {venue_id:<10} no exchange-reported timestamps")
    print(
        "\nA venue that is consistently seconds behind is publishing stale books:\n"
        "its 'cheap' price is not one you can trade against."
    )


if __name__ == "__main__":
    asyncio.run(main())
