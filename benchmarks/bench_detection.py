"""Benchmark: detection throughput at full universe size.

Generates a synthetic market snapshot at (or above) the engine's design
target -- 50+ coins x 15+ venues -- and measures each strategy's
`scan()` throughput in scans/second. Run directly:

    python -m benchmarks.bench_detection

Numbers are reported in the README. They will vary with hardware; what
matters is the relative cost of vectorized (numpy matrix) strategies vs.
graph-search strategies (triangular, multi-leg), which is architecturally
the more interesting comparison than the absolute numbers on any one
machine.
"""

from __future__ import annotations

import random
import time
import warnings

from config.universe import ALL_BASE_ASSETS
from config.venues import ALL_CEX_IDS, ALL_DEX_IDS
from core.book import BookStore
from core.market_state import MarketState, PoolState
from core.types import FundingRate, FuturesQuote
from strategies.base import Strategy
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
from strategies.statistical import StatisticalArbStrategy
from strategies.triangular import TriangularStrategy
from strategies.wrapped_asset import WrappedAssetStrategy

QUOTE_ASSETS = ("USDT", "USDC")


def build_synthetic_market_state(seed: int = 42) -> MarketState:
    """Build a synthetic full-universe snapshot: 70 coins x 20 CEX venues,
    plus perp books, funding rates, futures curves, and DEX pools for the
    non-spot strategies."""
    rng = random.Random(seed)
    book_store = BookStore()
    symbols: list[str] = []

    base_prices = {base: rng.uniform(0.01, 60_000.0) for base in ALL_BASE_ASSETS}

    for quote in QUOTE_ASSETS:
        for base in ALL_BASE_ASSETS:
            symbol = f"{base}/{quote}"
            symbols.append(symbol)
            fair_price = base_prices[base]
            for venue_id in ALL_CEX_IDS:
                venue_price = fair_price * (1.0 + rng.uniform(-0.004, 0.004))
                spread = venue_price * 0.0005
                bid = venue_price - spread / 2
                ask = venue_price + spread / 2
                size = rng.uniform(0.5, 50.0)
                book_store.get_or_create(venue_id, symbol).replace(
                    bids=[(bid, size), (bid * 0.999, size * 2)],
                    asks=[(ask, size), (ask * 1.001, size * 2)],
                )

    # Perp books (subset of symbols, subset of derivatives venues).
    perp_symbols = symbols[:20]
    derivatives_venues = [v for v in ALL_CEX_IDS][:10]
    for symbol in perp_symbols:
        base = symbol.split("/")[0]
        perp_symbol = f"{symbol}-PERP"
        for venue_id in derivatives_venues:
            fair_price = base_prices[base] * (1.0 + rng.uniform(-0.004, 0.004))
            spread = fair_price * 0.0005
            size = rng.uniform(1.0, 20.0)
            book_store.get_or_create(venue_id, perp_symbol).replace(
                bids=[(fair_price - spread / 2, size)], asks=[(fair_price + spread / 2, size)]
            )

    market_state = MarketState(book_store=book_store, symbols=symbols)

    for symbol in perp_symbols:
        for venue_id in derivatives_venues:
            market_state.funding_rates[(venue_id, symbol)] = FundingRate(
                venue_id, symbol, rate=rng.uniform(-0.002, 0.002), interval_hours=8.0, next_funding_ts=time.time() + 3600
            )

    now = time.time()
    for symbol in symbols[:10]:
        base = symbol.split("/")[0]
        spot = base_prices[base]
        for venue_id in ALL_CEX_IDS[:5]:
            for days in (30, 90, 180):
                price = spot * (1.0 + rng.uniform(-0.02, 0.05))
                market_state.futures_quotes[(venue_id, symbol, now + days * 86400)] = FuturesQuote(
                    venue_id, symbol, now + days * 86400, price, spot
                )

    for _i, symbol in enumerate(symbols[:15]):
        base = symbol.split("/")[0]
        fair_price = base_prices[base]
        for dex_id in list(ALL_DEX_IDS)[:3]:
            reserve_base = rng.uniform(100, 10_000)
            reserve_quote = reserve_base * fair_price * (1.0 + rng.uniform(-0.01, 0.01))
            market_state.dex_pools[f"{dex_id}:{symbol}"] = PoolState(
                dex_id=dex_id, chain="ethereum", symbol=symbol, reserve_base=reserve_base, reserve_quote=reserve_quote, fee=0.003
            )

    return market_state


