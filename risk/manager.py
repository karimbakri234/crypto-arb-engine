"""Central risk enforcement: capital allocation, exposure caps, and
circuit breakers.

`RiskManager` is the single gate every opportunity must pass through
before execution: per-strategy capital allocation, per-venue exposure
caps, a max notional per trade, a daily loss limit, a max-trades-per-day
circuit breaker, a max-consecutive-failures kill-switch, and a manual
global emergency stop. Sizing additionally walks real order book depth
(via `core.book.BookState.vwap_fill_price`) rather than trusting
top-of-book size, and rejects a trade if the volume-weighted fill price
would eat the opportunity's edge.
"""

from __future__ import annotations

import logging
from datetime import date

from core.book import BookStore
from risk.limits import RiskLimits
from strategies.base import Opportunity

logger = logging.getLogger(__name__)


class RiskManager:
    """Enforces `RiskLimits` and tracks the daily/consecutive-failure state needed to."""

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

        self._current_day: date = date.today()
        self.daily_pnl_usd: float = 0.0
        self.daily_trade_count: int = 0
        self.consecutive_failures: int = 0

        self._strategy_deployed_usd: dict[str, float] = {}
        self._venue_deployed_usd: dict[str, float] = {}

    def _roll_day_if_needed(self) -> None:
        today = date.today()
        if today != self._current_day:
            logger.info(
                "New day: resetting risk counters (prior PnL=%.2f, trades=%d)",
                self.daily_pnl_usd, self.daily_trade_count,
            )
            self._current_day = today
            self.daily_pnl_usd = 0.0
            self.daily_trade_count = 0

    def can_trade(self, opportunity: Opportunity | None = None) -> bool:
        """Return False if any circuit breaker or limit blocks trading right now.

        When `opportunity` is given, also checks its strategy's capital
        allocation and every leg venue's exposure cap against what's
        already deployed.
        """
        self._roll_day_if_needed()

        if self.limits.emergency_stop:
            logger.warning("Emergency stop is active; trading halted")
            return False

        if self.daily_pnl_usd <= -abs(self.limits.daily_loss_limit_usd):
            logger.warning(
                "Daily loss limit breached (PnL=%.2f, limit=-%.2f); trading halted",
                self.daily_pnl_usd, self.limits.daily_loss_limit_usd,
            )
            return False

        if self.daily_trade_count >= self.limits.max_trades_per_day:
            logger.warning(
                "Max trades/day reached (%d/%d); trading halted",
                self.daily_trade_count, self.limits.max_trades_per_day,
            )
            return False

        if self.consecutive_failures >= self.limits.max_consecutive_failures:
            logger.warning(
                "Max consecutive failures reached (%d/%d); trading halted",
                self.consecutive_failures, self.limits.max_consecutive_failures,
            )
            return False

        if opportunity is not None:
            deployed = self._strategy_deployed_usd.get(opportunity.strategy, 0.0)
            cap = self.limits.strategy_capital_limit(opportunity.strategy)
            if deployed + opportunity.max_size_usd > cap:
                logger.info(
                    "Strategy %s capital cap reached (%.2f + %.2f > %.2f); skipping",
                    opportunity.strategy, deployed, opportunity.max_size_usd, cap,
                )
                return False

            for leg in opportunity.legs:
                venue_deployed = self._venue_deployed_usd.get(leg.venue_id, 0.0)
                venue_cap = self.limits.venue_exposure_limit(leg.venue_id)
                if venue_deployed + opportunity.max_size_usd > venue_cap:
                    logger.info(
                        "Venue %s exposure cap reached (%.2f + %.2f > %.2f); skipping",
                        leg.venue_id, venue_deployed, opportunity.max_size_usd, venue_cap,
                    )
                    return False

        return True

    def size_with_depth_check(
        self,
        opportunity: Opportunity,
        book_store: BookStore,
        max_slippage_pct: float = 0.1,
    ) -> float:
        """Compute a depth-aware size (USD) for `opportunity`'s buy leg.

        Walks the real order book (not just top-of-book) to find the
        volume-weighted fill price at the opportunity's proposed size,
        and shrinks (or zeroes) the size if that VWAP price would eat
        more than `max_slippage_pct` of the opportunity's edge.
        """
        if not opportunity.legs:
            return 0.0
        buy_leg = next((leg for leg in opportunity.legs if leg.side == "buy"), opportunity.legs[0])

        proposed_usd = min(opportunity.max_size_usd, self.limits.max_notional_per_trade_usd)
        if buy_leg.price <= 0:
            return 0.0
        proposed_size = proposed_usd / buy_leg.price

        book = book_store.get(buy_leg.venue_id, buy_leg.symbol)
        if book is None:
            return proposed_usd  # no depth data available; fall back to top-of-book sizing upstream

        vwap_price, filled_size = book.snapshot().vwap_fill_price("buy", proposed_size)
        if filled_size <= 0 or vwap_price <= 0:
            return 0.0

        slippage_pct = (vwap_price - buy_leg.price) / buy_leg.price * 100.0
        if slippage_pct > max_slippage_pct:
            logger.info(
                "Rejecting size for %s: slippage %.4f%% > max %.4f%%",
                opportunity, slippage_pct, max_slippage_pct,
            )
            return 0.0

        return min(proposed_usd, filled_size * vwap_price)

    def record_result(self, opportunity: Opportunity, pnl_usd: float, success: bool) -> None:
        """Record a completed (or failed) trade's outcome."""
        self._roll_day_if_needed()
        self.daily_pnl_usd += pnl_usd
        self.daily_trade_count += 1
        self.consecutive_failures = 0 if success else self.consecutive_failures + 1

        self._strategy_deployed_usd[opportunity.strategy] = (
            self._strategy_deployed_usd.get(opportunity.strategy, 0.0) + opportunity.max_size_usd
        )
        for leg in opportunity.legs:
            self._venue_deployed_usd[leg.venue_id] = self._venue_deployed_usd.get(leg.venue_id, 0.0) + opportunity.max_size_usd

        logger.info(
            "Trade recorded: strategy=%s pnl=%.4f success=%s (day total=%.4f, count=%d, consecutive_failures=%d)",
            opportunity.strategy, pnl_usd, success, self.daily_pnl_usd, self.daily_trade_count, self.consecutive_failures,
        )

    def trigger_emergency_stop(self, reason: str) -> None:
        """Manually halt all trading until explicitly cleared."""
        logger.critical("EMERGENCY STOP TRIGGERED: %s", reason)
        self.limits.emergency_stop = True

    def clear_emergency_stop(self) -> None:
        logger.info("Emergency stop cleared")
        self.limits.emergency_stop = False
