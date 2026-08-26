"""Tiered asset universe.

Tiering matters because scan cost scales with universe size: majors
(tier 1) get the highest poll priority and lowest profit thresholds
since their books are deep and reliable, while mid-caps (tier 3) are
scanned less often and demanded to clear a higher bar.

The tradeable *symbol* list is never hardcoded as pair strings — it is
built programmatically as the intersection of what each venue actually
lists (from `load_markets()` / a venue's market metadata) with this
universe, via `build_tradeable_symbols`.
"""

from __future__ import annotations

from dataclasses import dataclass

TIER_1: tuple[str, ...] = (
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "TRX", "LINK",
)

TIER_2: tuple[str, ...] = (
    "DOT", "MATIC", "TON", "SHIB", "LTC", "BCH", "UNI", "ATOM", "XLM", "ETC",
    "NEAR", "APT", "FIL", "ARB", "OP", "INJ", "IMX", "HBAR", "VET", "SUI",
)

TIER_3: tuple[str, ...] = (
    "AAVE", "MKR", "GRT", "SAND", "MANA", "AXS", "RUNE", "FTM", "ALGO", "EGLD",
    "THETA", "FLOW", "CHZ", "CRV", "LDO", "SNX", "COMP", "ENS", "DYDX", "GMX",
    "PENDLE", "JTO", "TIA", "SEI", "STX", "RNDR", "PYTH", "JUP", "WIF", "BONK",
)

