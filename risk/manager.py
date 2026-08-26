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

from config.venues import min_order_usd_for
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
            # Safety net: deployed capital is normally released as each
            # trade completes (see `record_result`). Clearing it here too
            # means a leaked reservation can at worst stall trading until
            # the next day rather than for the process's whole lifetime.
            self._strategy_deployed_usd.clear()
            self._venue_deployed_usd.clear()

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
            # These fire per candidate opportunity, of which a busy tick has
            # thousands -- debug, not info, or the log becomes the bottleneck.
            deployed = self._strategy_deployed_usd.get(opportunity.strategy, 0.0)
            cap = self.limits.strategy_capital_limit(opportunity.strategy)
            if deployed + opportunity.max_size_usd > cap:
                logger.debug(
                    "Strategy %s capital cap reached (%.2f + %.2f > %.2f); skipping",
                    opportunity.strategy, deployed, opportunity.max_size_usd, cap,
                )
                return False

            for leg in opportunity.legs:
                venue_deployed = self._venue_deployed_usd.get(leg.venue_id, 0.0)
                venue_cap = self.limits.venue_exposure_limit(leg.venue_id)
                if venue_deployed + opportunity.max_size_usd > venue_cap:
                    logger.debug(
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
        more than `max_slippage_pct` of the opportunity's edge. Returns 0
        for a size no venue on the route would accept (see
        `_clears_venue_minimums`).
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
            # No depth data; fall back to top-of-book sizing upstream --
            # but the venue minimum still applies, since it's a property
            # of the venue rather than of the book we happen to have.
            if not self._clears_venue_minimums(opportunity, proposed_usd):
                return 0.0
            return proposed_usd

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

        sized_usd = min(proposed_usd, filled_size * vwap_price)
        if not self._clears_venue_minimums(opportunity, sized_usd):
            return 0.0
        return sized_usd

    def _clears_venue_minimums(self, opportunity: Opportunity, size_usd: float) -> bool:
        """Whether `size_usd` meets the minimum order notional on every leg's venue.

        An opportunity sized below a venue's minimum is one that venue
        would reject outright, so treating it as tradeable is worse than
        finding nothing -- it inflates paper results with fills that could
        never happen for real. The binding constraint is the largest
        minimum across the legs, since every leg has to actually execute.
        """
        for leg in opportunity.legs:
            minimum = min_order_usd_for(leg.venue_id)
            if size_usd < minimum:
                logger.info(
                    "Rejecting %s: size $%.2f is below %s's $%.2f minimum order",
                    opportunity, size_usd, leg.venue_id, minimum,
                )
                return False
        return True

    def reverify_profitability(
        self,
        opportunity: Opportunity,
        book_store: BookStore,
        size_usd: float,
    ) -> tuple[bool, float]:
        """Recompute this opportunity's net profit against the order book
        *right now*, immediately before committing capital.

        `opportunity.net_profit_pct` is stamped at detection time. By the
        time sizing/risk checks finish, real REST-polling latency (see
        `config.settings.LATENCY_BUDGETS`) means the book has often moved
        or another actor has already taken the edge -- the recorder's own
        decay-curve analytics show most detected spreads do not survive
        even 100ms. This walks every leg's *current* book depth and real
        fees to get an as-of-now net profit; the caller should skip the
        trade unless the result is actually profitable, rather than
        trusting the stale detection-time figure.

        Returns `(still_profitable, recomputed_net_profit_pct)`.
        """
        if not opportunity.legs or size_usd <= 0:
            return False, 0.0

        buy_leg = next((leg for leg in opportunity.legs if leg.side == "buy"), opportunity.legs[0])
        if buy_leg.price <= 0 or buy_leg.size <= 0:
            return False, 0.0

        # Every leg's size scales together with the buy leg's (arbitrage
        # legs move fixed ratios of one another around a cycle), so one
        # scale factor derived from the sized buy leg applies to all legs.
        scale = (size_usd / buy_leg.price) / buy_leg.size

        total_cost = 0.0
        total_proceeds = 0.0
        for leg in opportunity.legs:
            leg_size = leg.size * scale
            book = book_store.get(leg.venue_id, leg.symbol)
            if book is None:
                price, filled = leg.price, leg_size
            else:
                price, filled = book.snapshot().vwap_fill_price(leg.side, leg_size)
            price, filled = float(price), float(filled)

            if filled <= 0 or price <= 0:
                return False, 0.0

            notional = price * filled
            fee_amount = notional * leg.fee
            if leg.side == "buy":
                total_cost += notional + fee_amount
            else:
                total_proceeds += notional - fee_amount

        if total_cost <= 0:
            return False, 0.0

        net_profit_pct = (total_proceeds - total_cost) / total_cost * 100.0
        return bool(net_profit_pct > 0.0), net_profit_pct

    def commit(self, opportunity: Opportunity) -> None:
        """Mark `opportunity`'s notional as deployed while it is in flight."""
        self._strategy_deployed_usd[opportunity.strategy] = (
            self._strategy_deployed_usd.get(opportunity.strategy, 0.0) + opportunity.max_size_usd
        )
        for leg in opportunity.legs:
            self._venue_deployed_usd[leg.venue_id] = (
                self._venue_deployed_usd.get(leg.venue_id, 0.0) + opportunity.max_size_usd
            )

    def release_uncommitted(self, opportunity: Opportunity) -> None:
        """Return notional held by `commit` for a trade that never executed.

        Callers commit before attempting a trade, but several paths bail
        out afterwards (zero size after the depth check, the edge gone on
        the as-of-now re-check). Without this the caps would ratchet up on
        opportunities that never traded.
        """
        self._release(opportunity)

    def _release(self, opportunity: Opportunity) -> None:
        """Return `opportunity`'s notional to the available pool."""
        strategy = opportunity.strategy
        remaining = self._strategy_deployed_usd.get(strategy, 0.0) - opportunity.max_size_usd
        self._strategy_deployed_usd[strategy] = max(0.0, remaining)
        for leg in opportunity.legs:
            venue_remaining = self._venue_deployed_usd.get(leg.venue_id, 0.0) - opportunity.max_size_usd
            self._venue_deployed_usd[leg.venue_id] = max(0.0, venue_remaining)

    def record_result(self, opportunity: Opportunity, pnl_usd: float, success: bool) -> None:
        """Record a completed (or failed) trade's outcome and free its capital.

        `strategy_capital_usd` / `venue_exposure_usd` bound notional
        deployed *at once* (see risk/limits.py), and every strategy here
        is a round trip whose legs open and close together -- so the
        capital is free again the moment the trade finishes, which is
        exactly here.

        This previously only ever *added* to the deployed totals and never
        subtracted, with no daily reset either. Deployed capital therefore
        ratcheted up until every strategy sat permanently at its cap and
        the engine stopped trading for the rest of the process's life --
        at the shipped defaults ($5,000 cap, $500 max notional) that was
        after just 10 trades per strategy, versus a `max_trades_per_day`
        of 2,000. A live paper run plateaued at 19 trades because of it.
        """
        self._roll_day_if_needed()
        self.daily_pnl_usd += pnl_usd
        self.daily_trade_count += 1
        self.consecutive_failures = 0 if success else self.consecutive_failures + 1

        self._release(opportunity)

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
