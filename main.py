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
import os

try:
    import uvloop

    uvloop.install()
except ImportError:
    pass  # uvloop is Linux/macOS-only; falls back to the default asyncio loop

import uvicorn

from analytics.metrics import MetricsRegistry
from analytics.recorder import OpportunityRecorder
from config.settings import (
    DASHBOARD_ENABLED,
    DASHBOARD_HOST,
    DASHBOARD_PASSWORD,
    DASHBOARD_PORT,
    DASHBOARD_USERNAME,
    MAX_POLLED_SYMBOLS,
    MAX_TRADE_USD,
    METRICS_HTTP_PORT,
    METRICS_LOG_DUMP_INTERVAL_SEC,
    MIN_VENUES_PER_SYMBOL,
    MODE,
    REST_POLL_INTERVAL_SEC,
    TIER_CONFIG,
)
from config.universe import build_tradeable_symbols, select_pollable_symbols, tier_of
from config.venues import ALL_CEX_IDS, min_order_usd_for
from core.book import BookStore
from core.control import ControlState
from core.feed_manager import FeedManager
from core.market_state import MarketState
from core.rest_manager import RestManager
from dashboard.server import create_app
from dashboard.state import Broadcaster, build_snapshot
from execution.executor import Executor
from execution.inventory import InventoryManager
from execution.reconciler import Reconciler
from execution.router import Router
from risk.manager import RiskManager
from strategies.base import Opportunity, Strategy
from strategies.cex_dex import CexDexStrategy
from strategies.cross_exchange import CrossExchangeStrategy
from strategies.cross_quote import CrossQuoteStrategy
from strategies.dex_dex import DexDexStrategy
from strategies.latency_arb import LatencyArbStrategy
from strategies.maker_rebate import MakerRebateStrategy
from strategies.perp_perp import PerpPerpStrategy
from strategies.stablecoin_depeg import StablecoinDepegStrategy
from strategies.triangular import TriangularStrategy
from strategies.wrapped_asset import WrappedAssetStrategy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

MIN_CONNECTED_VENUES = 2

# Paper-mode starting balance per venue, in USD, split evenly between each
# symbol's base and quote asset.
#
# This used to be a flat 1,000,000 *units* of every asset on every venue --
# ~$200M of SOL on a single venue. The effect was that `Router.select`'s
# inventory check, the thing that stops two strategies spending the same
# balance, could never fail, and inventory never ran lopsided enough to
# need a transfer. Paper mode reported profits that quietly assumed
# unlimited capital, perfectly distributed, forever.
#
# A realistic figure makes paper mode answer the question that actually
# matters: does this still make money when the same euros have to be in
# the right place at the right time?
PAPER_SEED_USD_PER_VENUE: float = float(os.getenv("PAPER_SEED_USD_PER_VENUE", "2000"))


def build_strategies() -> list[Strategy]:
    """Instantiate every strategy at its tier-1 profit threshold.

    Cross-exchange re-applies a per-symbol tier threshold each tick (see
    `run`); the rest use their own strategy-appropriate default, which is
    itself sourced from config.settings rather than hardcoded here.
    """
    return [
        CrossExchangeStrategy(min_profit_pct=TIER_CONFIG["tier1"].min_profit_pct, max_trade_usd=MAX_TRADE_USD),
        # CexDexStrategy and DexDexStrategy size trades via `probe_size_base`
        # (a fixed base-asset amount), not a USD cap -- they don't take
        # max_trade_usd, unlike every other strategy here.
        CexDexStrategy(),
        DexDexStrategy(),
        CrossQuoteStrategy(max_trade_usd=MAX_TRADE_USD),
        StablecoinDepegStrategy(max_trade_usd=MAX_TRADE_USD),
        WrappedAssetStrategy(max_trade_usd=MAX_TRADE_USD),
        PerpPerpStrategy(max_trade_usd=MAX_TRADE_USD),
        MakerRebateStrategy(max_trade_usd=MAX_TRADE_USD),
        LatencyArbStrategy(max_trade_usd=MAX_TRADE_USD),
        # 3 legs, not config's MAX_CYCLE_LENGTH of 5. Cycle-search cost grows
        # exponentially with length while the edge needed to clear fees grows
        # only linearly: measured at production universe size, length 5 costs
        # ~9x the CPU of length 3 (206ms vs 23ms per tick) and needs ~1.0% of
        # edge to break even at 0.20% taker per leg, versus ~0.6% for 3 legs.
        # A 1% single-venue triangular edge on liquid pairs effectively does
        # not occur, so the extra hops buy cost rather than opportunities.
        TriangularStrategy(min_profit_pct=TIER_CONFIG["tier1"].min_profit_pct, max_cycle_length=3),
        # Deliberately NOT in the default run:
        #
        # * FundingRateStrategy, BasisCarryStrategy, CalendarSpreadStrategy
        #   read `MarketState.funding_rates` / `.futures_quotes`, which
        #   nothing in this loop populates -- only the benchmark and unit
        #   tests fill them synthetically. Running them against empty data
        #   burns CPU every tick to correctly find nothing, which on a
        #   small host is capacity taken from the strategies that do have
        #   live data. Re-add them together with a real funding-rate /
        #   futures-curve poller, not before.
        #
        # * MultiLegStrategy only emits routes containing at least one
        #   `transfer` edge (it skips pure single-venue cycles as
        #   triangular's job), and every transfer carries
        #   `DEFAULT_TRANSFER_LATENCY_SEC` -- 15 minutes of withdrawal,
        #   confirmation and deposit. A spread has to survive that entire
        #   window to be capturable, which is the exact thing
        #   execution/executor.py's module docstring explains does not
        #   happen. Two concrete costs to leaving it on: it was ~84% of
        #   every detection tick's CPU at production universe size, and in
        #   `paper` mode its routes fill instantly with no transfer delay
        #   simulated, inflating paper PnL with trades that cannot happen
        #   for real. Re-enable it only as an inventory-rebalancing
        #   planner, not as an arbitrage signal.
        #
        # * StatisticalArbStrategy needs an explicit, curated pairs list
        #   (see its constructor) and carries real directional risk -- it
        #   is not arbitrage. See README "Before running with real money".
    ]


