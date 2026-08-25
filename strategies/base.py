"""Strategy ABC and the shared `Opportunity` result type.

Every strategy implements `scan(market_state) -> list[Opportunity]`
against one shared `MarketState` snapshot per tick. This uniform
interface is what lets the engine run all strategies concurrently
(`asyncio.gather`) against the same data instead of each strategy owning
its own polling loop.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from core.market_state import MarketState


@dataclass(slots=True)
class Leg:
    """One tradeable action within an opportunity (a single order/venue)."""

    venue_id: str
    symbol: str
    side: str  # "buy" | "sell"
    price: float
    size: float
    fee: float


@dataclass(slots=True)
class Opportunity:
    """A detected, fee-adjusted arbitrage opportunity.

    `legs` captures every order that would need to be placed to realize
    the opportunity (2 for spatial/cross-exchange, 3-5 for triangular/
    multi-leg, 2 delta-neutral legs for funding/basis/perp-perp, etc).
    `requires_prefunded_inventory` flags non-atomic strategies where both
    (or all) legs must trade against capital already sitting on each
    venue -- see execution/executor.py and README.
    """

    strategy: str
    symbol: str
    legs: tuple[Leg, ...]
    gross_profit_pct: float
    net_profit_pct: float
    max_size_usd: float
    detected_at: float = field(default_factory=time.time)
    requires_prefunded_inventory: bool = True
    is_atomic: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        leg_desc = " -> ".join(f"{leg.side}@{leg.venue_id}:{leg.symbol}@{leg.price:.6g}" for leg in self.legs)
        return f"[{self.strategy}] {self.symbol} net={self.net_profit_pct:.4f}% {leg_desc}"


class Strategy(ABC):
    """Base class every arbitrage strategy inherits from."""

    #: Short, stable identifier used in logs, recorder output, and routing.
    name: str = "base"

    def __init__(self, min_profit_pct: float = 0.1) -> None:
        self.min_profit_pct = min_profit_pct

    @abstractmethod
    def scan(self, market_state: MarketState) -> list[Opportunity]:
        """Scan `market_state` and return all opportunities clearing this
        strategy's minimum profit threshold, sorted by net profit descending."""
        raise NotImplementedError

    def _passes_threshold(self, net_profit_pct: float) -> bool:
        return net_profit_pct >= self.min_profit_pct
