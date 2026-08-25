# crypto-arb-engine

A high-performance, multi-strategy crypto arbitrage **detection and
execution engine**. It scans 15+ CEX venues and 8+ DEX venues across 6
chains, over a 70+ coin tiered universe, for 15 distinct categories of
arbitrage, and can monitor, paper-trade, or live-execute what it finds.

Latency, throughput, and correctness of PnL accounting are treated as
first-class requirements throughout — see [Performance](#performance)
and [Profitability instrumentation](#profitability-instrumentation).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in real API keys / RPC URLs
```

## Environment configuration

- `ARB_MODE`: `monitor` (default) | `paper` | `live`.
- CEX credentials: `{EXCHANGE_ID}_API_KEY` / `_SECRET` / `_PASSPHRASE`, uppercased ccxt id (see `config/venues.py`).
- DEX hot wallet: `DEX_HOT_WALLET_ADDRESS`, `DEX_HOT_WALLET_PRIVATE_KEY` — **use a dedicated wallet with limited funds, never a main wallet.**
- RPC endpoints per chain: `ETH_RPC_URL`, `ARBITRUM_RPC_URL`, `BASE_RPC_URL`, `POLYGON_RPC_URL`, `BSC_RPC_URL`, `SOLANA_RPC_URL`.
- Aggregator API keys (optional): `ONEINCH_API_KEY`, `ZEROX_API_KEY`, `JUPITER_API_KEY`, `ODOS_API_KEY`.
- Risk overrides: `MAX_TRADE_USD`, `DAILY_LOSS_LIMIT_USD`, `MAX_TRADES_PER_DAY`, `MAX_CONSECUTIVE_FAILURES`.

All credentials load via `python-dotenv` from a local `.env`. Nothing is hardcoded.

## Running

```bash
ARB_MODE=monitor python main.py   # log only, no orders
ARB_MODE=paper python main.py     # simulate fills, track PnL
ARB_MODE=live python main.py      # real orders against pre-funded inventory -- read the section below first
```

A `/metrics` HTTP endpoint (`METRICS_HTTP_PORT`, default 9100) serves a JSON snapshot of latency histograms, hit rates, and PnL attribution; the same snapshot is also logged periodically.

## Project structure

```
config/     tunables, tiered asset universe, CEX/DEX venue definitions
core/       order book state, feed multiplexer, REST fallback, currency graph, typed structs, market snapshot
strategies/ 15 arbitrage strategies, one file each, all implementing strategies.base.Strategy
execution/  router, monitor/paper/live executor, inventory tracking, one-sided-fill reconciler
risk/       declarative limits + enforcement (capital, exposure, circuit breakers)
analytics/  latency/hit-rate/PnL metrics, parquet opportunity recorder with decay-curve tracking
benchmarks/ synthetic full-universe detection throughput benchmark
tests/      pytest suite, one module per strategy + core + execution/risk, plus hypothesis property tests
main.py     async entry point
```

## Asset universe and venues

`config/universe.py` defines a 4-tier universe (10 majors, 20 large-caps, 30 mid-caps, plus 7 stablecoins and 10 wrapped/LST assets — 70+ base assets total) with per-tier poll intervals and profit thresholds (`config/settings.TIER_CONFIG`). The *tradeable symbol list* is never hardcoded as pair strings: `build_tradeable_symbols` intersects each venue's actual listed markets (from `load_markets()`) with this universe, across 5 quote assets (USDT, USDC, BTC, ETH, EUR).

`config/venues.py` defines 20 CEX venues (binance, coinbase, kraken, bybit, okx, kucoin, bitget, gate, mexc, htx, bitfinex, bitstamp, gemini, bingx, cryptocom, bitmart, lbank, phemex, woo, deribit) and 13 DEX venues across 6 chains (Ethereum, BSC, Solana, Arbitrum, Base, Polygon), each with taker/maker fees, withdrawal fees, minimum order sizes, rate limits, and websocket support flags.

## Strategies

All 15 strategies implement `strategies.base.Strategy.scan(market_state) -> list[Opportunity]` and run concurrently against one shared `MarketState` snapshot per detection tick.

| # | Strategy | File | Summary |
|---|----------|------|---------|
| 1 | Cross-exchange (spatial) | `cross_exchange.py` | Buy low ask on venue A, sell high bid on venue B, vectorized as one numpy matrix op across all venue pairs. Needs pre-funded inventory on both venues. |
| 2 | Triangular | `triangular.py` | Models one venue as a currency graph (`core/graph.py`), finds negative-weight cycles (Bellman-Ford) and bounded-length cycles (DFS, configurable up to 5 legs) in one pass instead of brute-forcing combinations. |
| 3 | CEX-DEX | `cex_dex.py` | CEX book vs. on-chain AMM price, pricing in gas (USD), swap fee, price impact at size, MEV/priority fee. Needs inventory on both the CEX and a hot wallet. |
| 4 | DEX-DEX | `dex_dex.py` | Same asset, two AMMs. Same-chain is atomic (optionally flash-loan-funded, but the on-chain repay contract is out of scope — this bot only detects and constructs calldata); cross-chain prices in bridge fee/latency as risk. Implements constant-product and a simplified concentrated-liquidity model directly, so pricing against cached reserves needs no network round-trip. |
| 5 | Funding rate | `funding_rate.py` | Delta-neutral: short perp / long spot (or reverse) to collect funding, ranked by annualized rate across venues. |
| 6 | Basis / cash-and-carry | `basis_carry.py` | Buy spot, short a rich dated future (or reverse), annualized across every listed expiry and venue. |
| 7 | Calendar spread | `calendar_spread.py` | Compares each expiry pair's implied marginal forward rate against the venue's own curve average; trades the outlier. |
| 8 | Cross-quote | `cross_quote.py` | Same base, different stablecoin quotes on one venue (BTC/USDT vs BTC/USDC) — cheap to scan, often overlooked. |
| 9 | Stablecoin depeg | `stablecoin_depeg.py` | CEX stable-stable pairs and DEX stable pools vs. $1.00, with a configurable depeg threshold **and a hard kill-switch** — a severe depeg halts trading on that pair rather than being treated as an opportunity. |
| 10 | Wrapped / LST asset | `wrapped_asset.py` | WBTC/BTC, stETH/ETH, mSOL/SOL, etc. The "fair" deviation band widens with each asset's real redemption latency (instant unwrap vs. multi-day withdrawal queue); nothing is flagged while a redemption path is marked closed. |
| 11 | Perp-perp | `perp_perp.py` | Same perp, two venues, delta-neutral; funding-rate differential surfaces as bonus context, not the entry gate. |
| 12 | Statistical / pairs | `statistical.py` | **Not true arbitrage** — cointegration-gated (rolling ADF test), z-score entry/exit/stop-loss mean reversion between correlated assets. Directional risk; can lose money. |
| 13 | Multi-leg / composite | `multi_leg.py` | Currency graph spanning every venue, with inter-venue transfer edges weighted by withdrawal fee and latency, to find composite routes (e.g. triangular on A, then cross-exchange to B) that beat any single strategy. |
| 14 | Maker-rebate | `maker_rebate.py` | Prices taker-taker, maker-taker, and maker-maker fill paths; flags spreads that only clear with a passive (maker) leg, flagging the fill risk that comes with it. |
| 15 | Latency / stale-quote | `latency_arb.py` | Flags a cross-exchange spread where the cheap side's book is also measurably staler (an update-recency gap). **Most latency-competitive category here — see the caveats below.** |

## Performance

- **uvloop** as the event loop policy (falls back cleanly on platforms without it).
- **orjson** for the `/metrics` JSON endpoint and other hot-path serialization.
- **Vectorized detection**: `cross_exchange`, `perp_perp`, `maker_rebate`, and `latency_arb` compute the full venue x venue spread matrix as one numpy operation instead of a Python double loop.
- **Lock-free order book reads**: `core/book.py`'s `OrderBook` swaps an immutable `BookState` reference on each update; readers never see a torn state and never need a lock (see that module's docstring for why this is safe under the GIL).
- **Feed/detect separation**: `core/feed_manager.py` only ever writes into `BookStore`; strategies only ever read from it. A slow detection pass cannot block ingestion.
- **Precomputed statics**: fee tables, the universe, and venue metadata are all loaded once at startup (`config/`), never recomputed per tick.
- **msgspec.Struct** (with `gc=False`) for the hottest per-tick objects (`Quote`, `OpportunitySignal`); `slots=True` dataclasses everywhere else.
- **Per-stage latency histograms** (`time.perf_counter_ns()`) for feed→state, state→detect, detect→decision, decision→ack, each with a configurable budget (`config.settings.LATENCY_BUDGETS`) that logs a warning when exceeded.

### Benchmark numbers

Measured with `python -m benchmarks.bench_detection` on a synthetic snapshot at **140 symbols x 20 venues (2,800 order books)** — above the 50-coin x 15-venue design target. Numbers are from one run on this machine; absolute throughput will vary by hardware, but the *relative* cost of vectorized-matrix strategies vs. graph-search strategies is the architecturally interesting comparison:

```
strategy                scans/sec  avg ms/scan  avg opps/scan
cross_exchange               21.3       46.956        6211.00
triangular                 1087.2        0.920           0.00
cex_dex                     334.5        2.990           0.00
dex_dex                    9481.9        0.105          20.00
funding_rate              11772.9        0.085           0.00
basis_carry                2240.9        0.446         140.00
calendar_spread            2312.0        0.433          98.00
cross_quote                  94.1       10.627           0.00
stablecoin_depeg            460.8        2.170           0.00
wrapped_asset               674.0        1.484           0.00
perp_perp                   609.6        1.641           0.00
statistical                5712.5        0.175           0.00
multi_leg                  1013.8        0.986           0.00
maker_rebate                 87.2       11.474           0.00
latency_arb                  81.2       12.321           0.00
```

`cross_exchange` is the slowest per-scan despite being fully vectorized — on this synthetic snapshot (every venue quoting every symbol with a small random spread) it finds **6,211 opportunities per scan**, and constructing that many `Opportunity` Python objects dominates the wall-clock cost, not the numpy matrix math itself. `cross_quote`, `maker_rebate`, and `latency_arb` are the next slowest because they run the vectorized comparison independently per symbol across a 140-symbol loop rather than once globally. `triangular` and `multi_leg` (graph search) land in the middle — cheaper than the busiest matrix strategies here, but architecturally more expensive per comparison than a matrix op; their cost scales with cycle length and venue connectivity rather than symbol count. Re-run `python -m benchmarks.bench_detection` on your own hardware before relying on these numbers.

## Profitability instrumentation

Detection numbers alone tell you nothing about whether this is profitable — a spread the code "finds" that's gone by the time you can act on it isn't a spread you captured. `analytics/recorder.py` is built to answer that directly instead of assuming it:

- Every detected opportunity is recorded to parquet with full context: timestamp, strategy, venues, sizes, gross spread, every fee component, and expected net PnL.
- In monitor mode, each opportunity's book is re-checked after realistic execution delays (100ms, 500ms, 2s by default — `config.settings.DECAY_CHECK_DELAYS_SEC`), recording whether the spread was still there. **This decay curve — the capturable fraction by latency bucket — is the single most important number in this project.** It tells you what fraction of what you "detect" you could actually have captured.
- Hit rate and realized-vs-expected slippage are tracked separately per strategy and per venue.
- `OpportunityRecorder.generate_summary_report()` produces opportunities/hour, median/p95 net spread, capturable fraction by latency bucket, and — since profit rate is fundamentally `capital x capture_rate x turnover` — the capital and capture rate that would actually be *required* to hit a given dollar-per-hour target, computed explicitly rather than left as an exercise:

```
=== Opportunity summary ===
Opportunities/hour: ...
Median net spread: ...%
P95 net spread: ...%
Capturable fraction by latency bucket:
  after 100ms: ...%
  after 500ms: ...%
  after 2000ms: ...%

=== Implied PnL model (profit_rate = capital x capture_rate x turnover) ===
Implied PnL per $1,000 deployed: $.../hour

=== Capital required for $X/hour target ===
At the observed capture rate and spread: $Y deployed capital
```

**The intended workflow is: run `monitor` for days and read this report** — especially the decay curve — before touching `paper`, and only consider `live` after `paper` results line up with what the decay curve said was actually capturable.

## Execution, inventory, and risk

- `execution/inventory.py` tracks free/locked balances per venue per asset (every non-atomic strategy needs capital *already positioned* on every venue a leg touches — see `execution/executor.py`'s module docstring), flags lopsided inventory via `imbalance_report`, and estimates rebalancing transfer cost/latency.
- `execution/router.py` ranks opportunities by expected value and reserves each leg's required inventory before accepting it, so two strategies can never both spend the same free balance in one tick.
- `execution/reconciler.py` detects one-sided fills (one leg of a multi-leg trade succeeded, another failed) — the single biggest real-money risk in non-atomic arbitrage — logs loudly, and proposes the exact offsetting trade to flatten the resulting position.
- `risk/limits.py` + `risk/manager.py`: per-strategy capital allocation, per-venue exposure caps, max notional per trade, a daily loss limit, a max-trades-per-day circuit breaker, a max-consecutive-failures kill-switch, and a manual global emergency stop. Sizing walks real order book depth (`BookState.vwap_fill_price`) and rejects a trade if the volume-weighted fill price would eat the opportunity's edge — never just trusts top-of-book size.

## Before running with real money

Read this section in full before ever setting `ARB_MODE=live`.

1. **Pre-funded balances across every venue a leg touches are mandatory.** You cannot buy on venue A, transfer to venue B, and sell there before the spread closes — transfers take seconds (on-chain) to tens of minutes (CEX withdrawal + confirmation), and the spread that justified the trade is almost always gone by the time funds arrive. Every non-atomic strategy here fires all legs concurrently against capital that must already be sitting on every venue involved.
2. **Withdrawal fees, gas, and transfer time aren't fully eliminated, only surfaced.** `execution/inventory.py` estimates rebalancing cost/latency and flags imbalance; it doesn't rebalance for you. You need an actual plan (manual or separately automated) for keeping every venue funded.
3. **Partial and one-sided fills need an unwind, not just detection.** `execution/reconciler.py` detects a one-sided fill and proposes the offsetting trade, but does not execute it automatically — that's a deliberate seam, not an oversight, since auto-unwinding without a human or a more sophisticated policy in the loop can itself compound a bad situation.
4. **Displayed top-of-book is not real depth.** `risk/manager.size_with_depth_check` walks the book and rejects excessive slippage, but only against the depth this engine has actually observed — a book that looks deep on a stale REST snapshot can still walk badly against a live order.
5. **Rate limits and quirks across 15+ CEX and 8+ DEX venues will need real hardening.** ccxt smooths over a lot; expect exchange-specific edge cases in production that this reference engine's REST fallback (see point 6) does not fully paper over.
6. **Latency is the deciding factor past paper mode.** `ccxt.pro` (real websocket streaming — `watch_order_book`) is a separately licensed product, not part of open-source `ccxt` on PyPI. Without it, `core/feed_manager.py` transparently falls back to REST polling, which is materially slower and coarser than a genuine websocket diff stream. Most of what this engine gets from vectorization and lock-free reads only matters once it's actually fed by real websocket streams — get a `ccxt.pro` license (or equivalent native exchange websocket clients) before trusting anything past `monitor`/`paper`.
7. **Smart contract risk is real for every DEX leg.** This bot detects and constructs on-chain transactions; it does not audit the AMM/aggregator/bridge contracts it interacts with. The flash-loan-funded same-chain path additionally requires a deployed repay-in-one-transaction contract this Python bot does not provide or audit.
8. **`latency_arb.py` is close to un-winnable for a Python bot.** It's included because it's a real category, and because feeding its findings into the decay-curve analytics is informative — not because you should expect to capture it. Firms colocated at exchange data centers close this kind of lag in low-single-digit milliseconds.
9. **`statistical.py` is not arbitrage.** It carries real directional risk and can lose money even when everything is implemented correctly.
10. **Exchange ToS, tax treatment of arbitrage trading, and local regulation on automated trading all vary by exchange and jurisdiction.** That's on you to check before running anything live.

**Workflow: run `monitor` for days and read the decay-curve analytics, then graduate to `paper` and confirm simulated results line up with what the decay curve implied was capturable, and only then consider `live` — with capital you can genuinely afford to lose.**

## Testing

```bash
pytest         # 88 tests: core, all 15 strategies, execution/risk, hypothesis property tests -- no live network calls
ruff check .   # clean
python -m benchmarks.bench_detection
```

Coverage highlights: a clean profitable case and a fee-rejected case per strategy where applicable; stale-quote filtering; same-exchange exclusion; book-depth-bounded sizing; a known negative cycle found by both the DFS and Bellman-Ford graph search (and a non-cycle graph correctly finding nothing); gas + price impact flipping a marginal CEX-DEX opportunity negative (this caught a real bug during development — the DEX "buy" direction was originally using the AMM's sell-side pricing formula, which gets slippage backwards at size; fixed and covered by `test_strategy_dex_dex.py`); every risk circuit breaker (daily loss, trade count, consecutive failures, emergency stop, per-strategy/per-venue caps); one-sided fill detection and unwind proposal; router inventory double-commit prevention; and hypothesis property tests on the AMM math and depth-aware sizing.
