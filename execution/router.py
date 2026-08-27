"""Picks the best execution path when multiple strategies flag the same
asset, and prevents double-committing the same inventory to two trades.

When cross_exchange, cex_dex, and multi_leg all separately flag BTC on
Binance in the same tick, only one of them can actually spend Binance's
BTC balance. `Router.select` ranks all candidate opportunities by
expected net value (`net_profit_pct * max_size_usd`), greedily accepts
the best one first, reserves the inventory each of its legs needs via
`InventoryManager.reserve`, and skips any subsequent opportunity that
cannot get all of its legs reserved -- so the same free balance is never
promised to two different trades in the same tick.
"""

from __future__ import annotations

import logging

from execution.inventory import InventoryManager
from strategies.base import Opportunity

logger = logging.getLogger(__name__)


def _leg_asset(symbol: str, side: str) -> str:
    """Best-effort extraction of the asset a leg's reservation is against.

    For a spot symbol `BASE/QUOTE`, buying spends QUOTE and selling spends
    BASE. Non-spot symbols (perp/futures/transfer legs) fall back to the
    symbol itself as a synthetic asset key.
    """
    if "/" in symbol:
        base, quote = symbol.split("/", 1)
        return quote if side == "buy" else base
    return symbol


class Router:
    """Selects and reserves inventory for the highest-expected-value opportunities."""

    def __init__(self, inventory: InventoryManager) -> None:
        self.inventory = inventory

    def select(self, opportunities: list[Opportunity]) -> list[Opportunity]:
        """Return the subset of `opportunities` that got their inventory reserved.

        Opportunities are considered in descending expected-value order
        (`net_profit_pct * max_size_usd`). An opportunity is accepted only
        if every leg's required asset can be reserved on its venue; if any
        leg fails to reserve, already-reserved legs for that opportunity
        are released and it is skipped.
        """
        ranked = sorted(opportunities, key=lambda o: o.net_profit_pct * o.max_size_usd, reverse=True)
        accepted: list[Opportunity] = []

        for opportunity in ranked:
            reserved: list[tuple[str, str, float]] = []
            ok = True
            for leg in opportunity.legs:
                asset = _leg_asset(leg.symbol, leg.side)
                # Buying a BASE/QUOTE pair spends QUOTE (notional = price * size);
                # selling it spends BASE (the size itself, in base units).
                amount = (leg.price * leg.size) if leg.side == "buy" else leg.size
                if amount <= 0:
                    continue
                if self.inventory.reserve(leg.venue_id, asset, amount):
                    reserved.append((leg.venue_id, asset, amount))
                else:
                    ok = False
                    break

            if ok:
                # Stash what was locked so the caller can resolve it after
                # the executor decides. An accepted opportunity is NOT a
                # completed trade -- the executor still re-checks
                # profitability and venue minimums and may reject it -- so
                # the reservation has to survive until that outcome is
                # known. Before this, nothing ever unlocked it either way.
                opportunity.detail["_reservations"] = reserved
                accepted.append(opportunity)
            else:
                for venue_id, asset, notional in reserved:
                    self.inventory.release(venue_id, asset, notional)
                logger.debug("Skipped %s: could not reserve inventory for all legs", opportunity)

        return accepted

    def release_unfilled(self, opportunity: Opportunity) -> None:
        """Hand a rejected opportunity's reserved balance back to free."""
        for venue_id, asset, amount in opportunity.detail.pop("_reservations", []):
            self.inventory.release(venue_id, asset, amount)

    def settle_fill(self, opportunity: Opportunity) -> None:
        """Apply a completed trade to inventory: spend one asset, receive the other.

        This is what makes inventory drift real. A cross-exchange fill
        spends QUOTE on the buy venue and BASE on the sell venue, and
        credits back the opposite asset on each -- so buying SOL on htx
        and selling it on bingx steadily drains htx's USDT and bingx's
        SOL. Repeat it enough and one side runs dry and the router stops
        funding the route, which is exactly the point at which a real
        operator has to pay for a transfer. Leaving fills unsettled (the
        previous behaviour) made that cost invisible.
        """
        # Popping the reservations is what makes this idempotent: the
        # credit loop below mints balance, so a second call on the same
        # opportunity (a retry, a future refactor) would otherwise create
        # assets out of nothing. Settling something that was never
        # reserved is a bug, not a no-op, so return rather than credit.
        reservations = opportunity.detail.pop("_reservations", None)
        if not reservations:
            return

        for venue_id, asset, amount in reservations:
            self.inventory.settle(venue_id, asset, amount)

        for leg in opportunity.legs:
            if "/" not in leg.symbol or leg.size <= 0:
                continue
            base, quote = leg.symbol.split("/", 1)
            if leg.side == "buy":
                self.inventory.credit(leg.venue_id, base, leg.size)
            elif leg.side == "sell":
                self.inventory.credit(leg.venue_id, quote, leg.price * leg.size)