_STABLE_USD_PROXIES = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USD", "EUR"}


def _usd_price_of(asset: str, book_store: BookStore) -> float | None:
    """Best-effort USD value of one unit of `asset` from live books."""
    if asset in _STABLE_USD_PROXIES:
        return 1.0
    for quote in ("USDT", "USDC", "USD"):
        for book in book_store.all_for_symbol(f"{asset}/{quote}"):
            state = book.snapshot()
            bid, ask = state.best_bid, state.best_ask
            if bid > 0 and ask < float("inf"):
                return (bid + ask) / 2.0
    return None


def _seed_paper_inventory(
    connected: list[str],
    symbol_list: list[str],
    inventory: InventoryManager,
    book_store: BookStore,
) -> None:
    """Give each venue `PAPER_SEED_USD_PER_VENUE` spread over the assets it trades.

    Sized in USD and converted to units at the live mid, rather than a
    flat unit count per asset -- 1,000,000 units means $1M of USDT and
    $200M of SOL, which is not a portfolio anyone has and quietly removes
    every capital constraint from the simulation.
    """
    for venue_id in connected:
        assets = {a for symbol in symbol_list for a in symbol.split("/")}
        priced = {a: p for a in assets if (p := _usd_price_of(a, book_store)) is not None}
        if not priced:
            continue
        usd_each = PAPER_SEED_USD_PER_VENUE / len(priced)
        for asset, price in priced.items():
            inventory.set_balance(venue_id, asset, usd_each / price)

    logger.info(
        "Seeded paper inventory: $%.0f per venue across %d venues (override with PAPER_SEED_USD_PER_VENUE)",
        PAPER_SEED_USD_PER_VENUE, len(connected),
    )


async def _seed_inventory_for_mode(
    mode: str,
    connected: list[str],
    symbol_list: list[str],
    inventory: InventoryManager,
    rest_manager: RestManager,
    book_store: BookStore,
) -> None:
    """Seed (or fetch) inventory the first time a mode that needs it is entered.

    `paper` gets a realistic synthetic balance sized in USD (see
    `_seed_paper_inventory`). `live` fetches real free balances via each
    exchange's private API -- this is the one place this engine reads real
    account state, and it only happens once per mode-entry, not every tick.
    """
    if mode == "paper":
        _seed_paper_inventory(connected, symbol_list, inventory, book_store)
    elif mode == "live":
        for venue_id in connected:
            try:
                balance = await rest_manager.fetch_balance(venue_id)
                for asset, amounts in balance.get("free", {}).items():
                    inventory.set_balance(venue_id, asset, float(amounts or 0.0))
            except Exception as exc:
                logger.warning("Could not fetch live balance for %s: %s", venue_id, exc)
        logger.warning("Fetched real account balances for live mode across %d venues", len(connected))


