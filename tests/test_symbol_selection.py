"""Tests for config.universe.select_pollable_symbols.

REST polling is a hard budget (see the note above TIER_CONFIG in
config/settings.py): `venues x symbols / interval` requests per second has
to stay inside what exchanges tolerate, or ccxt queues the surplus and
every book -- majors included -- arrives stale. These filters are what
keep the working set inside that budget.
"""

from __future__ import annotations

from config.universe import select_pollable_symbols


def test_single_venue_symbols_are_dropped():
    """A symbol on one venue has no counterparty venue to arbitrage
    against, so polling it cannot produce an opportunity."""
    symbols_by_venue = {
        "kraken": {"BTC/USDT", "OBSCURE/USDT"},
        "gemini": {"BTC/USDT"},
    }

    chosen = select_pollable_symbols(symbols_by_venue, max_symbols=50)

    assert "BTC/USDT" in chosen
    assert "OBSCURE/USDT" not in chosen


def test_majors_survive_the_cap_before_long_tail_alts():
    symbols_by_venue = {
        "kraken": {"BTC/USDT", "ETH/USDT", "PENDLE/USDT", "BONK/USDT"},
        "gemini": {"BTC/USDT", "ETH/USDT", "PENDLE/USDT", "BONK/USDT"},
    }

    chosen = select_pollable_symbols(symbols_by_venue, max_symbols=2)

    assert set(chosen) == {"BTC/USDT", "ETH/USDT"}


def test_budget_goes_to_the_most_liquid_bases_not_alphabetical_ones():
    """Regression: ranking by venue-count-then-name degenerated to
    alphabetical among equally-listed symbols, filling the entire budget
    with ADA/* and AVAX/* while never reaching BTC or ETH at all."""
    bases = ["ADA", "AVAX", "BTC", "DOGE", "ETH", "SOL"]
    listed = {f"{b}/USDT" for b in bases}
    symbols_by_venue = {"kraken": listed, "gemini": listed}

    chosen = select_pollable_symbols(symbols_by_venue, max_symbols=3)

    assert chosen[0] == "BTC/USDT"
    assert "ETH/USDT" in chosen
    assert "ADA/USDT" not in chosen


def test_liquid_quotes_are_preferred_for_the_same_base():
    listed = {"BTC/USDT", "BTC/EUR", "BTC/USDC"}
    symbols_by_venue = {"kraken": listed, "gemini": listed}

    chosen = select_pollable_symbols(symbols_by_venue, max_symbols=1)

    assert chosen == ["BTC/USDT"]


def test_more_widely_listed_symbols_rank_higher_within_a_tier():
    """More venues listing a symbol means more venue pairs to compare, so
    it earns its poll budget ahead of an equally-ranked but thinner one."""
    symbols_by_venue = {
        "kraken": {"BTC/USDT", "ADA/USDT"},
        "gemini": {"BTC/USDT", "ADA/USDT"},
        "bitstamp": {"BTC/USDT"},
        "coinbase": {"BTC/USDT"},
    }

    chosen = select_pollable_symbols(symbols_by_venue, max_symbols=1)

    assert chosen == ["BTC/USDT"]


def test_cap_is_respected():
    symbols_by_venue = {
        "kraken": {f"ALT{i}/USDT" for i in range(50)},
        "gemini": {f"ALT{i}/USDT" for i in range(50)},
    }

    chosen = select_pollable_symbols(symbols_by_venue, max_symbols=10)

    assert len(chosen) == 10


def test_zero_cap_means_unbounded():
    symbols_by_venue = {
        "kraken": {"BTC/USDT", "ETH/USDT"},
        "gemini": {"BTC/USDT", "ETH/USDT"},
    }

    assert len(select_pollable_symbols(symbols_by_venue, max_symbols=0)) == 2


def test_no_overlap_between_venues_selects_nothing():
    """Guards the case main.py treats as fatal: nothing to arbitrage."""
    symbols_by_venue = {"kraken": {"BTC/USDT"}, "gemini": {"ETH/USDT"}}

    assert select_pollable_symbols(symbols_by_venue, max_symbols=50) == []
