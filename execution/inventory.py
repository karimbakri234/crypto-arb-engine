"""Per-venue, per-asset balance tracking and rebalancing signals.

Every non-atomic strategy in this engine (everything except the
same-chain flash-loan DEX-DEX path) requires capital *already sitting*
on both sides of a trade before the opportunity appears -- see
execution/executor.py's module docstring for why. `InventoryManager` is
the source of truth for what capital actually exists where, so
`router.py` can check whether a leg is fundable before committing to it,
and so operators can see when an asset has drifted lopsided across
venues and needs a manual (or scheduled) rebalancing transfer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from config.venues import CEX_VENUES
from core.types import Balance

logger = logging.getLogger(__name__)

DEFAULT_TRANSFER_LATENCY_SEC = 900.0
REBALANCE_TRIGGER_RATIO = 0.15  # a venue holding < 15% of its "fair share" needs topping up


@dataclass(frozen=True, slots=True)
class TransferEstimate:
    """Estimated cost and time to move `asset` from one venue to another."""

    from_venue: str
    to_venue: str
    asset: str
    fee_units: float
    latency_sec: float


@dataclass(frozen=True, slots=True)
class RebalanceSignal:
    """A flag that an asset's inventory is lopsided across venues."""

    asset: str
    underfunded_venue: str
    current_fraction: float
    fair_fraction: float
    suggested_source_venue: str


class InventoryManager:
    """Tracks free/locked balances per (venue, asset) and flags imbalance."""

    def __init__(self) -> None:
        self._balances: dict[tuple[str, str], Balance] = {}

    def set_balance(self, venue_id: str, asset: str, free: float, locked: float = 0.0) -> None:
        self._balances[(venue_id, asset)] = Balance(venue_id, asset, free, locked)

    def get_balance(self, venue_id: str, asset: str) -> Balance:
        return self._balances.get((venue_id, asset), Balance(venue_id, asset, 0.0, 0.0))

    def reserve(self, venue_id: str, asset: str, amount: float) -> bool:
        """Lock `amount` of free balance so two strategies can't both spend it.

        Returns False (and locks nothing) if free balance is insufficient.
        """
        balance = self.get_balance(venue_id, asset)
        if balance.free < amount:
            return False
        balance.free -= amount
        balance.locked += amount
        self._balances[(venue_id, asset)] = balance
        return True

    def release(self, venue_id: str, asset: str, amount: float) -> None:
        """Unlock a previously reserved amount back to free balance."""
        balance = self.get_balance(venue_id, asset)
        amount = min(amount, balance.locked)
        balance.locked -= amount
        balance.free += amount
        self._balances[(venue_id, asset)] = balance

    def settle(self, venue_id: str, asset: str, amount: float) -> None:
        """Consume a previously reserved amount permanently (trade executed)."""
        balance = self.get_balance(venue_id, asset)
        balance.locked = max(0.0, balance.locked - amount)
        self._balances[(venue_id, asset)] = balance

    def credit(self, venue_id: str, asset: str, amount: float) -> None:
        """Add `amount` to free balance -- the asset a filled leg received.

        `settle` only removes what a leg spent. Without the matching
        credit, a buy would consume USDT on the buy venue and the SOL it
        bought would never appear anywhere, so inventory would drain to
        zero no matter how well the trading went. Together the two calls
        move balance across the (venue, asset) grid the way a real fill
        does -- which is what makes the lopsided drift that eventually
        forces a rebalancing transfer actually show up in paper mode
        instead of being silently free.
        """
        if amount <= 0:
            return
        balance = self.get_balance(venue_id, asset)
        balance.free += amount
        self._balances[(venue_id, asset)] = balance

    def total_across_venues(self, asset: str) -> float:
        return sum(b.total for (v, a), b in self._balances.items() if a == asset)

    def imbalance_report(self, asset: str, venues: list[str]) -> list[RebalanceSignal]:
        """Flag venues holding far less than an equal ("fair") share of `asset`."""
        total = self.total_across_venues(asset)
        if total <= 0 or not venues:
            return []

        fair_fraction = 1.0 / len(venues)
        signals: list[RebalanceSignal] = []
        per_venue = {v: self.get_balance(v, asset).total for v in venues}

        for venue_id, amount in per_venue.items():
            fraction = amount / total if total else 0.0
            if fraction < fair_fraction * REBALANCE_TRIGGER_RATIO:
                richest_venue = max(per_venue, key=per_venue.get)
                if richest_venue != venue_id:
                    signals.append(RebalanceSignal(asset, venue_id, fraction, fair_fraction, richest_venue))
                    logger.warning(
                        "Inventory imbalance: %s on %s is at %.1f%% of fair share; consider transferring from %s",
                        asset, venue_id, fraction * 100, richest_venue,
                    )
        return signals

    @staticmethod
    def estimate_transfer(from_venue: str, to_venue: str, asset: str, latency_sec: float = DEFAULT_TRANSFER_LATENCY_SEC) -> TransferEstimate:
        """Estimate withdrawal fee (in `asset` units) and latency for a rebalancing transfer."""
        venue = CEX_VENUES.get(from_venue)
        fee_units = venue.withdrawal_fees.get(asset, 0.0) if venue else 0.0
        return TransferEstimate(from_venue, to_venue, asset, fee_units, latency_sec)
