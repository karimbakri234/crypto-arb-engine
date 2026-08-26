"""Tests for core.rest_manager's connect-time market pruning and fee capture.

No live network calls: these exercise the pure helpers against a stub
client shaped like a loaded ccxt exchange.

Both behaviours here came out of a real deployment. Pruning exists
because `load_markets()` holds every market a venue lists (thousands,
each carrying the venue's raw JSON) for the life of the client, which was
the largest memory consumer in a process the OS eventually OOM-killed.
Fee capture exists because the static fallback table is an approximation,
and a 0.1% fee error is wider than most real spreads.
"""

from __future__ import annotations

import pytest

from config.venues import clear_live_fees, maker_fee_for, taker_fee_for
from core.rest_manager import (
    _as_rate,
    _capture_live_fees,
    _prune_markets,
    universe_symbols,
)


class FakeClient:
    """Minimal stand-in for a ccxt client after `load_markets()`."""

    def __init__(self, markets: dict, fees: dict | None = None) -> None:
        self.markets = markets
        self.symbols = sorted(markets)
        self.markets_by_id = {m["id"]: [m] for m in markets.values() if m.get("id")}
        self.ids = sorted(self.markets_by_id)
        self.fees = fees or {}


def _market(symbol: str, taker: float | None = None, maker: float | None = None) -> dict:
    market = {"id": symbol.replace("/", ""), "symbol": symbol}
    if taker is not None:
        market["taker"] = taker
    if maker is not None:
        market["maker"] = maker
    return market


@pytest.fixture(autouse=True)
def _no_live_fee_leakage():
    """Live fees are process-global; keep them from leaking between tests."""
    clear_live_fees()
    yield
    clear_live_fees()


def test_universe_symbols_covers_both_symbol_builders():
    symbols = universe_symbols()

    assert "BTC/USDT" in symbols          # build_tradeable_symbols
    assert "USDC/USDT" in symbols         # build_stable_pairs
    assert "WBTC/USDC" in symbols         # wrapped assets
    assert "BTC/BTC" not in symbols       # never pair an asset with itself


def test_prune_drops_off_universe_markets():
    markets = {
        "BTC/USDT": _market("BTC/USDT"),
        "ETH/USDT": _market("ETH/USDT"),
        "SOMETHING/ELSE": _market("SOMETHING/ELSE"),
        "BTC/USDT:USDT-240927": _market("BTC/USDT:USDT-240927"),  # dated future
    }
    client = FakeClient(markets)

    kept, dropped = _prune_markets(client, universe_symbols())

    assert kept == 2
    assert dropped == 2
    assert set(client.markets) == {"BTC/USDT", "ETH/USDT"}


def test_prune_keeps_ccxt_symbol_and_id_indexes_consistent():
    """Order placement resolves a symbol through these indexes, so they
    must not survive pruning still pointing at dropped markets."""
    markets = {"BTC/USDT": _market("BTC/USDT"), "SOMETHING/ELSE": _market("SOMETHING/ELSE")}
    client = FakeClient(markets)

    _prune_markets(client, universe_symbols())

    assert client.symbols == ["BTC/USDT"]
    assert client.ids == ["BTCUSDT"]
    assert set(client.markets_by_id) == {"BTCUSDT"}


def test_prune_is_a_noop_rather_than_emptying_a_non_overlapping_venue():
    """A venue whose listings don't overlap this universe should be left
    intact for the caller to notice, not silently reduced to nothing."""
    markets = {"AAA/BBB": _market("AAA/BBB")}
    client = FakeClient(markets)

    kept, dropped = _prune_markets(client, universe_symbols())

    assert (kept, dropped) == (0, 0)
    assert client.markets == markets


def test_live_fees_override_the_static_table():
    client = FakeClient(
        {"BTC/USDT": _market("BTC/USDT", taker=0.0007, maker=0.0002)},
        fees={"trading": {"taker": 0.0009, "maker": 0.0004}},
    )

    _capture_live_fees("kraken", client)

    # Per-symbol beats venue-wide, venue-wide beats the static table.
    assert taker_fee_for("kraken", 0.001, symbol="BTC/USDT") == 0.0007
    assert maker_fee_for("kraken", 0.001, symbol="BTC/USDT") == 0.0002
    assert taker_fee_for("kraken", 0.001, symbol="ETH/USDT") == 0.0009


def test_static_table_still_used_when_nothing_live_was_reported():
    from config.venues import CEX_VENUES

    assert taker_fee_for("kraken", 0.001) == CEX_VENUES["kraken"].taker_fee


def test_implausible_fee_values_are_rejected_rather_than_trusted():
    """A misparsed fee silently corrupts every downstream profitability
    calculation, so anything outside a sane fractional band is dropped."""
    assert _as_rate(0.001) == 0.001
    assert _as_rate(-0.0001) == -0.0001  # small maker rebates are real
    assert _as_rate(None) is None
    assert _as_rate("not a number") is None
    assert _as_rate(1.5) is None         # 150% -- certainly a percent/fraction mixup
    assert _as_rate(-5.0) is None
    assert _as_rate(True) is None        # bool is an int subclass; not a fee
