"""Persists every detected opportunity to parquet and measures whether it
would actually have survived to execution.

This is the profitability instrumentation the README calls the single
most important number in the project: `schedule_decay_check` re-examines
a symbol's live quotes after realistic execution delays (default 100ms,
500ms, 2s -- see `config.settings.DECAY_CHECK_DELAYS_SEC`) and records
whether the *same* spread was still there. The resulting "decay curve" --
what fraction of detected opportunities are still capturable after N
milliseconds of latency -- tells you what this engine can actually
capture in the real world, independent of how good detection looks on
paper.

`generate_summary_report` then turns the recorded opportunities and decay
checks into the numbers that actually matter for a go/no-go decision:
opportunities/hour, spread distribution, capturable fraction by latency
bucket, and -- explicitly, since profit rate is capital x capture_rate x
turnover -- what capital and capture rate would be *required* to hit a
given dollar-per-hour target, rather than leaving that as an exercise for
the reader.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

import pyarrow as pa
import pyarrow.parquet as pq

from config.settings import DECAY_CHECK_DELAYS_SEC, RECORDER_OUTPUT_DIR
from strategies.base import Opportunity

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DecayResult:
    """Whether an opportunity's spread was still present after a delay."""

    opportunity_id: int
    strategy: str
    symbol: str
    delay_sec: float
    original_net_profit_pct: float
    observed_net_profit_pct: float
    survived: bool


