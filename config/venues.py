"""CEX and DEX venue definitions.

Every venue carries the static facts strategies need to price an
opportunity correctly: fees, withdrawal costs, minimum order sizes, rate
limits, supported chains, and websocket availability. These are loaded
once at startup (see core/feed_manager.py, core/rest_manager.py) so
nothing static is recomputed in the hot path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Chain(str, Enum):
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
# ETH/USDT and are approximate — always refresh from the exchange's live
# fee schedule before sizing real trades.
# ---------------------------------------------------------------------------

CEX_VENUES: dict[str, CexVenue] = {
    v.id: v
    for v in [
        CexVenue("binance", "Binance", 0.0010, 0.0010, {"BTC": 0.0002, "ETH": 0.0015, "USDT": 1.0}, 10, 6000, True, True, True),
        CexVenue("coinbase", "Coinbase", 0.0060, 0.0040, {"BTC": 0.0001, "ETH": 0.0010, "USDT": 2.5}, 10, 600, True, False, False),
        CexVenue("kraken", "Kraken", 0.0026, 0.0016, {"BTC": 0.00005, "ETH": 0.0015, "USDT": 2.5}, 10, 900, True, True, True),
        CexVenue("bybit", "Bybit", 0.0010, 0.0010, {"BTC": 0.0002, "ETH": 0.0010, "USDT": 1.0}, 5, 1200, True, True, True),
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
        CexVenue("cryptocom", "Crypto.com", 0.0040, 0.0040, {"BTC": 0.0004, "ETH": 0.0015, "USDT": 1.0}, 10, 300, True, True, True),
        CexVenue("bitmart", "BitMart", 0.0025, 0.0025, {"BTC": 0.0005, "ETH": 0.0020, "USDT": 1.0}, 5, 600, True, False, False),
        CexVenue("lbank", "LBank", 0.0010, 0.0010, {"BTC": 0.0005, "ETH": 0.0020, "USDT": 1.0}, 5, 300, True, False, False),
        CexVenue("phemex", "Phemex", 0.0010, 0.0010, {"BTC": 0.0002, "ETH": 0.0015, "USDT": 1.0}, 5, 600, True, True, True),
        # WOO X's maker fee is a representative negative rebate (VIP maker-rebate tier).
        CexVenue("woo", "WOO X", 0.0005, -0.0001, {"BTC": 0.0002, "ETH": 0.0010, "USDT": 1.0}, 5, 600, True, True, True),
        CexVenue("deribit", "Deribit", 0.0003, 0.0000, {"BTC": 0.0001, "ETH": 0.0010, "USDT": 1.0}, 10, 1200, True, True, True),
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


def taker_fee_for(venue_id: str, fallback: float) -> float:
    """Look up a venue's taker fee, or `fallback` if the venue is unknown."""
    cex = CEX_VENUES.get(venue_id)
    if cex is not None:
        return cex.taker_fee
    dex = DEX_VENUES.get(venue_id)
    if dex is not None and dex.swap_fee_tiers:
        return min(dex.swap_fee_tiers)
    return fallback


def maker_fee_for(venue_id: str, fallback: float) -> float:
    """Look up a venue's maker fee, or `fallback` if the venue is unknown.

    Can be negative (a rebate) on venues that pay makers to add liquidity.
    """
    cex = CEX_VENUES.get(venue_id)
    if cex is not None:
        return cex.maker_fee
    return taker_fee_for(venue_id, fallback)
