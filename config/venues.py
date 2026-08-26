"""CEX and DEX venue definitions.

Every venue carries the static facts strategies need to price an
opportunity correctly: fees, withdrawal costs, minimum order sizes, rate
limits, supported chains, and websocket availability. These are loaded
once at startup (see core/feed_manager.py, core/rest_manager.py) so
nothing static is recomputed in the hot path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Chain(StrEnum):
    ETHEREUM = "ethereum"
    BSC = "bsc"
    SOLANA = "solana"
    ARBITRUM = "arbitrum"
    BASE = "base"
    POLYGON = "polygon"
    NONE = "none"  # CEX, off-chain


@dataclass(frozen=True, slots=True)
class CexVenue:
    """A centralized exchange, accessed through ccxt / ccxt.pro."""

    id: str
    name: str
    taker_fee: float
    maker_fee: float
    # Flat withdrawal fee in the withdrawn asset's own units, keyed by asset.
    withdrawal_fees: dict[str, float] = field(default_factory=dict)
    min_order_usd: float = 10.0
    rate_limit_per_min: int = 1200
    supports_websocket: bool = True
    is_derivatives: bool = False
    supports_funding_rates: bool = False


@dataclass(frozen=True, slots=True)
class DexVenue:
    """An on-chain venue: an AMM pool family or a routing aggregator."""

    id: str
    name: str
    chain: Chain
    protocol_type: str  # "amm_v2" | "amm_v3" | "stableswap" | "weighted" | "aggregator"
    swap_fee_tiers: tuple[float, ...]  # e.g. (0.0001, 0.0005, 0.003, 0.01) for v3
    min_order_usd: float = 10.0
    rate_limit_per_min: int = 300
    supports_websocket: bool = False  # on-chain venues are polled/subscribed via RPC logs
    is_aggregator: bool = False


# ---------------------------------------------------------------------------
# CEX venues (>= 15 required; 20 defined). Fees are representative VIP-0
# taker/maker rates; withdrawal fees are representative flat fees for BTC/
# ETH/USDT and are approximate. These are only a *fallback* -- once a venue
# is connected, `RestManager` overrides them with the real per-symbol fees
# ccxt reports for that account (see `core/rest_manager.py`), since fee
# accuracy directly decides which spreads are actually profitable.
#
# Every id here must be a valid `ccxt.async_support` exchange id -- ccxt
# adds/removes/renames exchanges between releases (e.g. BitMart is not
# present as of the ccxt version this was written against), so if
# `RestManager.connect_all()` logs "ccxt has no exchange named ...", check
# the installed ccxt's `ccxt.async_support.exchanges` list and fix the id
# here rather than assuming the venue itself is unreachable.
#
# Why these 20 (trimmed from 31 after a real deployment):
#   * Dropped `binance` and `bybit`: both hard-block the deploy region at
#     the API edge ("Service unavailable from a restricted location";
#     CloudFront "configured to block access from your country"). No code
#     change makes those reachable -- `binanceus` covers that liquidity
#     from a US host instead.
#   * Dropped `upbit`, `bithumb`, `coincheck`: KRW/JPY-denominated books
#     with little overlap with this engine's USDT/USDC universe, so they
#     contributed venue count without contributing comparable prices.
#   * Dropped `deribit` and `bitmex`: derivatives-only, no spot books for
#     the spot strategies that actually run here.
#   * Dropped `cryptocom` and `blockchaincom`: 0.40% taker. A round trip
#     costs ~0.80%, which is wider than essentially every real spot spread
#     -- they generate candidates that can never clear fees.
#   * Dropped `lbank` and `digifinex`: thin books and/or unreliable
#     reported volume; depth-aware sizing rejects most of what they
#     surface anyway.
# Fewer, better venues also directly cuts per-tick CPU and RAM, which is
# what a small VPS actually runs out of first.
# ---------------------------------------------------------------------------

CEX_VENUES: dict[str, CexVenue] = {
    v.id: v
    for v in [
        CexVenue("binanceus", "Binance US", 0.0010, 0.0010, {"BTC": 0.0002, "ETH": 0.0015, "USDT": 1.0}, 10, 1200, True, False, False),
        CexVenue("coinbase", "Coinbase", 0.0060, 0.0040, {"BTC": 0.0001, "ETH": 0.0010, "USDT": 2.5}, 10, 600, True, False, False),
        CexVenue("kraken", "Kraken", 0.0026, 0.0016, {"BTC": 0.00005, "ETH": 0.0015, "USDT": 2.5}, 10, 900, True, True, True),
        CexVenue("okx", "OKX", 0.0010, 0.0008, {"BTC": 0.0001, "ETH": 0.0006, "USDT": 0.8}, 5, 1200, True, True, True),
        CexVenue("kucoin", "KuCoin", 0.0010, 0.0010, {"BTC": 0.0002, "ETH": 0.0012, "USDT": 1.0}, 5, 1800, True, True, True),
        CexVenue("bitget", "Bitget", 0.0010, 0.0010, {"BTC": 0.0002, "ETH": 0.0015, "USDT": 1.0}, 5, 1200, True, True, True),
        CexVenue("gate", "Gate.io", 0.0020, 0.0020, {"BTC": 0.0004, "ETH": 0.0015, "USDT": 1.0}, 5, 900, True, True, True),
        CexVenue("mexc", "MEXC", 0.0000, 0.0000, {"BTC": 0.0002, "ETH": 0.0012, "USDT": 1.0}, 5, 1200, True, True, True),
        CexVenue("htx", "HTX (Huobi)", 0.0020, 0.0020, {"BTC": 0.0002, "ETH": 0.0015, "USDT": 1.0}, 5, 600, True, True, True),
        CexVenue("bitfinex", "Bitfinex", 0.0020, 0.0010, {"BTC": 0.0004, "ETH": 0.0015, "USDT": 1.0}, 10, 600, True, True, True),
        CexVenue("bitstamp", "Bitstamp", 0.0030, 0.0030, {"BTC": 0.0002, "ETH": 0.0015, "USDT": 2.0}, 25, 480, True, False, False),
        CexVenue("gemini", "Gemini", 0.0035, 0.0010, {"BTC": 0.0002, "ETH": 0.0020, "USDT": 2.5}, 10, 600, True, False, False),
        CexVenue("bingx", "BingX", 0.0010, 0.0010, {"BTC": 0.0002, "ETH": 0.0015, "USDT": 1.0}, 5, 600, True, True, True),
        CexVenue("phemex", "Phemex", 0.0010, 0.0010, {"BTC": 0.0002, "ETH": 0.0015, "USDT": 1.0}, 5, 600, True, True, True),
        # WOO X's maker fee is a representative negative rebate (VIP maker-rebate tier).
        CexVenue("woo", "WOO X", 0.0005, -0.0001, {"BTC": 0.0002, "ETH": 0.0010, "USDT": 1.0}, 5, 600, True, True, True),
        CexVenue("bitvavo", "Bitvavo", 0.0025, 0.0015, {"BTC": 0.0002, "ETH": 0.0012, "USDT": 1.5}, 5, 1000, True, False, False),
        CexVenue("coinex", "CoinEx", 0.0020, 0.0020, {"BTC": 0.0003, "ETH": 0.0015, "USDT": 1.0}, 5, 600, True, True, True),
        CexVenue("whitebit", "WhiteBIT", 0.0010, 0.0010, {"BTC": 0.0002, "ETH": 0.0012, "USDT": 1.0}, 5, 600, True, True, True),
        CexVenue("hitbtc", "HitBTC", 0.0009, 0.0009, {"BTC": 0.0004, "ETH": 0.0015, "USDT": 1.0}, 5, 600, True, False, False),
        CexVenue("poloniex", "Poloniex", 0.0015, 0.0015, {"BTC": 0.0002, "ETH": 0.0015, "USDT": 1.0}, 5, 600, True, True, True),
    ]
}

# ---------------------------------------------------------------------------
# DEX venues (>= 8 required; 11 defined across 6 chains).
# ---------------------------------------------------------------------------

DEX_VENUES: dict[str, DexVenue] = {
    v.id: v
    for v in [
        DexVenue("uniswap_v2_eth", "Uniswap v2", Chain.ETHEREUM, "amm_v2", (0.003,), 20, 300, False, False),
        DexVenue("uniswap_v3_eth", "Uniswap v3", Chain.ETHEREUM, "amm_v3", (0.0001, 0.0005, 0.003, 0.01), 20, 300, False, False),
        DexVenue("curve_eth", "Curve", Chain.ETHEREUM, "stableswap", (0.0004,), 50, 300, False, False),
        DexVenue("balancer_eth", "Balancer", Chain.ETHEREUM, "weighted", (0.0001, 0.003, 0.01), 20, 300, False, False),
        DexVenue("sushiswap_eth", "SushiSwap", Chain.ETHEREUM, "amm_v2", (0.003,), 20, 300, False, False),
        DexVenue("pancakeswap_v3_bsc", "PancakeSwap v3", Chain.BSC, "amm_v3", (0.0001, 0.0005, 0.0025, 0.01), 10, 300, False, False),
        DexVenue("raydium_sol", "Raydium", Chain.SOLANA, "amm_v2", (0.0025,), 5, 600, False, False),
        DexVenue("orca_sol", "Orca", Chain.SOLANA, "amm_v3", (0.0001, 0.0005, 0.003, 0.01), 5, 600, False, False),
        DexVenue("jupiter_sol", "Jupiter (aggregator)", Chain.SOLANA, "aggregator", (), 5, 600, False, True),
        DexVenue("camelot_arb", "Camelot", Chain.ARBITRUM, "amm_v3", (0.0001, 0.0005, 0.003, 0.01), 10, 300, False, False),
        DexVenue("uniswap_v3_arb", "Uniswap v3", Chain.ARBITRUM, "amm_v3", (0.0001, 0.0005, 0.003, 0.01), 10, 300, False, False),
        DexVenue("aerodrome_base", "Aerodrome", Chain.BASE, "amm_v2", (0.0005, 0.003), 10, 300, False, False),
        DexVenue("quickswap_polygon", "QuickSwap", Chain.POLYGON, "amm_v3", (0.0001, 0.0005, 0.003, 0.01), 10, 300, False, False),
    ]
}

# Off-chain routing aggregators used to get quotes without reimplementing
# AMM math per protocol (see strategies/cex_dex.py, strategies/dex_dex.py).
AGGREGATOR_IDS: tuple[str, ...] = ("1inch", "0x", "jupiter", "odos")

ALL_CEX_IDS: tuple[str, ...] = tuple(CEX_VENUES.keys())
ALL_DEX_IDS: tuple[str, ...] = tuple(DEX_VENUES.keys())


# ---------------------------------------------------------------------------
# Live fee overrides.
#
# The tables above are hand-written approximations. Once `RestManager`
# connects a venue, ccxt reports the maker/taker rates that actually apply
# to that account, and those get registered here. Fee accuracy is not a
# detail: typical real cross-exchange spreads are a few basis points, so a
# 0.1% error in an assumed fee is larger than the entire edge and flips
# opportunities between "profitable" and "not" incorrectly.
#
# Keyed by `(venue_id, symbol)` with `symbol=None` holding the venue-wide
# default, so a lookup prefers the most specific rate available and falls
# back to the static table only when nothing live was reported.
# ---------------------------------------------------------------------------

_LIVE_TAKER_FEES: dict[tuple[str, str | None], float] = {}
_LIVE_MAKER_FEES: dict[tuple[str, str | None], float] = {}


def register_live_fees(
    venue_id: str,
    symbol: str | None,
    taker: float | None,
    maker: float | None,
) -> None:
    """Record real maker/taker rates reported by a connected venue."""
    if taker is not None:
        _LIVE_TAKER_FEES[(venue_id, symbol)] = taker
    if maker is not None:
        _LIVE_MAKER_FEES[(venue_id, symbol)] = maker


def clear_live_fees() -> None:
    """Drop all registered live fees (used by tests and on reconnect)."""
    _LIVE_TAKER_FEES.clear()
    _LIVE_MAKER_FEES.clear()


def _live_fee(table: dict[tuple[str, str | None], float], venue_id: str, symbol: str | None) -> float | None:
    if symbol is not None:
        exact = table.get((venue_id, symbol))
        if exact is not None:
            return exact
    return table.get((venue_id, None))


def taker_fee_for(venue_id: str, fallback: float, symbol: str | None = None) -> float:
    """Look up a venue's taker fee, or `fallback` if the venue is unknown.

    Prefers the live rate reported by the connected venue (per-symbol,
    then venue-wide) over the static table above.
    """
    live = _live_fee(_LIVE_TAKER_FEES, venue_id, symbol)
    if live is not None:
        return live
    cex = CEX_VENUES.get(venue_id)
    if cex is not None:
        return cex.taker_fee
    dex = DEX_VENUES.get(venue_id)
    if dex is not None and dex.swap_fee_tiers:
        return min(dex.swap_fee_tiers)
    return fallback


def maker_fee_for(venue_id: str, fallback: float, symbol: str | None = None) -> float:
    """Look up a venue's maker fee, or `fallback` if the venue is unknown.

    Can be negative (a rebate) on venues that pay makers to add liquidity.
    Prefers the live rate reported by the connected venue.
    """
    live = _live_fee(_LIVE_MAKER_FEES, venue_id, symbol)
    if live is not None:
        return live
    cex = CEX_VENUES.get(venue_id)
    if cex is not None:
        return cex.maker_fee
    return taker_fee_for(venue_id, fallback, symbol)


def min_order_usd_for(venue_id: str, fallback: float = 10.0) -> float:
    """Look up a venue's minimum order notional in USD.

    Enforced by `risk.manager.RiskManager` -- an "opportunity" sized below
    the venue's minimum is one the exchange would reject outright, so
    surfacing it as tradeable is worse than finding nothing.
    """
    cex = CEX_VENUES.get(venue_id)
    if cex is not None:
        return cex.min_order_usd
    dex = DEX_VENUES.get(venue_id)
    if dex is not None:
        return dex.min_order_usd
    return fallback