def benchmark_strategy(strategy: Strategy, market_state: MarketState, iterations: int = 20) -> dict:
    # Warm-up (JIT-ish numpy allocator warmup, page cache warmup).
    strategy.scan(market_state)

    start = time.perf_counter()
    total_opportunities = 0
    for _ in range(iterations):
        total_opportunities += len(strategy.scan(market_state))
    elapsed = time.perf_counter() - start

    avg_scan_sec = elapsed / iterations
    return {
        "strategy": strategy.name,
        "scans_per_sec": (1.0 / avg_scan_sec) if avg_scan_sec > 0 else float("inf"),
        "avg_scan_ms": avg_scan_sec * 1000.0,
        "avg_opportunities_per_scan": total_opportunities / iterations,
    }


def main() -> None:
    # The statistical-arb warm-up below feeds the same static snapshot
    # repeatedly (there's no real time-series in this synthetic benchmark),
    # which makes numpy's polyfit ill-conditioned on a near-constant series.
    # That's a benchmark-harness artifact, not a strategy bug -- silence it.
    warnings.filterwarnings("ignore", message="Polyfit may be poorly conditioned")

    market_state = build_synthetic_market_state()
    num_venues = len(market_state.book_store.all_venues())
    num_symbols = len(market_state.symbols)
    print(f"Synthetic universe: {num_symbols} symbols x {num_venues} venues "
          f"({num_symbols * num_venues} books)")
    print()

    strategies: list[Strategy] = [
        CrossExchangeStrategy(min_profit_pct=0.05),
        TriangularStrategy(min_profit_pct=0.05, max_cycle_length=4),
        CexDexStrategy(min_profit_pct=0.05),
        DexDexStrategy(min_profit_pct=0.05),
        FundingRateStrategy(min_annualized_pct=1.0),
        BasisCarryStrategy(min_annualized_pct=1.0),
        CalendarSpreadStrategy(min_divergence_pct=0.1),
        CrossQuoteStrategy(min_profit_pct=0.05),
        StablecoinDepegStrategy(depeg_threshold_pct=0.01),
        WrappedAssetStrategy(),
        PerpPerpStrategy(min_profit_pct=0.05),
        StatisticalArbStrategy(pairs=[(market_state.symbols[0], market_state.symbols[1])], lookback=10),
        MultiLegStrategy(min_profit_pct=0.1, max_path_length=3),
        MakerRebateStrategy(min_profit_pct=0.02),
        LatencyArbStrategy(min_profit_pct=0.05, min_lag_sec=0.0),
    ]

    results = []
    for strategy in strategies:
        # StatisticalArbStrategy needs `lookback` ticks of history before it
        # does real work; feed it a few extra scans so the benchmark measures
        # steady-state cost, not the cold-start no-op.
        if isinstance(strategy, StatisticalArbStrategy):
            for _ in range(strategy.lookback):
                strategy.scan(market_state)
        results.append(benchmark_strategy(strategy, market_state))

    print(f"{'strategy':<20} {'scans/sec':>12} {'avg ms/scan':>12} {'avg opps/scan':>14}")
    for r in results:
        print(f"{r['strategy']:<20} {r['scans_per_sec']:>12.1f} {r['avg_scan_ms']:>12.3f} {r['avg_opportunities_per_scan']:>14.2f}")


if __name__ == "__main__":
    main()