STABLECOINS: tuple[str, ...] = ("USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "EURC")

WRAPPED_ASSETS: tuple[str, ...] = (
    "WBTC", "WETH", "STETH", "WSTETH", "RETH", "CBETH", "WBNB", "WSOL", "JITOSOL", "MSOL",
)

# Maps a wrapped/derivative asset to the underlying it should track ~1:1
# (or via a known, monotonic exchange rate) for wrapped_asset.py.
WRAPPED_UNDERLYING: dict[str, str] = {
    "WBTC": "BTC",
    "WETH": "ETH",
    "STETH": "ETH",
    "WSTETH": "ETH",
    "RETH": "ETH",
    "CBETH": "ETH",
    "WBNB": "BNB",
    "WSOL": "SOL",
    "JITOSOL": "SOL",
    "MSOL": "SOL",
}

# Preferred quote assets to scan a base against, in priority order.
# cross_quote.py compares implied cross-rates across these on one venue.
QUOTE_ASSETS: tuple[str, ...] = ("USDT", "USDC", "BTC", "ETH", "EUR")

ALL_BASE_ASSETS: tuple[str, ...] = TIER_1 + TIER_2 + TIER_3 + WRAPPED_ASSETS
UNIVERSE_SIZE: int = len(ALL_BASE_ASSETS) + len(STABLECOINS)


@dataclass(frozen=True, slots=True)
class TieredSymbol:
    """A unified `BASE/QUOTE` symbol tagged with its scan tier."""

    symbol: str
    base: str
    quote: str
    tier: str


def tier_of(base_asset: str) -> str:
    """Return the tier name for a base asset (tier1/tier2/tier3/stable/wrapped)."""
    if base_asset in STABLECOINS:
        return "stable"
    if base_asset in WRAPPED_ASSETS:
        return "wrapped"
    if base_asset in TIER_1:
        return "tier1"
    if base_asset in TIER_2:
        return "tier2"
    if base_asset in TIER_3:
        return "tier3"
    return "tier3"  # unranked assets default to the lowest-priority tier


def build_tradeable_symbols(
    venue_markets: dict[str, str],
    quote_assets: tuple[str, ...] = QUOTE_ASSETS,
    base_assets: tuple[str, ...] = ALL_BASE_ASSETS,
) -> list[TieredSymbol]:
    """Build the tradeable symbol list for one venue.

    `venue_markets` is the set of unified symbols a venue actually lists
    (e.g. the keys of `ccxt_client.markets`, passed as a dict/set of
    strings like "BTC/USDT"). This intersects that real listing with the
    configured universe and quote assets rather than hardcoding pair
    strings, so a venue that doesn't list a given pair is simply skipped.
    """
    tradeable: list[TieredSymbol] = []
    for base in base_assets:
        for quote in quote_assets:
            if base == quote:
                continue
            symbol = f"{base}/{quote}"
            if symbol in venue_markets:
                tradeable.append(TieredSymbol(symbol=symbol, base=base, quote=quote, tier=tier_of(base)))
    return tradeable


#: Order tiers are given up when the symbol budget is exceeded -- majors
#: last, long-tail alts first, since arbitrage edge concentrates in liquid
#: books and the long tail mostly produces spreads too thin to clear fees.
_TIER_PRIORITY: tuple[str, ...] = ("tier1", "stable", "wrapped", "tier2", "tier3")

#: Position of each base asset inside its own tier tuple. The tier tuples
#: above are written roughly in descending liquidity order, so this doubles
#: as a liquidity rank and is what keeps a budget cut from dropping ETH
#: while keeping ADA.
_BASE_RANK: dict[str, int] = {
    base: index
    for tier in (TIER_1, TIER_2, TIER_3, STABLECOINS, WRAPPED_ASSETS)
    for index, base in enumerate(tier)
}


def _symbol_rank(symbol: str, venue_count: int) -> tuple[int, int, int, int, str]:
    """Sort key for poll-budget priority; lower sorts first.

    Ordered by tier, then the base asset's liquidity rank within that
    tier, then quote asset (`QUOTE_ASSETS` is written most-liquid first,
    so USDT/USDC books beat the same base quoted in EUR or ETH), then how
    many venues list it, and finally the name purely for determinism.

    Base-and-quote rank must come before venue count: without it the
    ordering degenerates to alphabetical among equally-listed symbols,
    which fills the whole budget with `ADA/*` and `AVAX/*` and never
    reaches ETH or SOL at all.
    """
    base, _, quote = symbol.partition("/")
    tier = tier_of(base)
    tier_priority = _TIER_PRIORITY.index(tier) if tier in _TIER_PRIORITY else len(_TIER_PRIORITY)
    base_rank = _BASE_RANK.get(base, len(_BASE_RANK))
    quote_rank = QUOTE_ASSETS.index(quote) if quote in QUOTE_ASSETS else len(QUOTE_ASSETS)
    return (tier_priority, base_rank, quote_rank, -venue_count, symbol)


def select_pollable_symbols(
    symbols_by_venue: dict[str, set[str]],
    max_symbols: int,
    min_venues: int = 2,
) -> list[str]:
    """Choose which symbols are worth spending poll budget on.

    REST polling is a hard budget: every exchange rate-limits public
    endpoints well below what `venues x symbols` demands at full universe
    size, and exceeding it doesn't fetch more data -- it queues, so
    *everything* goes stale, including the majors that matter. Two filters
    keep the working set inside that budget:

    * **`min_venues`**: a symbol listed on only one connected venue cannot
      produce a cross-venue opportunity, so polling it is pure waste. This
      is free to apply -- it removes no reachable profit.
    * **`max_symbols`**: whatever survives is capped by `_symbol_rank`,
      which spends the budget on the most liquid bases in the most liquid
      quotes first.

    Returns the chosen symbols, most-arbitrageable first.
    """
    venue_count: dict[str, int] = {}
    for listed in symbols_by_venue.values():
        for symbol in listed:
            venue_count[symbol] = venue_count.get(symbol, 0) + 1

    eligible = [s for s, count in venue_count.items() if count >= min_venues]
    eligible.sort(key=lambda s: _symbol_rank(s, venue_count[s]))
    return eligible[:max_symbols] if max_symbols > 0 else eligible


def build_stable_pairs(venue_markets: dict[str, str]) -> list[TieredSymbol]:
    """Build the stable/stable pair list actually listed on a venue.

    Used by strategies/stablecoin_depeg.py to scan CEX stable-stable
    order books for deviations from 1.00.
    """
    pairs: list[TieredSymbol] = []
    for base in STABLECOINS:
        for quote in STABLECOINS:
            if base == quote:
                continue
            symbol = f"{base}/{quote}"
            if symbol in venue_markets:
                pairs.append(TieredSymbol(symbol=symbol, base=base, quote=quote, tier="stable"))
    return pairs
