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
from dataclasses import dataclass, field

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
    ) -> None:
        valid_modes = {m.value for m in Mode}
        if mode not in valid_modes:
            raise ValueError(f"Invalid mode={mode!r}; must be one of {valid_modes}")
        self.rest_manager = rest_manager
        self.risk_manager = risk_manager
        self.reconciler = reconciler
        self.book_store = book_store
        self.mode = mode
        self.trade_log: list[TradeLogEntry] = []

    async def handle(self, opportunity: Opportunity) -> None:
        """Route `opportunity` to the execution path for the current mode."""
        if not self.risk_manager.can_trade(opportunity):
            logger.debug("Risk manager declined opportunity: %s", opportunity)
            return

        if self.mode == Mode.MONITOR.value:
            self._handle_monitor(opportunity)
        elif self.mode == Mode.PAPER.value:
            self._handle_paper(opportunity)
        elif self.mode == Mode.LIVE.value:
            await self._handle_live(opportunity)

    def _handle_monitor(self, opportunity: Opportunity) -> None:
        logger.info("[MONITOR] %s", opportunity)

    def _estimate_pnl_usd(self, opportunity: Opportunity, size_usd: float) -> float:
        """Simple fee-adjusted PnL estimate used by monitor/paper accounting."""
        return size_usd * (opportunity.net_profit_pct / 100.0)

    def _handle_paper(self, opportunity: Opportunity) -> None:
        size_usd = self.risk_manager.size_with_depth_check(opportunity, self.book_store)
        if size_usd <= 0:
            logger.info("[PAPER] Skipping zero-size (post-slippage-check) opportunity: %s", opportunity)
            return

        pnl = self._estimate_pnl_usd(opportunity, size_usd)
        self.risk_manager.record_result(opportunity, pnl, success=True)
        self.trade_log.append(TradeLogEntry(opportunity=opportunity, mode="paper", pnl_usd=pnl))
        logger.info("[PAPER] Simulated fill: size=$%.2f net_pnl=$%.4f (%s)", size_usd, pnl, opportunity)

    async def _handle_live(self, opportunity: Opportunity) -> None:
        """Fire every leg of `opportunity` concurrently as real market orders.

        Every leg trades against pre-existing inventory (see module
        docstring). If any leg raises while others succeed, the
        reconciler is invoked immediately -- this leaves a one-sided
        position that requires an unwind trade, not silent acceptance.
        """
        size_usd = self.risk_manager.size_with_depth_check(opportunity, self.book_store)
        if size_usd <= 0:
            logger.info("[LIVE] Skipping zero-size (post-slippage-check) opportunity: %s", opportunity)
            return

        logger.warning("[LIVE] Firing %d leg(s) for %s (size=$%.2f)", len(opportunity.legs), opportunity, size_usd)

        results = await asyncio.gather(
            *(self._execute_leg(leg, size_usd, opportunity) for leg in opportunity.legs),
            return_exceptions=True,
        )

        leg_results: list[LegResult] = []
        for leg, result in zip(opportunity.legs, results):
            if isinstance(result, Exception):
                leg_results.append(LegResult(leg=leg, success=False, error=str(result)))
            else:
                leg_results.append(LegResult(leg=leg, success=True))

        all_ok = all(r.success for r in leg_results)
        self.reconciler.check(opportunity, leg_results)

        pnl = self._estimate_pnl_usd(opportunity, size_usd) if all_ok else 0.0
        self.risk_manager.record_result(opportunity, pnl, success=all_ok)
        note = "" if all_ok else "PARTIAL_FILL_RECONCILIATION_REQUIRED"
        self.trade_log.append(TradeLogEntry(opportunity=opportunity, mode="live", pnl_usd=pnl if all_ok else None, note=note))

        if all_ok:
            logger.info("[LIVE] All legs filled successfully. Estimated net PnL=$%.4f", pnl)

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