def is_fillable(opportunity: Opportunity) -> bool:
    """Whether `opportunity` has enough displayed size to be worth reporting.

    A venue can quote an attractive top-of-book price with almost nothing
    behind it. The spread is real in the sense that the numbers differ, but
    it is not tradeable: every leg has to clear its venue's minimum order
    notional, and the binding constraint is the largest minimum across the
    route.

    The executor already rejects these (`RiskManager.size_with_depth_check`),
    but only *after* they have been recorded as opportunities -- which puts
    unfillable quotes in the dashboard feed as if they were money, and, worse,
    into the decay curve. That corrupts the single most important number in
    the project: a thin quote nobody wants sits unchanged for seconds, so it
    scores as "survived" every time and drags the capturable fraction toward
    100%, exactly backwards from what it should measure. A real spread is
    taken almost immediately; one that persists is evidence it *cannot* be
    filled, not evidence that it could.

    Filtering at detection keeps both the feed and the decay curve honest.
    """
    if not opportunity.legs:
        return False
    required = max(min_order_usd_for(leg.venue_id) for leg in opportunity.legs)
    return opportunity.max_size_usd >= required


def route_key(opportunity: Opportunity) -> tuple[tuple[str, str, str], ...]:
    """The physical trade an opportunity describes, independent of strategy.

    Two strategies can describe the same trade. `latency_arb` computes
    exactly the same net edge as `cross_exchange` -- same formula, same
    inputs -- and then adds one extra condition (the buy venue's book
    being staler than the sell venue's). Every latency_arb hit is
    therefore, by construction, also a cross_exchange hit. They are not
    two findings that happen to agree; they cannot disagree.

    Left undeduplicated they were each recorded, each executed, and each
    counted, so one real trade produced two entries in the feed and twice
    its PnL -- observed live as identical `+0.162% / $0.81` rows for
    SOL/USDT htx->bingx under both strategy names.
    """
    return tuple(sorted((leg.venue_id, leg.symbol, leg.side) for leg in opportunity.legs))


def dedupe_by_route(opportunities: list[Opportunity]) -> tuple[list[Opportunity], int]:
    """Keep the best-edged opportunity per distinct route.

    Returns the kept opportunities and how many duplicates were dropped.
    """
    best: dict[tuple[tuple[str, str, str], ...], Opportunity] = {}
    duplicates = 0
    for opportunity in opportunities:
        key = route_key(opportunity)
        incumbent = best.get(key)
        if incumbent is None:
            best[key] = opportunity
        else:
            duplicates += 1
            if opportunity.net_profit_pct > incumbent.net_profit_pct:
                best[key] = opportunity
    return list(best.values()), duplicates


def _rescan_net_profit_pct(book_store: BookStore, opportunity: Opportunity) -> float | None:
    """Recompute an opportunity's approximate net profit % from live books.

    Used by the decay-curve check (analytics/recorder.py) -- the metric
    that answers whether a detected spread would still have been there by
    the time it could be traded.

    This walks the opportunity's *own legs* against current top-of-book
    rather than re-deriving a buy/sell venue pair from `detail`. An
    earlier version only handled two-leg routes carrying explicit
    `detail["buy_venue"]`/`["sell_venue"]` keys, which only 4 of the 15
    strategies set; for every other strategy -- including `multi_leg` and
    `cross_quote`, which dominate the live feed -- it returned `None`, and
    the recorder scores `None` as "did not survive". The reported
    capturable fraction was therefore biased sharply downward, counting
    strategies it could not measure as decayed rather than as unmeasured.

    Each leg is treated as a multiplicative conversion on a notional unit
    and chained around the route, which is the same formulation
    `core/graph.py` uses to price cycles in the first place:

        buy  @ ask P -> holding x (1/P) x (1 - fee)
        sell @ bid P -> holding x P x (1 - fee)
        transfer     -> holding x (1 - fee)

    Chaining rather than summing cost/proceeds matters because cycle
    strategies (`multi_leg`, `triangular`, `cross_quote`) don't carry a
    per-leg `size` -- they describe a closed loop of conversions, and a
    notional-weighted sum would read zero for all of them.

    Returns `None` only when a leg genuinely can't be repriced (venue
    dropped out, empty book), which the recorder still treats as decayed.
    """
    if not opportunity.legs:
        return None

    holding = 1.0
    for leg in opportunity.legs:
        if leg.side == "transfer":
            # No book to reprice: a transfer's cost is its withdrawal fee.
            holding *= 1.0 - leg.fee
            continue

        book = book_store.get(leg.venue_id, leg.symbol)
        if book is None:
            return None
        state = book.snapshot()
        price = state.best_ask if leg.side == "buy" else state.best_bid
        if price <= 0 or price == float("inf"):
            return None

        rate = (1.0 / price) if leg.side == "buy" else price
        holding *= rate * (1.0 - leg.fee)

    return (holding - 1.0) * 100.0


