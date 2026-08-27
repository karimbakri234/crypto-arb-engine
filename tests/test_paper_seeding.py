"""Paper capital must equal what you would actually deposit.

`PAPER_SEED_USD_TOTAL` is a total across all venues, not a per-venue
figure. The distinction is the whole point: $1,000 sounds like enough
until it is divided by seventeen venues and twenty assets, at which point
every position is under the exchanges' minimum order size and nothing can
trade. That arithmetic is a finding about the strategy, so it has to be
what the simulation actually models.
"""

from __future__ import annotations

import pytest

import main
from core.book import BookStore
from execution.inventory import InventoryManager
from main import _plan_paper_allocation, _seed_paper_inventory

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "SOL/USDC"]


def _book_store() -> BookStore:
    store = BookStore()
    prices = {"BTC/USDT": 100_000.0, "ETH/USDT": 4_000.0, "SOL/USDT": 200.0, "SOL/USDC": 200.0}
    for symbol, price in prices.items():
        for venue in ("kraken", "gemini", "bitstamp", "kucoin"):
            store.get_or_create(venue, symbol).replace(
                bids=[(price * 0.999, 50.0)], asks=[(price * 1.001, 50.0)]
            )
    return store


def _total_usd(inventory: InventoryManager, store: BookStore) -> float:
    total = 0.0
    for (_venue, asset), balance in inventory._balances.items():
        price = main._usd_price_of(asset, store)
        if price:
            total += balance.total * price
    return total


def test_the_seed_is_a_total_not_a_per_venue_amount(monkeypatch):
    monkeypatch.setattr(main, "PAPER_SEED_USD_TOTAL", 1000.0)
    monkeypatch.setattr(main, "PAPER_SEED_MAX_VENUES", 0)
    inventory, store = InventoryManager(), _book_store()

    _seed_paper_inventory(["kraken", "gemini", "bitstamp", "kucoin"], SYMBOLS, inventory, store)

    assert _total_usd(inventory, store) == pytest.approx(1000.0)


def test_the_total_holds_regardless_of_venue_count(monkeypatch):
    """Four venues or one, the operator deposited the same money."""
    monkeypatch.setattr(main, "PAPER_SEED_USD_TOTAL", 1000.0)
    monkeypatch.setattr(main, "PAPER_SEED_MAX_VENUES", 0)
    store = _book_store()

    one, four = InventoryManager(), InventoryManager()
    _seed_paper_inventory(["kraken"], SYMBOLS, one, store)
    _seed_paper_inventory(["kraken", "gemini", "bitstamp", "kucoin"], SYMBOLS, four, store)

    assert _total_usd(one, store) == pytest.approx(1000.0)
    assert _total_usd(four, store) == pytest.approx(1000.0)


def test_concentrating_the_book_raises_the_per_position_size(monkeypatch):
    """Same money, fewer venues, bigger positions -- the knob that makes a
    small book tradeable instead of stranded below venue minimums."""
    monkeypatch.setattr(main, "PAPER_SEED_USD_TOTAL", 1000.0)
    store = _book_store()
    venues = ["kraken", "gemini", "bitstamp", "kucoin"]

    monkeypatch.setattr(main, "PAPER_SEED_MAX_VENUES", 0)
    spread = InventoryManager()
    _seed_paper_inventory(venues, SYMBOLS, spread, store)

    monkeypatch.setattr(main, "PAPER_SEED_MAX_VENUES", 2)
    concentrated = InventoryManager()
    _seed_paper_inventory(venues, SYMBOLS, concentrated, store)

    assert concentrated.get_balance("kraken", "USDT").free == pytest.approx(
        2 * spread.get_balance("kraken", "USDT").free
    )
    assert concentrated.get_balance("bitstamp", "USDT").free == 0.0
    assert _total_usd(concentrated, store) == pytest.approx(1000.0)


def test_capital_is_split_between_quote_and_base_assets():
    """A cross-venue trade spends quote on one venue and base on the
    other. An all-stablecoin book cannot fund the sell side at all."""
    allocation = _plan_paper_allocation(SYMBOLS, venue_usd=1000.0)

    quotes = allocation["USDT"] + allocation["USDC"]
    bases = allocation["BTC"] + allocation["ETH"] + allocation["SOL"]

    assert quotes == 500.0
    assert bases == 500.0


def test_the_dominant_quote_gets_the_most_capital():
    """Three of four symbols quote in USDT, so USDT holds three quarters
    of the quote allocation rather than an even split that strands money
    in a quote barely used."""
    allocation = _plan_paper_allocation(SYMBOLS, venue_usd=1000.0)

    assert allocation["USDT"] == 375.0  # 3/4 of $500
    assert allocation["USDC"] == 125.0  # 1/4 of $500


def test_an_unpriceable_asset_is_skipped_not_seeded_at_zero(monkeypatch):
    """No book means no way to convert USD into units. Seeding it anyway
    would either divide by zero or invent a balance at a made-up price."""
    monkeypatch.setattr(main, "PAPER_SEED_USD_TOTAL", 1000.0)
    monkeypatch.setattr(main, "PAPER_SEED_MAX_VENUES", 0)
    inventory = InventoryManager()

    _seed_paper_inventory(["kraken"], [*SYMBOLS, "NOSUCH/USDT"], inventory, _book_store())

    assert inventory.get_balance("kraken", "NOSUCH").total == 0.0
    assert inventory.get_balance("kraken", "USDT").free > 0.0


def test_seeding_without_books_seeds_only_cash(monkeypatch, caplog):
    """The startup-ordering bug, pinned. Base assets are converted from USD
    at the live mid, so with no books only the stablecoins (1.0 by
    definition) can be priced. The result funds every buy leg and no sell
    leg: the engine runs, finds opportunities, trades nothing, and reports
    no error. It must be loud instead."""
    monkeypatch.setattr(main, "PAPER_SEED_USD_TOTAL", 1000.0)
    monkeypatch.setattr(main, "PAPER_SEED_MAX_VENUES", 0)
    inventory = InventoryManager()

    with caplog.at_level("ERROR"):
        _seed_paper_inventory(["kraken"], SYMBOLS, inventory, BookStore())

    assert inventory.get_balance("kraken", "USDT").free > 0.0
    assert inventory.get_balance("kraken", "SOL").total == 0.0
    assert "stablecoins only" in caplog.text


def test_seeding_with_books_funds_both_sides():
    inventory = InventoryManager()

    _seed_paper_inventory(["kraken"], SYMBOLS, inventory, _book_store())

    assert inventory.get_balance("kraken", "USDT").free > 0.0
    assert inventory.get_balance("kraken", "SOL").free > 0.0
    assert inventory.get_balance("kraken", "BTC").free > 0.0
