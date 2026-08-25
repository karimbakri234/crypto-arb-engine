"""Hot-path typed structures.

`Quote` and `OpportunitySignal` sit on the ingestion/detection hot path
and use `msgspec.Struct` for low allocation overhead. Everything else is
a `slots=True` dataclass, which is nearly as cheap and keeps the rest of
the codebase in plain-dataclass style.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import msgspec


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Mode(StrEnum):
    MONITOR = "monitor"
    PAPER = "paper"
    LIVE = "live"


class Quote(msgspec.Struct, frozen=True, gc=False):
    """Top-of-book snapshot for one symbol on one venue.

    `gc=False` tells msgspec these instances never participate in
    reference cycles, letting the garbage collector skip them entirely —
    meaningful when thousands are allocated per second from feed data.
    """

    venue_id: str
    symbol: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    timestamp_ns: int
    taker_fee: float
    maker_fee: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


class OpportunitySignal(msgspec.Struct, gc=False):
    """Uniform, lightweight opportunity record emitted by every strategy.

    Strategy-specific detail (e.g. buy/sell venue, cycle path, funding
    rate) lives in `detail`, a plain dict, so the hot fields used for
    ranking/routing/recording stay flat and fast to serialize.
    """

    strategy: str
    symbol: str
    detected_at_ns: int
    gross_profit_pct: float
    net_profit_pct: float
    est_size_usd: float
    detail: dict = msgspec.field(default_factory=dict)


@dataclass(slots=True)
class OrderBookLevel:
    """One price/size level of an order book."""

    price: float
    size: float


@dataclass(slots=True)
class Balance:
    """A single venue/asset balance entry used by execution/inventory.py."""

    venue_id: str
    asset: str
    free: float
    locked: float = 0.0

    @property
    def total(self) -> float:
        return self.free + self.locked


@dataclass(slots=True)
class FundingRate:
    """A perpetual futures funding rate snapshot."""

    venue_id: str
    symbol: str
    rate: float          # fraction, per funding interval
    interval_hours: float
    next_funding_ts: float

    @property
    def annualized_pct(self) -> float:
        periods_per_year = (365.0 * 24.0) / self.interval_hours
        return self.rate * periods_per_year * 100.0


@dataclass(slots=True)
class FuturesQuote:
    """A dated-futures / calendar-spread quote."""

    venue_id: str
    symbol: str
    expiry_ts: float
    price: float
    spot_price: float

    @property
    def days_to_expiry(self) -> float:
        import time

        return max((self.expiry_ts - time.time()) / 86400.0, 1e-9)

    @property
    def basis_pct(self) -> float:
        if self.spot_price <= 0:
            return 0.0
        return (self.price - self.spot_price) / self.spot_price * 100.0

    @property
    def annualized_basis_pct(self) -> float:
        return self.basis_pct * (365.0 / self.days_to_expiry)
