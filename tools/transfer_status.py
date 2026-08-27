"""Can you actually move the asset off the venue that quotes it cheaply?

`tools/venue_lag.py` established that htx quotes SOL/USDT ~0.5% below
bitget, mexc and bingx, that all four are spot markets, that htx's book
is only ~0.6s behind, and that the gap survives fetching every venue in
the same instant. So the price difference is real in the sense that htx
publishes it.

That leaves one explanation standing. A 0.5% spread on SOL/USDT between
top-20 exchanges is taken in milliseconds by people whose entire job is
taking it. One that survives a full minute is not being missed -- it is
being *declined*, and the usual reason is that the cheap venue's deposits
or withdrawals for that asset are suspended. A wallet under maintenance,
a network upgrade, a paused chain: the asset strands on that exchange,
its local price decouples from the global one, and the gap persists for
hours or days precisely because nobody can arbitrage it away.

If that is what is happening here, the engine's SOL "opportunity" is a
one-way door: you buy cheap SOL on htx and it stays on htx.

This checks that directly, via each exchange's own currency metadata
(`fetch_currencies`) -- deposit and withdrawal flags, per-network status,
and the withdrawal fee that any real rebalancing would pay.

Usage:
    python -m tools.transfer_status                     # SOL on the usual venues
    python -m tools.transfer_status --asset USDT
    python -m tools.transfer_status --asset SOL --venues htx,mexc,bitget
"""

from __future__ import annotations

import argparse
import asyncio

import ccxt.async_support as ccxt

DEFAULT_VENUES = ["htx", "bingx", "bitget", "mexc"]
DEFAULT_ASSET = "SOL"


def _flag(value: object) -> str:
    """ccxt reports these as True/False/None; None means 'not stated'."""
    if value is True:
        return "yes"
    if value is False:
        return "NO"
    return "?"


async def _check(venue_id: str, asset: str) -> None:
    client = getattr(ccxt, venue_id)({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    try:
        currencies = await client.fetch_currencies()
    except Exception as exc:  # noqa: BLE001 - an unsupported venue is data, not fatal
        print(f"{venue_id:<10} could not fetch currency metadata: {type(exc).__name__}: {exc}")
        return
    finally:
        await client.close()

    entry = (currencies or {}).get(asset)
    if not entry:
        print(f"{venue_id:<10} does not list {asset}")
        return

    deposit, withdraw = _flag(entry.get("deposit")), _flag(entry.get("withdraw"))
    active = _flag(entry.get("active"))
    fee = entry.get("fee")
    fee_text = f"{fee:g} {asset}" if isinstance(fee, int | float) else "not stated"

    alarm = "   <-- CANNOT WITHDRAW" if withdraw == "NO" else ""
    print(f"{venue_id:<10} active={active:<4} deposit={deposit:<4} withdraw={withdraw:<4} fee={fee_text}{alarm}")

    networks = entry.get("networks") or {}
    for name, net in sorted(networks.items()):
        if not isinstance(net, dict):
            continue
        net_fee = net.get("fee")
        net_fee_text = f"{net_fee:g}" if isinstance(net_fee, int | float) else "?"
        print(
            f"             via {name:<12} deposit={_flag(net.get('deposit')):<4} "
            f"withdraw={_flag(net.get('withdraw')):<4} fee={net_fee_text}"
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--asset", default=DEFAULT_ASSET)
    parser.add_argument("--venues", default=",".join(DEFAULT_VENUES))
    args = parser.parse_args()

    venue_ids = [v.strip() for v in args.venues.split(",") if v.strip() and hasattr(ccxt, v.strip())]

    print(f"\n{args.asset} deposit/withdrawal status\n")
    for venue_id in venue_ids:
        await _check(venue_id, args.asset)
        print()

    print("=" * 72)
    print(
        f"A venue showing withdraw=NO for {args.asset} explains a persistent price gap without\n"
        "any arbitrage being available: the asset can be bought there and not moved out,\n"
        "so the discount is the market pricing in that you would be stuck holding it.\n\n"
        "Compare the withdrawal fee against the profit per trade before treating a gap as\n"
        "capturable even where withdrawals are open -- moving the asset back to rebalance is\n"
        "the cost that decides whether a one-directional edge is worth anything."
    )


if __name__ == "__main__":
    asyncio.run(main())
