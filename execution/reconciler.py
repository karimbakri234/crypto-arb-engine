"""Detects one-sided fills and proposes an unwind.

Non-atomic multi-leg execution (`asyncio.gather(..., return_exceptions=True)`
in executor.py) means one leg can fail after the other has already
filled, leaving an unhedged, unintended position. This is the single
biggest source of real losses in non-atomic arbitrage: a "arbitrage" bot
that doesn't handle this can silently accumulate directional exposure
one failed leg at a time. `Reconciler` is the safety net -- it inspects
each leg's result, and for any opportunity where exactly one leg
succeeded, logs loudly and proposes the exact offsetting trade needed to
flatten the resulting position back to neutral.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from strategies.base import Leg, Opportunity

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UnwindPlan:
    """The offsetting trade needed to flatten a one-sided fill."""

    venue_id: str
    symbol: str
    side: str  # opposite of the filled leg's side
    size: float
    reason: str


@dataclass(frozen=True, slots=True)
class LegResult:
    """The outcome of attempting to execute one leg."""

    leg: Leg
    success: bool
    error: str = ""


class Reconciler:
    """Detects partial fills across an opportunity's legs and proposes unwinds."""

    def __init__(self) -> None:
        self.open_incidents: list[tuple[Opportunity, list[LegResult]]] = []

    def check(self, opportunity: Opportunity, leg_results: list[LegResult]) -> list[UnwindPlan]:
        """Inspect `leg_results` for a one-sided fill and propose unwinds if found.

        A fully successful execution (all legs filled) or a fully failed
        one (no legs filled) needs no unwind -- exposure is either
        complete and intended, or nonexistent. Anything in between is a
        one-sided fill.
        """
        successes = [r for r in leg_results if r.success]
        failures = [r for r in leg_results if not r.success]

        if not failures or not successes:
            return []

        logger.error(
            "*** ONE-SIDED FILL DETECTED *** opportunity=%s: %d/%d legs filled. "
            "MANUAL RECONCILIATION REQUIRED. Failed legs: %s",
            opportunity, len(successes), len(leg_results),
            [(r.leg.venue_id, r.leg.symbol, r.error) for r in failures],
        )
        self.open_incidents.append((opportunity, leg_results))

        plans: list[UnwindPlan] = []
        for result in successes:
            opposite_side = "sell" if result.leg.side == "buy" else "buy"
            plans.append(
                UnwindPlan(
                    venue_id=result.leg.venue_id,
                    symbol=result.leg.symbol,
                    side=opposite_side,
                    size=result.leg.size,
                    reason=(
                        f"Leg filled ({result.leg.side} {result.leg.size} {result.leg.symbol} "
                        f"@ {result.leg.venue_id}) but its counterpart leg failed; flattening."
                    ),
                )
            )
            logger.warning("Proposed unwind: %s %s %.8f on %s", opposite_side, result.leg.symbol, result.leg.size, result.leg.venue_id)

        return plans

    def resolve_incident(self, opportunity: Opportunity) -> None:
        """Mark an incident as manually resolved (unwind confirmed filled)."""
        self.open_incidents = [(o, r) for (o, r) in self.open_incidents if o is not opportunity]

    @property
    def has_open_incidents(self) -> bool:
        return len(self.open_incidents) > 0
