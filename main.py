"""Async entry point: connect to every venue, then run feed ingestion and
strategy detection as two independent loops (see core/feed_manager.py for
why they're kept separate), routing/risk-checking/executing whatever
each detection tick finds.

Run modes (`ARB_MODE`): `monitor` (default, log only), `paper` (simulate
fills), `live` (fire real orders against pre-funded inventory -- see
execution/executor.py's module docstring before ever using this).
"""

from __future__ import annotations

import asyncio
import logging

try:
    import uvloop

    uvloop.install()
except ImportError:
    pass  # uvloop is Linux/macOS-only; falls back to the default asyncio loop

from analytics.metrics import MetricsRegistry
from analytics.recorder import OpportunityRecorder
from config.settings import (
    MAX_TRADE_USD,
    METRICS_HTTP_PORT,
    METRICS_LOG_DUMP_INTERVAL_SEC,
    MODE,
    REST_POLL_INTERVAL_SEC,
    TIER_CONFIG,
)
from config.universe import build_tradeable_symbols, tier_of
from config.venues import ALL_CEX_IDS
from core.book import BookStore
from core.feed_manager import FeedManager
from core.market_state import MarketState
from core.rest_manager import RestManager
from execution.executor import Executor
from execution.inventory import InventoryManager
from execution.reconciler import Reconciler
from execution.router import Router
from risk.manager import RiskManager
from strategies.base import Opportunity, Strategy
from strategies.basis_carry import BasisCarryStrategy
from strategies.calendar_spread import CalendarSpreadStrategy
from strategies.cex_dex import CexDexStrategy
from strategies.cross_exchange import CrossExchangeStrategy
from strategies.cross_quote import CrossQuoteStrategy
from strategies.dex_dex import DexDexStrategy
from strategies.funding_rate import FundingRateStrategy
from strategies.latency_arb import LatencyArbStrategy
from strategies.maker_rebate import MakerRebateStrategy
from strategies.multi_leg import MultiLegStrategy
from strategies.perp_perp import PerpPerpStrategy
from strategies.stablecoin_depeg import StablecoinDepegStrategy
from strategies.wrapped_asset import WrappedAssetStrategy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

MIN_CONNECTED_VENUES = 2
PAPER_MODE_SEED_BALANCE = 1_000_000.0


def build_strategies() -> list[Strategy]:
    """Instantiate every strategy at its tier-1 profit threshold.

    Cross-exchange re-applies a per-symbol tier threshold each tick (see
    `run`); the rest use their own strategy-appropriate default, which is
    itself sourced from config.settings rather than hardcoded here.
    """
    return [
        CrossExchangeStrategy(min_profit_pct=TIER_CONFIG["tier1"].min_profit_pct, max_trade_usd=MAX_TRADE_USD),
        CexDexStrategy(max_trade_usd=MAX_TRADE_USD),
        DexDexStrategy(),
        FundingRateStrategy(max_trade_usd=MAX_TRADE_USD),
        BasisCarryStrategy(max_trade_usd=MAX_TRADE_USD),
        CalendarSpreadStrategy(max_trade_usd=MAX_TRADE_USD),
        CrossQuoteStrategy(max_trade_usd=MAX_TRADE_USD),
        StablecoinDepegStrategy(max_trade_usd=MAX_TRADE_USD),
        WrappedAssetStrategy(max_trade_usd=MAX_TRADE_USD),
        PerpPerpStrategy(max_trade_usd=MAX_TRADE_USD),
        MultiLegStrategy(max_trade_usd=MAX_TRADE_USD),
        MakerRebateStrategy(max_trade_usd=MAX_TRADE_USD),
        LatencyArbStrategy(max_trade_usd=MAX_TRADE_USD),
        # TriangularStrategy and StatisticalArbStrategy are intentionally
        # left out of the default run: triangular needs no extra wiring
        # (it's included via build_strategies below in a full deployment),
        # and statistical needs an explicit, curated pairs list -- see
        # strategies/statistical.py's constructor.
    ]


def _rescan_net_profit_pct(book_store: BookStore, opportunity: Opportunity) -> float | None:
    """Recompute an opportunity's approximate net profit % from live books.

    Used by the decay-curve check (analytics/recorder.py). This is an
    approximation: it re-derives buy/sell venue from `detail` and
    recomputes the top-of-book spread fresh, using the same fee figures
    the opportunity was originally priced with (fees don't change tick to
    tick, prices do).
    """
    buy_venue = opportunity.detail.get("buy_venue")
    sell_venue = opportunity.detail.get("sell_venue")
    if not buy_venue or not sell_venue:
        return None
    buy_book = book_store.get(buy_venue, opportunity.symbol)
    sell_book = book_store.get(sell_venue, opportunity.symbol)
    if buy_book is None or sell_book is None:
        return None
    buy_state, sell_state = buy_book.snapshot(), sell_book.snapshot()
    if buy_state.best_ask <= 0:
        return None
    fee_pct = sum(leg.fee for leg in opportunity.legs) * 100.0
    gross_pct = (sell_state.best_bid - buy_state.best_ask) / buy_state.best_ask * 100.0
    return gross_pct - fee_pct


