"""Turns detected opportunities into (simulated or real) trades.

KEY CONSTRAINT -- READ BEFORE USING `live` MODE:

Every non-atomic strategy in this engine (cross-exchange, triangular's
sequential fills, CEX-DEX, cross-chain DEX-DEX, funding-rate, basis
carry, calendar spread, cross-quote, stablecoin depeg, wrapped-asset,
perp-perp, multi-leg) requires *capital already sitting on every venue a
leg touches before the opportunity appears*. You cannot buy the asset on
venue A, transfer it to venue B, and sell it there fast enough to capture
the spread: transfers take anywhere from seconds (on-chain, congestion
dependent) to tens of minutes (CEX withdrawal + confirmations + credit),
and by the time funds arrive the spread that justified the trade is
almost always gone. The `live` execution path therefore fires every leg
concurrently against *existing, pre-positioned inventory* -- it is never
a buy-transfer-sell pipeline. Running this for real money means manually
(or via execution/inventory.py's rebalancing signals) maintaining working
balances of every relevant asset on every venue you want it to trade on,
with a plan to periodically rebalance that inventory -- which itself
costs withdrawal fees and time this engine does not eliminate, only
surfaces (see `execution/inventory.py`).

The one exception is the same-chain, flash-loan-funded path in
`strategies/dex_dex.py`, which is genuinely atomic and needs no upfront
capital -- but that requires a deployed on-chain smart contract this
Python bot does not provide; see that module's docstring.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from analytics.metrics import MetricsRegistry
from core.book import BookStore
from core.rest_manager import RestManager
from core.types import Mode
from execution.reconciler import LegResult, Reconciler
from risk.manager import RiskManager
from strategies.base import Leg, Opportunity

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TradeLogEntry:
    """A single record of a paper or live trade attempt."""

    opportunity: Opportunity
    mode: str
    pnl_usd: float | None = None
    note: str = ""


class Executor:
    """Dispatches opportunities to the monitor, paper, or live execution path."""

    def __init__(
        self,
        rest_manager: RestManager,
        risk_manager: RiskManager,
        reconciler: Reconciler,
        book_store: BookStore,
        mode: str = Mode.MONITOR.value,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        valid_modes = {m.value for m in Mode}
        if mode not in valid_modes:
            raise ValueError(f"Invalid mode={mode!r}; must be one of {valid_modes}")
        self.rest_manager = rest_manager
        self.risk_manager = risk_manager
        self.reconciler = reconciler
        self.book_store = book_store
        self.mode = mode
        self.metrics = metrics
        self.trade_log: list[TradeLogEntry] = []

    def _reject(self, reason: str) -> None:
        """Count why a detected opportunity didn't become a trade.

        See `MetricsRegistry.record_rejection`: the per-opportunity skip
        logs are at debug level (a busy tick emits thousands), so these
        counters are what make "found opportunities, made no trades"
        explainable rather than looking like a broken engine.
        """
        if self.metrics is not None:
            self.metrics.record_rejection(reason)

    async def handle(self, opportunity: Opportunity) -> None:
        """Route `opportunity` to the execution path for the current mode.

        Monitor mode never commits capital -- it only logs -- so the
        commit/release pair wraps just the paper and live paths.
        """
        if not self.risk_manager.can_trade(opportunity):
            logger.debug("Risk manager declined opportunity: %s", opportunity)
            self._reject("risk_limits")
            return

        if self.mode == Mode.MONITOR.value:
            self._handle_monitor(opportunity)
            return

        # Hold the notional against the strategy/venue caps for as long as
        # the trade is in flight. `record_result` releases it on completion;
        # the paths that bail out before trading release it here, or the
        # caps would ratchet up on opportunities that never executed.
        self.risk_manager.commit(opportunity)
        traded = False
        try:
            if self.mode == Mode.PAPER.value:
                traded = self._handle_paper(opportunity)
            elif self.mode == Mode.LIVE.value:
                traded = await self._handle_live(opportunity)
        finally:
            if not traded:
                self.risk_manager.release_uncommitted(opportunity)

    def _handle_monitor(self, opportunity: Opportunity) -> None:
        logger.info("[MONITOR] %s", opportunity)

    def _estimate_pnl_usd(self, size_usd: float, net_profit_pct: float) -> float:
        """Fee-adjusted PnL estimate from an as-of-now net profit percentage."""
        return size_usd * (net_profit_pct / 100.0)

    def _handle_paper(self, opportunity: Opportunity) -> bool:
        """Simulate a fill. Returns whether a trade was actually recorded."""
        size_usd = self.risk_manager.size_with_depth_check(opportunity, self.book_store)
        if size_usd <= 0:
            logger.debug("[PAPER] Skipping zero-size (post-slippage-check) opportunity: %s", opportunity)
            self._reject("below_venue_minimum_or_slippage")
            return False

        still_profitable, net_profit_pct = self.risk_manager.reverify_profitability(
            opportunity, self.book_store, size_usd
        )
        if not still_profitable:
            logger.debug(
                "[PAPER] Skipping: no longer profitable as-of-now (recomputed net=%.4f%%): %s",
                net_profit_pct, opportunity,
            )
            self._reject("edge_gone_before_execution")
            return False

        pnl = self._estimate_pnl_usd(size_usd, net_profit_pct)
        self.risk_manager.record_result(opportunity, pnl, success=True)
        self.trade_log.append(TradeLogEntry(opportunity=opportunity, mode="paper", pnl_usd=pnl))
        logger.info("[PAPER] Simulated fill: size=$%.2f net_pnl=$%.4f (%s)", size_usd, pnl, opportunity)
        return True

    async def _handle_live(self, opportunity: Opportunity) -> bool:
        """Fire every leg of `opportunity` concurrently as real market orders.

        Every leg trades against pre-existing inventory (see module
        docstring). If any leg raises while others succeed, the
        reconciler is invoked immediately -- this leaves a one-sided
        position that requires an unwind trade, not silent acceptance.

        Returns whether orders were actually placed.
        """
        size_usd = self.risk_manager.size_with_depth_check(opportunity, self.book_store)
        if size_usd <= 0:
            logger.debug("[LIVE] Skipping zero-size (post-slippage-check) opportunity: %s", opportunity)
            self._reject("below_venue_minimum_or_slippage")
            return False

        still_profitable, net_profit_pct = self.risk_manager.reverify_profitability(
            opportunity, self.book_store, size_usd
        )
        if not still_profitable:
            logger.debug(
                "[LIVE] Skipping: no longer profitable as-of-now (recomputed net=%.4f%%); real money stays put: %s",
                net_profit_pct, opportunity,
            )
            self._reject("edge_gone_before_execution")
            return False

        logger.warning("[LIVE] Firing %d leg(s) for %s (size=$%.2f)", len(opportunity.legs), opportunity, size_usd)

        results = await asyncio.gather(
            *(self._execute_leg(leg, size_usd, opportunity) for leg in opportunity.legs),
            return_exceptions=True,
        )

        leg_results: list[LegResult] = []
        for leg, result in zip(opportunity.legs, results, strict=True):
            if isinstance(result, Exception):
                leg_results.append(LegResult(leg=leg, success=False, error=str(result)))
            else:
                leg_results.append(LegResult(leg=leg, success=True))

        all_ok = all(r.success for r in leg_results)
        self.reconciler.check(opportunity, leg_results)

        pnl = self._estimate_pnl_usd(size_usd, net_profit_pct) if all_ok else 0.0
        self.risk_manager.record_result(opportunity, pnl, success=all_ok)
        note = "" if all_ok else "PARTIAL_FILL_RECONCILIATION_REQUIRED"
        self.trade_log.append(TradeLogEntry(opportunity=opportunity, mode="live", pnl_usd=pnl if all_ok else None, note=note))

        if all_ok:
            logger.info("[LIVE] All legs filled successfully. Estimated net PnL=$%.4f", pnl)
        return True

    async def _execute_leg(self, leg: Leg, size_usd: float, opportunity: Opportunity) -> dict:
        """Place one leg's market order. Non-CEX venues (DEX/synthetic) raise
        `NotImplementedError`, since on-chain transaction construction and
        signing is out of scope for this reference engine -- see
        strategies/dex_dex.py's flash-loan note."""
        size = size_usd / leg.price if leg.price else 0.0
        if leg.venue_id not in self.rest_manager.clients:
            raise NotImplementedError(
                f"No live execution path configured for venue {leg.venue_id!r} "
                f"(non-CEX legs require chain-specific transaction construction)."
            )
        return await self.rest_manager.create_market_order(leg.venue_id, leg.symbol, leg.side, size)