async def run() -> None:
    rest_manager = RestManager(list(ALL_CEX_IDS))
    book_store = BookStore()
    metrics = MetricsRegistry()
    recorder = OpportunityRecorder()
    risk_manager = RiskManager()
    reconciler = Reconciler()
    inventory = InventoryManager()
    router = Router(inventory)
    control = ControlState(mode=MODE)
    broadcaster = Broadcaster()
    stop_event = asyncio.Event()
    feed_manager: FeedManager | None = None
    dump_task: asyncio.Task | None = None
    dashboard_task: asyncio.Task | None = None

    try:
        connected = await rest_manager.connect_all()
        if len(connected) < MIN_CONNECTED_VENUES:
            logger.error("Only %d venue(s) connected (need >= %d); aborting", len(connected), MIN_CONNECTED_VENUES)
            return

        # Build the tradeable symbol list per venue from what it actually
        # lists, intersected with the configured universe -- never hardcoded.
        symbols_by_venue: dict[str, set[str]] = {}
        for venue_id in connected:
            client = rest_manager.clients[venue_id]
            tiered_symbols = build_tradeable_symbols(client.markets)
            symbols_by_venue[venue_id] = {s.symbol for s in tiered_symbols}

        listed_total = len({s for listed in symbols_by_venue.values() for s in listed})
        # Spend the REST poll budget on symbols that can actually produce a
        # cross-venue trade, majors first -- see MAX_POLLED_SYMBOLS in
        # config/settings.py for why this is bounded rather than "all of them".
        symbol_list = select_pollable_symbols(
            symbols_by_venue,
            max_symbols=MAX_POLLED_SYMBOLS,
            min_venues=MIN_VENUES_PER_SYMBOL,
        )
        logger.info(
            "Tradeable universe: polling %d of %d listed symbols across %d venues "
            "(symbols on >=%d venues, capped at %d -- see MAX_POLLED_SYMBOLS)",
            len(symbol_list), listed_total, len(connected),
            MIN_VENUES_PER_SYMBOL, MAX_POLLED_SYMBOLS,
        )
        if not symbol_list:
            logger.error(
                "No symbol is listed on >=%d connected venues; nothing to arbitrage. "
                "Check venue connectivity above.", MIN_VENUES_PER_SYMBOL,
            )
            return

        seeded_modes: set[str] = set()
        if control.mode in ("paper", "live"):
            await _seed_inventory_for_mode(control.mode, connected, symbol_list, inventory, rest_manager, book_store)
            seeded_modes.add(control.mode)

        feed_manager = FeedManager(book_store, rest_manager)
        await feed_manager.start(connected, symbol_list, REST_POLL_INTERVAL_SEC)

        strategies = build_strategies()
        metrics.start_http_server(METRICS_HTTP_PORT)
        dump_task = asyncio.create_task(metrics.periodic_dump(METRICS_LOG_DUMP_INTERVAL_SEC, stop_event))

        executor = Executor(rest_manager, risk_manager, reconciler, book_store, mode=control.mode, metrics=metrics)

        if DASHBOARD_ENABLED:
            auth_configured = bool(DASHBOARD_USERNAME and DASHBOARD_PASSWORD)
            if DASHBOARD_HOST not in ("127.0.0.1", "localhost") and not auth_configured:
                logger.critical(
                    "Dashboard is bound to %s with NO LOGIN configured -- anyone who finds this "
                    "IP/port can switch this bot to live and arm real orders. Set DASHBOARD_USERNAME "
                    "and DASHBOARD_PASSWORD in .env and restart before leaving this exposed.",
                    DASHBOARD_HOST,
                )
            app = create_app(
                control, risk_manager, strategies, recorder, metrics, rest_manager, book_store, broadcaster,
                auth_username=DASHBOARD_USERNAME or None, auth_password=DASHBOARD_PASSWORD or None,
            )
            uvicorn_config = uvicorn.Config(app, host=DASHBOARD_HOST, port=DASHBOARD_PORT, log_level="warning")
            uvicorn_server = uvicorn.Server(uvicorn_config)
            dashboard_task = asyncio.create_task(uvicorn_server.serve())
            logger.info(
                "Dashboard: http://%s:%d (mode/risk/strategy controls; live means real orders; auth=%s)",
                DASHBOARD_HOST, DASHBOARD_PORT, "on" if auth_configured else "OFF",
            )

        logger.info("Running in %r mode with %d strategies", control.mode, len(strategies))

        while not stop_event.is_set():
            executor.mode = control.mode
            if control.mode in ("paper", "live") and control.mode not in seeded_modes:
                # Dashboard-driven mode switch into paper/live for the first
                # time this run -- seed/fetch inventory now rather than at
                # startup only, so a switch made mid-run actually has
                # capital to trade against instead of silently filtering
                # every opportunity out for lack of reservable balance.
                await _seed_inventory_for_mode(control.mode, connected, symbol_list, inventory, rest_manager, book_store)
                seeded_modes.add(control.mode)
            new_opportunities: list[Opportunity] = []

            if control.is_active():
                enabled_strategies = [s for s in strategies if control.strategy_enabled.get(s.name, True)]
                with metrics.timed_stage("state_to_detect"):
                    market_state = MarketState(book_store=book_store, symbols=symbol_list)

                    all_opportunities: list[Opportunity] = []
                    for strategy in enabled_strategies:
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

                # Drop quotes with no real size behind them before they reach
                # the feed or the decay curve -- see `is_fillable`.
                fillable: list[Opportunity] = []
                for opportunity in all_opportunities:
                    if is_fillable(opportunity):
                        fillable.append(opportunity)
                    else:
                        metrics.record_rejection("too_thin_to_fill")
                all_opportunities = fillable

                # Collapse the same physical trade found by several
                # strategies into one -- see `route_key`. This has to
                # happen before recording, or the feed, the decay curve
                # and PnL all count one trade more than once.
                all_opportunities, duplicate_count = dedupe_by_route(all_opportunities)
                for _ in range(duplicate_count):
                    metrics.record_rejection("duplicate_route")

                for opportunity in all_opportunities:
                    metrics.record_hit(opportunity.strategy, hit=True)
                    opportunity_id = recorder.record(opportunity)
                    # Stashed on the opportunity itself (not a side dict) so it
                    # travels naturally into the execution loop below, where a
                    # trade's realized PnL gets attached back to this exact
                    # detected opportunity for the dashboard to display.
                    opportunity.detail["recorder_id"] = opportunity_id
                    recorder.schedule_decay_checks(
                        opportunity_id, opportunity, rescan_fn=lambda o=opportunity: _rescan_net_profit_pct(book_store, o)
                    )
                new_opportunities = all_opportunities

                with metrics.timed_stage("detect_to_decision"):
                    if control.mode == "monitor":
                        to_execute = all_opportunities
                    else:
                        to_execute = router.select(all_opportunities)

                for opportunity in to_execute:
                    trades_before = len(executor.trade_log)
                    await executor.handle(opportunity)
                    # Compare trade_log length rather than truthiness -- the
                    # list is non-empty forever after the first trade, so a
                    # bare `if executor.trade_log:` would misattribute an
                    # earlier trade's PnL to every later opportunity that the
                    # risk manager, router, or profitability re-check skips.
                    if len(executor.trade_log) > trades_before:
                        # Move the balance the fill actually moved, so the
                        # next tick sees the inventory this trade consumed.
                        router.settle_fill(opportunity)
                        last_trade = executor.trade_log[-1]
                        pnl = last_trade.pnl_usd or 0.0
                        metrics.record_pnl(opportunity.strategy, pnl)
                        recorder_id = opportunity.detail.get("recorder_id")
                        if recorder_id is not None:
                            recorder.attach_trade_result(recorder_id, pnl, control.mode)
                    else:
                        # The executor rejected it (edge gone, below venue
                        # minimum). `router.select` had already locked the
                        # balance; without this it stays locked forever and
                        # the engine slowly starves itself of inventory.
                        router.release_unfilled(opportunity)

                recorder.flush()

            if DASHBOARD_ENABLED:
                await broadcaster.publish(
                    {
                        "type": "tick",
                        "data": {
                            # all_opportunity_records is a deque (bounded rolling
                            # history, see analytics/recorder.py) -- doesn't
                            # support slicing directly.
                            "opportunities": list(recorder.all_opportunity_records)[-len(new_opportunities):] if new_opportunities else [],
                            "stats": build_snapshot(control, risk_manager, strategies, rest_manager, book_store, metrics),
                        },
                    }
                )

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
        if dashboard_task is not None:
            dashboard_task.cancel()
            await asyncio.gather(dashboard_task, return_exceptions=True)
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