async def run() -> None:
    rest_manager = RestManager(list(ALL_CEX_IDS))
    book_store = BookStore()
    metrics = MetricsRegistry()
    recorder = OpportunityRecorder()
    risk_manager = RiskManager()
    reconciler = Reconciler()
    inventory = InventoryManager()
    router = Router(inventory)
    stop_event = asyncio.Event()
    feed_manager: FeedManager | None = None
    dump_task: asyncio.Task | None = None

    try:
        connected = await rest_manager.connect_all()
        if len(connected) < MIN_CONNECTED_VENUES:
            logger.error("Only %d venue(s) connected (need >= %d); aborting", len(connected), MIN_CONNECTED_VENUES)
            return

        # Build the tradeable symbol list per venue from what it actually
        # lists, intersected with the configured universe -- never hardcoded.
        symbols: set[str] = set()
        for venue_id in connected:
            client = rest_manager.clients[venue_id]
            tiered_symbols = build_tradeable_symbols(client.markets)
            symbols.update(s.symbol for s in tiered_symbols)
        symbol_list = sorted(symbols)
        logger.info("Tradeable universe: %d symbols across %d venues", len(symbol_list), len(connected))

        if MODE == "paper":
            for venue_id in connected:
                for symbol in symbol_list:
                    base, quote = symbol.split("/")
                    inventory.set_balance(venue_id, base, PAPER_MODE_SEED_BALANCE)
                    inventory.set_balance(venue_id, quote, PAPER_MODE_SEED_BALANCE)
        elif MODE == "live":
            for venue_id in connected:
                try:
                    balance = await rest_manager.fetch_balance(venue_id)
                    for asset, amounts in balance.get("free", {}).items():
                        inventory.set_balance(venue_id, asset, float(amounts or 0.0))
                except Exception as exc:
                    logger.warning("Could not fetch live balance for %s: %s", venue_id, exc)

        feed_manager = FeedManager(book_store, rest_manager)
        await feed_manager.start(connected, symbol_list, REST_POLL_INTERVAL_SEC)

        strategies = build_strategies()
        metrics.start_http_server(METRICS_HTTP_PORT)
        dump_task = asyncio.create_task(metrics.periodic_dump(METRICS_LOG_DUMP_INTERVAL_SEC, stop_event))

        executor = Executor(rest_manager, risk_manager, reconciler, book_store, mode=MODE)
        logger.info("Running in %r mode with %d strategies", MODE, len(strategies))

        while not stop_event.is_set():
            with metrics.timed_stage("state_to_detect"):
                market_state = MarketState(book_store=book_store, symbols=symbol_list)

                all_opportunities: list[Opportunity] = []
                for strategy in strategies:
                    if isinstance(strategy, CrossExchangeStrategy):
                        # Tier-driven threshold: scan tier1 symbols at the tier1
                        # bar, then re-scan at tier3's looser bar for everything
                        # else, since a flat threshold would either miss cheap
                        # mid-cap edges or flood majors with noise.
                        tier1_symbols = [s for s in symbol_list if tier_of(s.split("/")[0]) == "tier1"]
                        strategy.min_profit_pct = TIER_CONFIG["tier1"].min_profit_pct
                        all_opportunities.extend(
                            strategy.scan(MarketState(book_store=book_store, symbols=tier1_symbols))
                        )
                        rest_symbols = [s for s in symbol_list if s not in tier1_symbols]
                        strategy.min_profit_pct = TIER_CONFIG["tier3"].min_profit_pct
                        all_opportunities.extend(
                            strategy.scan(MarketState(book_store=book_store, symbols=rest_symbols))
                        )
                    else:
                        all_opportunities.extend(strategy.scan(market_state))

            for opportunity in all_opportunities:
                metrics.record_hit(opportunity.strategy, hit=True)
                opportunity_id = recorder.record(opportunity)
                recorder.schedule_decay_checks(
                    opportunity_id, opportunity, rescan_fn=lambda o=opportunity: _rescan_net_profit_pct(book_store, o)
                )

            with metrics.timed_stage("detect_to_decision"):
                if MODE == "monitor":
                    to_execute = all_opportunities
                else:
                    to_execute = router.select(all_opportunities)

            for opportunity in to_execute:
                await executor.handle(opportunity)
                if executor.trade_log:
                    metrics.record_pnl(opportunity.strategy, executor.trade_log[-1].pnl_usd or 0.0)

            recorder.flush()
            await asyncio.sleep(REST_POLL_INTERVAL_SEC)

    except KeyboardInterrupt:
        logger.info("Interrupted by user, shutting down...")
    finally:
        stop_event.set()
        if feed_manager is not None:
            await feed_manager.stop()
        if dump_task is not None:
            dump_task.cancel()
            await asyncio.gather(dump_task, return_exceptions=True)
        metrics.stop_http_server()
        recorder.flush()
        logger.info(recorder.generate_summary_report())
        await rest_manager.close_all()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()
