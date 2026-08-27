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

  > **Set these only for venues you have real keys for.** `monitor` and `paper` need no credentials at all — they read public market data. A *placeholder* value is strictly worse than an absent one: the exchange receives a real auth attempt, rejects it (`401`, `Invalid API-KEY`), and the venue drops out of the run entirely. Shipping `.env.example` with uncommented sample keys is exactly how `coinbase` and `okx` got silently dropped from a live deployment, so those lines are now commented out by default.
- Poll budget: `MAX_POLLED_SYMBOLS` (default `24`), `MIN_VENUES_PER_SYMBOL` (default `2`), `MAX_CONCURRENT_POLLS` (default `40`) — see the request-budget note under [Asset universe and venues](#asset-universe-and-venues) before raising these.
- DEX hot wallet: `DEX_HOT_WALLET_ADDRESS`, `DEX_HOT_WALLET_PRIVATE_KEY` — **use a dedicated wallet with limited funds, never a main wallet.**
- RPC endpoints per chain: `ETH_RPC_URL`, `ARBITRUM_RPC_URL`, `BASE_RPC_URL`, `POLYGON_RPC_URL`, `BSC_RPC_URL`, `SOLANA_RPC_URL`.
- Aggregator API keys (optional): `ONEINCH_API_KEY`, `ZEROX_API_KEY`, `JUPITER_API_KEY`, `ODOS_API_KEY`.
- Risk overrides: `MAX_TRADE_USD`, `DAILY_LOSS_LIMIT_USD`, `MAX_TRADES_PER_DAY`, `MAX_CONSECUTIVE_FAILURES`.
- Dashboard: `DASHBOARD_ENABLED` (default `true`), `DASHBOARD_HOST` (default `127.0.0.1`), `DASHBOARD_PORT` (default `8420`), `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` (required together if `DASHBOARD_HOST` is not local) — see [Local dashboard](#local-dashboard).

All credentials load via `python-dotenv` from a local `.env`. Nothing is hardcoded.

## Running

```bash
ARB_MODE=monitor python main.py   # log only, no orders
ARB_MODE=paper python main.py     # simulate fills, track PnL
ARB_MODE=live python main.py      # real orders against pre-funded inventory -- read the section below first
```

A `/metrics` HTTP endpoint (`METRICS_HTTP_PORT`, default 9100) serves a JSON snapshot of latency histograms, hit rates, and PnL attribution; the same snapshot is also logged periodically.

### Running it as a service (recommended for anything unattended)

The commands above run in the foreground; backgrounding them with `nohup ... & disown` leaves nothing to restart the bot when it crashes, gets OOM-killed, or the host reboots. On a systemd host, install it as a service instead — once:

```bash
bash deploy/install-service.sh            # mode from .env, or paper if unset
bash deploy/install-service.sh monitor    # or pin the mode explicitly
bash deploy/install-service.sh --dry-run  # print the unit it would install, change nothing
```

Then:

```bash
systemctl status kbot      # is it up?
journalctl -u kbot -f      # live logs
systemctl restart kbot     # after a git pull
systemctl stop kbot        # stop it (stays stopped until started again)
```

`deploy/kbot.service` is a template with `__PLACEHOLDER__` tokens; the installer substitutes the repo path, the mode, and memory limits sized from the host's actual RAM, then stops any previously running instance (service *or* stray `nohup` process, which would otherwise hold the dashboard and `/metrics` ports), installs the unit, and enables it at boot. `tests/test_deploy_service.py` covers that substitution contract — an unsubstituted placeholder yields a unit that fails only at boot, on the one machine nobody is watching.

Three details worth knowing:

- **`ExecStart` invokes `.venv/bin/python` by absolute path** rather than sourcing the venv. `source .venv/bin/activate` is an interactive-shell-ism that no-ops in a service context, and a bot started without its venv doesn't fail loudly — it fails as `ModuleNotFoundError: ccxt` in a log nobody is reading.
- **`MemoryMax`/`MemoryHigh` cap the service's cgroup**, sized as the host's RAM minus a reserve (~330M/280M on a 512MB droplet; capped at 2GB on larger hosts, since the engine's steady state is a few hundred MB and anything past that is a leak worth capping rather than headroom worth granting). This is not about the bot dying — systemd restarts it. It's about the kernel OOM killer otherwise picking an arbitrary victim on the host: on a small droplet that has meant `sshd`, turning a bot outage into an unreachable machine.
- **The mode is baked into the unit**, so a restart always comes back in that mode. A mode switched from the dashboard lives in process memory only — nothing can silently resurrect itself in `live` because of something you clicked once. Change the service's mode by re-running the installer with the mode you want.

To remove it: `systemctl disable --now kbot && rm /etc/systemd/system/kbot.service && systemctl daemon-reload`.

## Local dashboard

`main.py` also starts a small control-plane dashboard (`dashboard/`) in the same process, bound to `http://127.0.0.1:8420` by default. Open that URL in a browser while the bot is running to see live opportunities (each with its realized $ P&L once it's actually been traded in paper/live mode, not just its detected spread), per-strategy toggles, latency/decay charts, and every configured venue's connection + API-key status, and to control the running process: switch mode (monitor/paper/live), start/stop the detection loop, pull the emergency stop, and adjust risk limits.

The "Connected venues" panel lists every venue in `config/venues.py` with an "Add key"/"Update" button per row (`dashboard/credentials.py`) — entering an API key + secret there saves it to this server's `.env` (so it survives a restart) and immediately reconnects that venue's ccxt client (`RestManager.reconnect`), without needing to SSH in and edit `.env` by hand. The key/secret are never echoed back by the API once saved — only whether a venue has one configured. This is still convenience layered on top of the same plain-HTTP transport as the rest of the dashboard (see the Basic Auth caveat below): typing a real exchange secret into this form carries the same in-transit exposure as the dashboard password does, so treat it accordingly.

**This is a real control plane for a real process, not a demo.** It reads and mutates the exact same `RiskManager`, `ControlState`, and strategy list the detection loop uses — there is no separate database or simulation layer. Switching to `live` from this page has the identical real-money consequences as setting `ARB_MODE=live` in `.env`: the next opportunity the loop finds fires real market orders against whatever balances are pre-funded on the connected exchanges. The dashboard's own `POST /api/mode` route refuses to arm `live` without an explicit `confirm: true`, and the page shows the same pre-funded-balance warning before letting you check that box — but that's a safety rail, not a substitute for reading [Before running with real money](#before-running-with-real-money) first.

It binds to localhost only (`DASHBOARD_HOST=127.0.0.1`) and is meant to be viewed from the same machine the bot runs on. Set `DASHBOARD_ENABLED=false` to disable it entirely, or change `DASHBOARD_HOST`/`DASHBOARD_PORT` if you understand the risk of exposing a mode-switching, order-arming API beyond your own machine — the default is deliberately conservative.

**If you do change `DASHBOARD_HOST` (e.g. to `0.0.0.0` on a cloud VPS so you can reach it from your own browser), you must also set `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD` in `.env`.** With both set, every route — the API, the websocket, and the static frontend — requires HTTP Basic Auth (`dashboard/auth.py`); your browser will show a normal login prompt. Leaving `DASHBOARD_HOST` non-local without these set logs a `CRITICAL` warning on startup, because the dashboard is otherwise reachable, unauthenticated, by anyone who finds the IP and port. Note that this is plain HTTP, not HTTPS: Basic Auth stops casual/opportunistic access (port scanners, a leaked IP), but the credentials themselves aren't encrypted in transit — don't reuse a password you care about elsewhere, and if you want protection against someone actively monitoring the network path to your server, put this behind an SSH tunnel or a TLS-terminating reverse proxy instead of trusting Basic Auth alone.

Some browsers (observed on iPad Safari) reliably resend cached Basic Auth credentials on every `fetch()` call but not on the raw WebSocket handshake `/ws` uses — the page would load correctly with real data but the live-updates connection badge would stay permanently "disconnected". `dashboard/auth.py`'s `WsTicketStore` works around this: the page first fetches a short-lived, single-use ticket over a normal (reliably authenticated) request (`GET /api/ws_ticket`), then connects to `/ws?ticket=...` instead of depending on the browser to carry the Authorization header over. This is transparent — nothing to configure — and falls back gracefully if the ticket fetch itself fails.

## Project structure

```
config/     tunables, tiered asset universe, CEX/DEX venue definitions
core/       order book state, feed multiplexer, REST fallback, currency graph, typed structs, market snapshot, live control state
strategies/ 15 arbitrage strategies, one file each, all implementing strategies.base.Strategy
execution/  router, monitor/paper/live executor, inventory tracking, one-sided-fill reconciler
risk/       declarative limits + enforcement (capital, exposure, circuit breakers)
analytics/  latency/hit-rate/PnL metrics, parquet opportunity recorder with decay-curve tracking
dashboard/  local control-plane API + frontend for a running engine process (see "Local dashboard" above)
benchmarks/ synthetic full-universe detection throughput benchmark
deploy/     systemd unit template + installer for running the engine unattended
tests/      pytest suite, one module per strategy + core + execution/risk, plus hypothesis property tests
main.py     async entry point
```

## Asset universe and venues

`config/universe.py` defines a 4-tier universe (10 majors, 20 large-caps, 30 mid-caps, plus 7 stablecoins and 10 wrapped/LST assets — 70+ base assets total) with per-tier poll intervals and profit thresholds (`config/settings.TIER_CONFIG`). The *tradeable symbol list* is never hardcoded as pair strings: `build_tradeable_symbols` intersects each venue's actual listed markets (from `load_markets()`) with this universe, across 5 quote assets (USDT, USDC, BTC, ETH, EUR).

**Not all of that universe is polled at once, and that is deliberate.** Keeping `N` symbols fresh across `V` venues at interval `T` costs `V * N / T` requests per second, and exchanges rate-limit public endpoints to roughly 10–30 req/s each. Exceeding that doesn't fetch more data — ccxt queues the surplus, so books arrive progressively later and *everything* goes stale, majors included. There's also a floor: a symbol polled less often than `PRICE_STALENESS_SEC` is discarded as stale on essentially every tick. `select_pollable_symbols` therefore spends the budget where it can actually pay off: it drops symbols listed on fewer than `MIN_VENUES_PER_SYMBOL` connected venues (a symbol on one venue has no counterparty to arbitrage against), then caps what remains at `MAX_POLLED_SYMBOLS`, most-liquid base and quote first. At 20 venues that's ~24 symbols and ~320 req/s (~16/venue) — versus ~2,500 req/s for the whole universe, which no venue would have served. This ceiling is a property of REST polling, not of the engine; real websocket streams lift it by orders of magnitude.

`config/venues.py` defines 20 CEX venues (binanceus, coinbase, kraken, okx, kucoin, bitget, gate, mexc, htx, bitfinex, bitstamp, gemini, bingx, phemex, woo, bitvavo, coinex, whitebit, hitbtc, poloniex) and 13 DEX venues across 6 chains (Ethereum, BSC, Solana, Arbitrum, Base, Polygon), each with taker/maker fees, withdrawal fees, minimum order sizes, rate limits, and websocket support flags. Every CEX id is validated against `ccxt.async_support`'s exchange list (`tests/test_config_venues.py` asserts this in CI, after an earlier invalid id — `bitmart`, since dropped from ccxt — shipped and only surfaced as a runtime connection warning); ccxt adds/drops exchanges between releases, so `RestManager` logs and skips (rather than crashes on) an id ccxt no longer recognizes.

That list was cut from 31 after a live deployment, on evidence rather than preference: `binance` and `bybit` hard-block the deploy region at the API edge (no code change reaches them — `binanceus` covers that liquidity from a US host); `upbit`/`bithumb`/`coincheck` quote in KRW/JPY with little overlap with this USDT/USDC universe; `deribit`/`bitmex` are derivatives-only and have no spot books for the strategies that actually run; `cryptocom`/`blockchaincom` charge 0.40% taker, so a round trip costs ~0.80% — wider than essentially every real spot spread; `lbank`/`digifinex` have thin books that depth-aware sizing rejects anyway. Fewer, better venues also directly cuts per-tick CPU and RAM, which is what a small host runs out of first.

**Fees come from the venue, not from that table.** The hand-written rates in `config/venues.py` are a fallback only: on connect, `RestManager` reads the maker/taker rates ccxt reports for the connected account and registers them (`register_live_fees`), preferring per-symbol over venue-wide over the static table. This isn't a detail — real cross-exchange spreads are a few basis points, so a 0.1% error in an assumed fee is larger than the entire edge and flips opportunities between "profitable" and "not". Values outside a sane fractional band are rejected rather than trusted, since a misparsed fee silently corrupts every downstream calculation.

## Strategies

All 15 strategies implement `strategies.base.Strategy.scan(market_state) -> list[Opportunity]` and run against one shared `MarketState` snapshot per detection tick.

**10 of the 15 run by default** (`main.build_strategies`). The other five are deliberately off, because running a strategy that cannot produce a real trade is not free — it spends CPU every tick that the strategies with live data need, and in `paper` mode it inflates results with fills that could never happen:

| Off by default | Why |
|---|---|
| `funding_rate`, `basis_carry`, `calendar_spread` | They read `MarketState.funding_rates` / `.futures_quotes`, which nothing in the live loop populates — only the benchmark and unit tests fill them synthetically. Re-enable them together with a real funding-rate / futures-curve poller, not before. |
| `multi_leg` | It only emits routes containing a `transfer` edge, and every transfer carries `DEFAULT_TRANSFER_LATENCY_SEC` — 15 minutes of withdrawal, confirmation and deposit. A spread must survive that entire window to be capturable, which is exactly what `execution/executor.py`'s module docstring explains doesn't happen. It was also ~84% of the detection tick's CPU at production universe size. Re-enable it as an inventory-rebalancing planner, not an arbitrage signal. |
| `statistical` | Needs a curated pairs list and carries real directional risk — it is not arbitrage. |

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
- **O(1) symbol lookup**: `BookStore.all_for_symbol`/`venues_for_symbol` are backed by a `dict[symbol, list[OrderBook]]` index maintained in `get_or_create`. Before this, both scanned every `(venue, symbol)` entry in the store per call — with a full universe (hundreds of symbols x a dozen-plus venues) and every symbol-scanning strategy calling this once per symbol per tick, that made book lookup, not the strategy math, the dominant real-world cost of the `state_to_detect` stage. This was found and fixed after a live deployment on a small VPS showed `state_to_detect` blowing its 5ms budget by 15-25x on every tick — see the benchmark note below for the measured before/after.
- **Per-tick matrix cache**: `MarketState.matrix_for_symbol` memoizes its result per symbol per `MarketState` instance. `cross_exchange`, `latency_arb`, `maker_rebate`, and `perp_perp` all call it for the same symbol within one detection tick; without the cache each paid the full book-scan-and-array-build cost independently. Safe because nothing mutates `book_store` mid-tick (the detection pass never awaits, so the concurrent feed loop can't interleave a write).
- **Bounded multi-leg graph search**: `strategies/multi_leg.py`'s cross-venue graph connects every pair of venues sharing an asset with a "transfer" edge — O(venues²) edges — and `MultiLegStrategy.scan` used to run `core/graph.py`'s cycle search from *every* node in that graph. This was fine at ~15-19 venues but combinatorially exploded at 24+: a live deployment saw `state_to_detect` jump from ~60ms to **~28 seconds** on a single tick after connecting more venues (visible in the dashboard's opportunity feed too — the same route repeated once per extra venue is the tell). Three independent bounds fix this regardless of how many venues get added later: `MAX_TRANSFER_VENUES_PER_ASSET` caps transfer-edge fan-out per asset (O(venues) instead of O(venues²)); DFS now only starts from hub-asset nodes (`USDT`/`USDC`/`BTC`/`ETH`) instead of every node, since a cycle can be discovered from any of its own members and real multi-leg routes pass through a common asset anyway; and `CurrencyGraph.find_cycles` takes an optional `max_expansions` cap that hard-stops the search after a fixed number of edge traversals, bounding one call's cost to a constant independent of graph density. Verified at the exact scale that caused the regression (24 venues × 15 shared symbols): **28s → 59.5ms**.
- **Bounded in-memory opportunity/decay history**: `analytics/recorder.py`'s `all_opportunity_records`/`all_decay_records` used to be plain lists that grew for the entire life of the process — everything is also written to parquet on every `flush()`, so this in-memory copy only ever existed to serve `generate_summary_report`/the dashboard without re-reading files. A multi-hour live deployment was OOM-killed by the OS as a direct result (`dmesg`: `Out of memory: Killed process ... (python)`), which also explains an SSH connection failing around the same time — the whole host was starved of memory, not just the bot. Both are now bounded rolling windows (`deque`, capped at 10k/30k records respectively); `OpportunityRecorder.attach_trade_result` was updated to look up by the real opportunity id via a `dict` index rather than by list position, since position-equals-id only held while nothing was ever evicted.
- **Bounded cycle search in `triangular`**: same class of fix, found when enabling it by default. It searched from *every* node on *every* venue and de-duplicated the rotations afterwards — doing length-many times the work to produce the same set — and passed no `max_expansions`. Restricting starts to hub assets (every tradeable pair is quoted in one, so real cycles are still found) plus the expansion cap took it from 529ms to ~21ms per tick. Its cycle length is also pinned to 3 rather than config's `MAX_CYCLE_LENGTH` of 5: search cost grows exponentially with length while the edge needed to clear fees grows only linearly, so length 5 costs ~9x the CPU (206ms vs 23ms) *and* needs ~1.0% of edge to break even at 0.20% taker per leg versus ~0.6% for 3 legs. A 1% single-venue triangular edge on liquid pairs effectively doesn't occur.
- **Pruned market metadata**: `load_markets()` returns every market a venue lists — often 2,000+ spot/futures/option entries, each carrying the venue's raw JSON in `market["info"]` — and ccxt holds all of it for the client's lifetime. Across a dozen-plus venues that was the single largest memory consumer in the process and a direct contributor to a real OOM kill. `RestManager` now prunes each client to the configured universe immediately after load, rebuilding ccxt's own symbol/id indexes so order placement still resolves. Measured on a representative market set: **~10 MB saved per venue, ~208 MB across 20 venues.**
- **Bounded, tier-aware polling**: `poll_loop` used to fire `venues x symbols` requests *concurrently, every cycle* — thousands of simultaneous in-flight HTTP requests at full universe size, each holding connection and buffer state. It now polls each symbol at its own tier's cadence and caps concurrency at `MAX_CONCURRENT_POLLS`. See "Asset universe and venues" for the request-budget arithmetic that bounds the working set.
- **Feed/detect separation**: `core/feed_manager.py` only ever writes into `BookStore`; strategies only ever read from it. A slow detection pass cannot block ingestion.
- **Precomputed statics**: fee tables, the universe, and venue metadata are all loaded once at startup (`config/`), never recomputed per tick.
- **msgspec.Struct** (with `gc=False`) for the hottest per-tick objects (`Quote`, `OpportunitySignal`); `slots=True` dataclasses everywhere else.
- **Per-stage latency histograms** (`time.perf_counter_ns()`) for feed→state, state→detect, detect→decision, decision→ack, each with a configurable budget (`config.settings.LATENCY_BUDGETS`) that logs a warning when exceeded.

### Benchmark numbers

Measured with `python -m benchmarks.bench_detection` on a synthetic snapshot at **140 symbols x 19 venues (2,660 order books)** — above the 50-coin x 15-venue design target. Numbers are from one run on this machine; absolute throughput will vary by hardware, but the *relative* cost of vectorized-matrix strategies vs. graph-search strategies is the architecturally interesting comparison:

```
strategy                scans/sec  avg ms/scan  avg opps/scan
cross_exchange               29.9       33.439        5796.00
triangular                 1630.5        0.613           0.00
cex_dex                     505.5        1.978           0.00
dex_dex                    14803.4        0.068          23.00
funding_rate              17883.0        0.056           0.00
basis_carry                3394.7        0.295         141.00
calendar_spread            3613.9        0.277         100.00
cross_quote                  317.1        3.153           0.00
stablecoin_depeg            805.7        1.241           0.00
wrapped_asset              7618.2        0.131           0.00
perp_perp                 10918.7        0.092           0.00
statistical               102042.4        0.010           0.00
multi_leg                  1585.1        0.631           0.00
maker_rebate               2874.9        0.348           0.00
latency_arb                2902.6        0.345           0.00
```

`cross_exchange` is the slowest per-scan despite being fully vectorized — on this synthetic snapshot (every venue quoting every symbol with a small random spread) it finds **thousands of opportunities per scan**, and constructing that many `Opportunity` Python objects dominates the wall-clock cost, not the numpy matrix math itself. Real market data finds nowhere near that many genuine spreads per tick, so this benchmark substantially overstates its real cost. Re-run on your own hardware before relying on any of these numbers.

**This benchmark is deliberately larger than production.** It sweeps 140 symbols to expose relative scaling between vectorized-matrix and graph-search strategies; the engine actually polls ~24 (see the request-budget note under "Asset universe and venues"). For what the deployed configuration costs, measure the production shape instead:

| Detection tick, 24 symbols × 20 venues (480 books), 10 active strategies | p50 |
|---|---|
| `triangular` | 20.9 ms |
| `maker_rebate` | 11.7 ms |
| `cross_exchange` | 1.8 ms |
| `cross_quote` | 1.2 ms |
| `latency_arb` | 0.9 ms |
| `stablecoin_depeg` | 0.5 ms |
| `wrapped_asset`, `perp_perp`, `cex_dex`, `dex_dex` | <0.1 ms each |
| **full tick** | **37 ms** (p95 117 ms) |

For scale, that same measurement was **~28 seconds** before the `multi_leg` combinatorial fix, and ~122 ms before `triangular` was bounded and `multi_leg` retired from the default set. Note that measuring this correctly requires rewriting the books every iteration — books written once age past `PRICE_STALENESS_SEC` within a few seconds, after which every strategy correctly skips them and the benchmark degenerates into timing the staleness filter rather than any real work.

37 ms is still above the 5 ms `state_to_detect` budget in `config.settings.LATENCY_BUDGETS`. That budget describes a machine fed by real websocket streams, not a small VPS on REST polling; treat it as the target that justifies a feed upgrade, not as a threshold this configuration is expected to meet.

## Profitability instrumentation

Detection numbers alone tell you nothing about whether this is profitable — a spread the code "finds" that's gone by the time you can act on it isn't a spread you captured. `analytics/recorder.py` is built to answer that directly instead of assuming it:

- Every detected opportunity is recorded to parquet with full context: timestamp, strategy, venues, sizes, gross spread, every fee component, and expected net PnL.
- In monitor mode, each opportunity's book is re-checked after realistic execution delays (100ms, 500ms, 2s by default — `config.settings.DECAY_CHECK_DELAYS_SEC`), recording whether the spread was still there. **This decay curve — the capturable fraction by latency bucket — is the single most important number in this project.** It tells you what fraction of what you "detect" you could actually have captured.

  > **If you have decay numbers from before this was fixed, discard them.** The re-pricing function required `detail["buy_venue"]`/`["sell_venue"]`, which only 4 of the 15 strategies set. For the other 11 — including `multi_leg` and `cross_quote`, which dominated the live feed — it returned `None`, and the recorder scores `None` as "did not survive". The headline capturable fraction was therefore counting every strategy it *couldn't measure* as decayed, biasing the number sharply downward. It now re-prices an opportunity's own legs as a chained conversion (`buy @ ask -> x (1/P)(1-fee)`, `sell @ bid -> x P(1-fee)`, `transfer -> x (1-fee)`), which is the same formulation `core/graph.py` uses to price cycles and works for both two-leg and cycle shapes — cycle strategies carry no per-leg size, so any notional-weighted sum reads zero for all of them.
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
- `risk/limits.py` + `risk/manager.py`: per-strategy capital allocation, per-venue exposure caps, max notional per trade, a daily loss limit, a max-trades-per-day circuit breaker, a max-consecutive-failures kill-switch, and a manual global emergency stop. Sizing walks real order book depth (`BookState.vwap_fill_price`) and rejects a trade if the volume-weighted fill price would eat the opportunity's edge — never just trusts top-of-book size. `RiskManager.reverify_profitability` is a second, final gate: immediately before every paper or live trade, it recomputes net profit from every leg's *current* book depth and real fees rather than trusting the detection-time figure, and `execution/executor.py` skips (never records or fires) any trade that comes back non-profitable. This closes the gap the decay-curve analytics expose — most detected spreads don't survive detection-to-execution latency — so an opportunity that decayed away between being seen and being acted on is never mistaken for a real trade.

  Two further gates worth calling out, both added after a live paper run behaved wrongly:

  - **Exchange minimum order sizes are enforced.** `min_order_usd` sat in `config/venues.py` from the start but nothing ever read it, so the engine surfaced and paper-filled opportunities below the minimum notional the venue would accept — trades that would simply be rejected in reality. `RiskManager` now zeroes any size that doesn't clear the largest minimum across the route's legs (every leg has to execute for the trade to work). This matters most at small capital, which is exactly where it was silently wrong.
  - **Deployed capital is released when a trade completes.** `strategy_capital_usd` / `venue_exposure_usd` bound notional deployed *at once*, and every strategy here is a round trip whose legs open and close together — so the capital is free again the moment the trade finishes. It previously only ever *added* to the deployed totals, never subtracted, with no daily reset either. Deployed capital ratcheted up until every strategy sat permanently at its cap and the engine **stopped trading for the rest of the process's life** — at the shipped defaults ($5,000 cap, $500 max notional) that was after just 10 trades per strategy, against a `max_trades_per_day` of 2,000. A live paper run plateaued at 19 trades because of it. The executor now commits notional while a trade is in flight and releases it on completion (or on any path that bails out before trading), and the daily roll clears the totals as a backstop.

## Before running with real money

Read this section in full before ever setting `ARB_MODE=live`.

1. **Pre-funded balances across every venue a leg touches are mandatory.** You cannot buy on venue A, transfer to venue B, and sell there before the spread closes — transfers take seconds (on-chain) to tens of minutes (CEX withdrawal + confirmation), and the spread that justified the trade is almost always gone by the time funds arrive. Every non-atomic strategy here fires all legs concurrently against capital that must already be sitting on every venue involved.
2. **Withdrawal fees, gas, and transfer time aren't fully eliminated, only surfaced.** `execution/inventory.py` estimates rebalancing cost/latency and flags imbalance; it doesn't rebalance for you. You need an actual plan (manual or separately automated) for keeping every venue funded.
3. **Partial and one-sided fills need an unwind, not just detection.** `execution/reconciler.py` detects a one-sided fill and proposes the offsetting trade, but does not execute it automatically — that's a deliberate seam, not an oversight, since auto-unwinding without a human or a more sophisticated policy in the loop can itself compound a bad situation.
4. **Displayed top-of-book is not real depth.** `risk/manager.size_with_depth_check` walks the book and rejects excessive slippage, but only against the depth this engine has actually observed — a book that looks deep on a stale REST snapshot can still walk badly against a live order.
5. **A profitable-looking detection does not mean the trade will be profitable.** The gap between when a strategy sees an edge and when the executor can act on it is real (see point 8), and other actors close spreads in that time. `execution/executor.py` re-checks every opportunity's real profitability against current book depth immediately before committing — via `RiskManager.reverify_profitability` — and silently skips anything that no longer clears zero net profit. This means fewer trades than the raw detection count, on purpose: it's the difference between what looked good a moment ago and what's actually still good now.
6. **Rate limits and quirks across 20 CEX and 13 DEX venues will need real hardening.** ccxt smooths over a lot; expect exchange-specific edge cases in production that this reference engine's REST fallback (see point 8) does not fully paper over. Note also that venue reachability is a property of *where you deploy*: two venues were dropped from the default list purely because they hard-block the deploy region at the API edge, which no code change fixes.
7. **`funding_rate.py`, `basis_carry.py`, `calendar_spread.py`, and the DEX-pool side of `cex_dex.py`/`dex_dex.py` need data feeds `main.py` does not currently fetch.** `MarketState.funding_rates`, `.futures_quotes`, and `.dex_pools` default to empty and nothing in the real detection loop populates them — only `benchmarks/bench_detection.py` and the unit tests fill them in synthetically. In a real run, these strategies scan every tick and correctly find nothing, spending CPU for zero opportunities; only the strategies driven directly by `BookStore` (cross_exchange, triangular, cex_dex's CEX leg, cross_quote, stablecoin_depeg, wrapped_asset, perp_perp, multi_leg, maker_rebate, latency_arb) are actually live today. Wiring real funding-rate/futures-curve/pool-reserve polling into the loop is real, valuable follow-up work — it is not done yet, and no opportunity from the dormant strategies should be trusted until it is.
8. **Latency is the deciding factor past paper mode.** `ccxt.pro` (real websocket streaming — `watch_order_book`) is a separately licensed product, not part of open-source `ccxt` on PyPI. Without it, `core/feed_manager.py` transparently falls back to REST polling, which is materially slower and coarser than a genuine websocket diff stream. Most of what this engine gets from vectorization and lock-free reads only matters once it's actually fed by real websocket streams — get a `ccxt.pro` license (or equivalent native exchange websocket clients) before trusting anything past `monitor`/`paper`.
9. **Smart contract risk is real for every DEX leg.** This bot detects and constructs on-chain transactions; it does not audit the AMM/aggregator/bridge contracts it interacts with. The flash-loan-funded same-chain path additionally requires a deployed repay-in-one-transaction contract this Python bot does not provide or audit.
10. **`latency_arb.py` is close to un-winnable for a Python bot.** It's included because it's a real category, and because feeding its findings into the decay-curve analytics is informative — not because you should expect to capture it. Firms colocated at exchange data centers close this kind of lag in low-single-digit milliseconds.
11. **`statistical.py` is not arbitrage.** It carries real directional risk and can lose money even when everything is implemented correctly.
12. **The dashboard is a real control surface, not a viewer.** Anyone who can reach `http://<DASHBOARD_HOST>:<DASHBOARD_PORT>` can switch the running process to `live` and arm real orders, and can now also set exchange API credentials directly from the "Connected venues" panel. The default (`127.0.0.1`) keeps it reachable only from the machine the bot runs on; treat changing that host the same way you'd treat exposing an exchange API key. If you do expose it, set `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` (see [Local dashboard](#local-dashboard)) — an exposed dashboard with no login is equivalent to publishing your exchange API keys.
13. **Exchange ToS, tax treatment of arbitrage trading, and local regulation on automated trading all vary by exchange and jurisdiction.** That's on you to check before running anything live.
14. **A restart is not recovery.** `deploy/install-service.sh` (see [Running it as a service](#running-it-as-a-service-recommended-for-anything-unattended)) fixes the crude half of this — a crash, an OOM kill, or a reboot no longer leaves the bot silently down until someone notices. What it does *not* do is make a crash safe in `live` mode: systemd restarts the process, and the fresh process has no memory of orders that were in flight when the old one died. `execution/reconciler.py` detects one-sided fills within a single process lifetime; it does not reconcile against the exchange on startup. A crash between two legs of a live trade therefore leaves real, unhedged exposure that nothing will notice or unwind. Startup reconciliation against actual exchange balances and open orders is required work before `live` — restarting reliably in the wrong state is not better than staying down.

**Workflow: run `monitor` for days and read the decay-curve analytics, then graduate to `paper` and confirm simulated results line up with what the decay curve implied was capturable, and only then consider `live` — with capital you can genuinely afford to lose.**

## Testing

```bash
pytest         # 184 tests: core (O(1) BookStore symbol index, MarketState matrix cache, bounded cycle search), all 15 strategies, execution/risk (pre-execution profitability re-check, venue minimum order sizes, deployed-capital release), analytics (bounded in-memory history, decay re-pricing), connect-time market pruning + live fee capture, tier-aware poll scheduling + concurrency cap, symbol-budget selection, dashboard API + Basic Auth + /ws ticket fallback + venue credentials, config venue-id validation, systemd unit rendering, hypothesis property tests -- no live network calls
ruff check .   # clean
python -m benchmarks.bench_detection
```

Coverage highlights: a clean profitable case and a fee-rejected case per strategy where applicable; stale-quote filtering; same-exchange exclusion; book-depth-bounded sizing; a known negative cycle found by both the DFS and Bellman-Ford graph search (and a non-cycle graph correctly finding nothing); gas + price impact flipping a marginal CEX-DEX opportunity negative (this caught a real bug during development — the DEX "buy" direction was originally using the AMM's sell-side pricing formula, which gets slippage backwards at size; fixed and covered by `test_strategy_dex_dex.py`); every risk circuit breaker (daily loss, trade count, consecutive failures, emergency stop, per-strategy/per-venue caps); one-sided fill detection and unwind proposal; router inventory double-commit prevention; and hypothesis property tests on the AMM math and depth-aware sizing.
