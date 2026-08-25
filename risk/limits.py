"""Declarative risk limits enforced by risk/manager.py.

Kept as a plain, serializable dataclass so limits can be reviewed,
version-controlled, and overridden per-deployment without touching
enforcement logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config.settings import (
    DAILY_LOSS_LIMIT_USD,
    MAX_CONSECUTIVE_FAILURES,
    MAX_TRADES_PER_DAY,
    MAX_TRADE_USD,
)


@dataclass(slots=True)
class RiskLimits:
    """All configured risk boundaries for one engine deployment."""

    #: Max USD notional this strategy may have deployed at once. Missing
    #: entries fall back to `default_strategy_capital_usd`.
    strategy_capital_usd: dict[str, float] = field(default_factory=dict)
    default_strategy_capital_usd: float = 5_000.0

    #: Max USD notional exposure allowed on one venue at once. Missing
    #: entries fall back to `default_venue_exposure_usd`.
    venue_exposure_usd: dict[str, float] = field(default_factory=dict)
    default_venue_exposure_usd: float = 10_000.0

    max_notional_per_trade_usd: float = MAX_TRADE_USD
    daily_loss_limit_usd: float = DAILY_LOSS_LIMIT_USD
    max_trades_per_day: int = MAX_TRADES_PER_DAY
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES

    #: Manually settable global kill-switch, independent of any counter.
    emergency_stop: bool = False

    def strategy_capital_limit(self, strategy: str) -> float:
        return self.strategy_capital_usd.get(strategy, self.default_strategy_capital_usd)

    def venue_exposure_limit(self, venue_id: str) -> float:
        return self.venue_exposure_usd.get(venue_id, self.default_venue_exposure_usd)