class OpportunityRecorder:
    """Buffers detected opportunities and their decay checks, and flushes to parquet."""

    def __init__(self, output_dir: str = RECORDER_OUTPUT_DIR, decay_delays: tuple[float, ...] = DECAY_CHECK_DELAYS_SEC) -> None:
        self.output_dir = output_dir
        self.decay_delays = decay_delays
        os.makedirs(output_dir, exist_ok=True)

        self._next_id = 0
        self._opportunity_buffer: list[dict] = []
        self._decay_buffer: list[dict] = []
        self.all_opportunity_records: list[dict] = []  # kept in-memory for generate_summary_report
        self.all_decay_records: list[dict] = []

    def record(self, opportunity: Opportunity) -> int:
        """Buffer one opportunity's full context. Returns an id for decay tracking."""
        opportunity_id = self._next_id
        self._next_id += 1

        record = {
            "id": opportunity_id,
            "timestamp": opportunity.detected_at,
            "strategy": opportunity.strategy,
            "symbol": opportunity.symbol,
            "num_legs": len(opportunity.legs),
            "venues": [leg.venue_id for leg in opportunity.legs],
            "gross_profit_pct": opportunity.gross_profit_pct,
            "net_profit_pct": opportunity.net_profit_pct,
            "fee_components": [leg.fee for leg in opportunity.legs],
            "max_size_usd": opportunity.max_size_usd,
            "requires_prefunded_inventory": opportunity.requires_prefunded_inventory,
            "is_atomic": opportunity.is_atomic,
            # Filled in later by `attach_trade_result` if this opportunity
            # actually got traded (paper or live) -- stays None for an
            # opportunity that was only ever detected (monitor mode, or
            # skipped by risk/router/the profitability re-check).
            "pnl_usd": None,
            "trade_mode": None,
        }
        self._opportunity_buffer.append(record)
        self.all_opportunity_records.append(record)
        return opportunity_id

    def attach_trade_result(self, opportunity_id: int, pnl_usd: float, mode: str) -> None:
        """Record a paper/live trade's realized PnL against its opportunity.

        `opportunity_id` indexes directly into `all_opportunity_records`
        since ids are assigned sequentially in `record()`'s append order.
        Lets the dashboard show a $ P&L next to the specific opportunity
        that produced it, not just an aggregate.
        """
        if 0 <= opportunity_id < len(self.all_opportunity_records):
            record = self.all_opportunity_records[opportunity_id]
            record["pnl_usd"] = pnl_usd
            record["trade_mode"] = mode

    def schedule_decay_checks(
        self,
        opportunity_id: int,
        opportunity: Opportunity,
        rescan_fn,
    ) -> list[asyncio.Task]:
        """Schedule a decay check at each configured delay.

        `rescan_fn` is a zero-arg callable returning the current
        `net_profit_pct` for the same strategy/venue pair (or None if it
        can no longer be computed, e.g. a venue dropped out) -- typically
        a small closure over the live `MarketState` and the specific
        strategy's comparison logic for this opportunity's venues.
        """
        tasks = []
        for delay in self.decay_delays:
            tasks.append(asyncio.create_task(self._check_after_delay(opportunity_id, opportunity, delay, rescan_fn)))
        return tasks

    async def _check_after_delay(self, opportunity_id: int, opportunity: Opportunity, delay: float, rescan_fn) -> None:
        await asyncio.sleep(delay)
        try:
            observed_net_pct = rescan_fn()
        except Exception:
            logger.debug("Decay rescan failed for opportunity %d", opportunity_id, exc_info=True)
            observed_net_pct = None

        survived = observed_net_pct is not None and observed_net_pct >= opportunity.net_profit_pct * 0.5
        record = {
            "opportunity_id": opportunity_id,
            "strategy": opportunity.strategy,
            "symbol": opportunity.symbol,
            "delay_sec": delay,
            "original_net_profit_pct": opportunity.net_profit_pct,
            "observed_net_profit_pct": observed_net_pct if observed_net_pct is not None else float("nan"),
            "survived": survived,
        }
        self._decay_buffer.append(record)
        self.all_decay_records.append(record)

    def flush(self) -> None:
        """Write buffered opportunity and decay records to parquet and clear buffers."""
        timestamp = int(time.time())
        if self._opportunity_buffer:
            table = pa.Table.from_pylist(self._opportunity_buffer)
            pq.write_table(table, os.path.join(self.output_dir, f"opportunities_{timestamp}.parquet"))
            self._opportunity_buffer.clear()
        if self._decay_buffer:
            table = pa.Table.from_pylist(self._decay_buffer)
            pq.write_table(table, os.path.join(self.output_dir, f"decay_{timestamp}.parquet"))
            self._decay_buffer.clear()

    def generate_summary_report(self, target_usd_per_hour: float | None = None) -> str:
        """Build a human-readable profitability summary from recorded data.

        Reports opportunities/hour, median/p95 net spread, capturable
        fraction by latency bucket, and -- since profit rate is
        capital x capture_rate x turnover -- the capital and capture rate
        that would actually be required to hit `target_usd_per_hour`, if
        given.
        """
        records = self.all_opportunity_records
        decays = self.all_decay_records
        if not records:
            return "No opportunities recorded yet."

        span_hours = max((time.time() - min(r["timestamp"] for r in records)) / 3600.0, 1e-9)
        opportunities_per_hour = len(records) / span_hours

        spreads = sorted(r["net_profit_pct"] for r in records)
        median_spread = spreads[len(spreads) // 2]
        p95_spread = spreads[int(len(spreads) * 0.95)] if len(spreads) > 1 else spreads[0]

        by_delay: dict[float, list[bool]] = {}
        for d in decays:
            by_delay.setdefault(d["delay_sec"], []).append(d["survived"])
        capturable_by_delay = {
            delay: (sum(flags) / len(flags) if flags else 0.0) for delay, flags in sorted(by_delay.items())
        }

        lines = [
            "=== Opportunity summary ===",
            f"Opportunities/hour: {opportunities_per_hour:.1f}",
            f"Median net spread: {median_spread:.4f}%",
            f"P95 net spread: {p95_spread:.4f}%",
            "Capturable fraction by latency bucket:",
        ]
        for delay, fraction in capturable_by_delay.items():
            lines.append(f"  after {delay * 1000:.0f}ms: {fraction * 100:.1f}%")

        avg_capture_rate = (sum(capturable_by_delay.values()) / len(capturable_by_delay)) if capturable_by_delay else 0.0
        avg_spread_pct = sum(r["net_profit_pct"] for r in records) / len(records)
        turnover_per_hour = opportunities_per_hour

        implied_pnl_per_hour_per_1000usd = 1000.0 * avg_capture_rate * (avg_spread_pct / 100.0) * turnover_per_hour
        lines.append("")
        lines.append("=== Implied PnL model (profit_rate = capital x capture_rate x turnover) ===")
        lines.append(f"Observed avg capture rate: {avg_capture_rate * 100:.1f}%")
        lines.append(f"Observed avg net spread: {avg_spread_pct:.4f}%")
        lines.append(f"Observed turnover: {turnover_per_hour:.1f} opportunities/hour")
        lines.append(f"Implied PnL per $1,000 deployed: ${implied_pnl_per_hour_per_1000usd:.4f}/hour")

        if target_usd_per_hour is not None:
            denom = avg_capture_rate * (avg_spread_pct / 100.0) * turnover_per_hour
            if denom > 0:
                required_capital = target_usd_per_hour / denom
                lines.append("")
                lines.append(f"=== Capital required for ${target_usd_per_hour:.2f}/hour target ===")
                lines.append(f"At the observed capture rate and spread: ${required_capital:,.2f} deployed capital")
            else:
                lines.append("")
                lines.append("Cannot size a capital requirement: observed capture rate or spread is zero.")

        return "\n".join(lines)
