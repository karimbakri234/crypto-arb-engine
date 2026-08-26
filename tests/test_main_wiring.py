"""Regression test for main.py's strategy wiring.

`build_strategies()` constructs every strategy with keyword arguments
main.py assumes are valid -- but each strategy class defines its own
constructor, and nothing else in the test suite actually calls
`build_strategies()` end to end. A mismatched kwarg (e.g. passing
`max_trade_usd` to a strategy that sizes trades via `probe_size_base`
instead) only surfaces at real startup, which is exactly what happened
running this against a live server. This test exists so that class of
bug fails in CI instead of at 3am against a real exchange connection.
"""

from __future__ import annotations

from core.book import BookStore
from main import _rescan_net_profit_pct, build_strategies, is_fillable
from strategies.base import Leg, Opportunity, Strategy


def test_build_strategies_constructs_without_error():
    strategies = build_strategies()

    assert len(strategies) > 0
    assert all(isinstance(s, Strategy) for s in strategies)
    # Every strategy must have a unique, non-default name (real strategies
    # override `Strategy.name`; a leftover base-class instance would not).
    names = [s.name for s in strategies]
    assert "base" not in names
    assert len(names) == len(set(names))


def test_strategies_needing_unpopulated_feeds_are_not_run():
    """funding_rate/basis_carry/calendar_spread read MarketState fields
    nothing in the live loop populates, so running them spends CPU every
    tick to correctly find nothing."""
    names = {s.name for s in build_strategies()}

    assert names.isdisjoint({"funding_rate", "basis_carry", "calendar_spread"})


def _two_leg_opportunity() -> Opportunity:
    return Opportunity(
        strategy="cross_exchange",
        symbol="BTC/USDT",
        legs=(
            Leg("kraken", "BTC/USDT", "buy", 100.0, 1.0, 0.001),
            Leg("gemini", "BTC/USDT", "sell", 102.0, 1.0, 0.001),
        ),
        gross_profit_pct=2.0,
        net_profit_pct=1.8,
        max_size_usd=100.0,
        detail={"buy_venue": "kraken", "sell_venue": "gemini"},
    )


def _cycle_opportunity() -> Opportunity:
    """A multi_leg-shaped opportunity: a closed conversion cycle whose legs
    carry no size and no buy_venue/sell_venue in `detail`."""
    return Opportunity(
        strategy="multi_leg",
        symbol="multi:USDT-BTC-USDT",
        legs=(
            Leg("kraken", "BTC/USDT", "buy", 100.0, 0.0, 0.001),
            Leg("kraken->gemini", "TRANSFER", "transfer", 0.0, 0.0, 0.0005),
            Leg("gemini", "BTC/USDT", "sell", 102.0, 0.0, 0.001),
        ),
        gross_profit_pct=2.0,
        net_profit_pct=1.7,
        max_size_usd=100.0,
    )


def _store_with(bid: float, ask: float) -> BookStore:
    store = BookStore()
    for venue in ("kraken", "gemini"):
        store.get_or_create(venue, "BTC/USDT").replace(bids=[(bid, 5.0)], asks=[(ask, 5.0)])
    return store


def test_rescan_reprices_a_two_leg_route():
    store = BookStore()
    store.get_or_create("kraken", "BTC/USDT").replace(bids=[(99.9, 5.0)], asks=[(100.0, 5.0)])
    store.get_or_create("gemini", "BTC/USDT").replace(bids=[(102.0, 5.0)], asks=[(102.1, 5.0)])

    net_pct = _rescan_net_profit_pct(store, _two_leg_opportunity())

    assert net_pct is not None
    assert net_pct > 1.0  # ~2% gross less ~0.2% fees


def test_rescan_reprices_a_cycle_route_without_leg_sizes():
    """Regression: cycle strategies (multi_leg/triangular/cross_quote) carry
    no per-leg size and no buy_venue/sell_venue in `detail`. The previous
    implementation returned None for all of them, and the recorder scores
    None as "did not survive" -- so the headline capturable fraction was
    counting every unmeasurable strategy as decayed."""
    store = BookStore()
    store.get_or_create("kraken", "BTC/USDT").replace(bids=[(99.9, 5.0)], asks=[(100.0, 5.0)])
    store.get_or_create("gemini", "BTC/USDT").replace(bids=[(102.0, 5.0)], asks=[(102.1, 5.0)])

    net_pct = _rescan_net_profit_pct(store, _cycle_opportunity())

    assert net_pct is not None
    assert net_pct > 0.0


def test_rescan_sees_a_spread_that_closed():
    """Both venues now quote the same book: the edge is gone and the
    rescan must report that rather than the detection-time figure."""
    store = _store_with(bid=99.9, ask=100.0)

    assert _rescan_net_profit_pct(store, _two_leg_opportunity()) < 0.0
    assert _rescan_net_profit_pct(store, _cycle_opportunity()) < 0.0


def test_rescan_returns_none_when_a_venue_dropped_out():
    store = BookStore()
    store.get_or_create("kraken", "BTC/USDT").replace(bids=[(99.9, 5.0)], asks=[(100.0, 5.0)])
    # gemini has no book at all

    assert _rescan_net_profit_pct(store, _two_leg_opportunity()) is None


def _sized_opportunity(max_size_usd: float, venue: str = "kraken") -> Opportunity:
    return Opportunity(
        strategy="cross_exchange",
        symbol="ADA/USDT",
        legs=(
            Leg(venue, "ADA/USDT", "buy", 1.0, 1.0, 0.001),
            Leg("gemini", "ADA/USDT", "sell", 1.01, 1.0, 0.001),
        ),
        gross_profit_pct=1.0,
        net_profit_pct=0.8,
        max_size_usd=max_size_usd,
    )


def test_quote_with_no_size_behind_it_is_not_fillable():
    """A venue can show an attractive top-of-book price with almost nothing
    behind it. Reported as an opportunity it pollutes the feed and, worse,
    the decay curve: a thin quote nobody wants sits unchanged for seconds
    and scores as "survived" every time, dragging the capturable fraction
    toward 100% -- exactly backwards."""
    assert is_fillable(_sized_opportunity(max_size_usd=0.40)) is False


def test_quote_clearing_every_leg_minimum_is_fillable():
    assert is_fillable(_sized_opportunity(max_size_usd=500.0)) is True


def test_the_largest_leg_minimum_binds():
    """Every leg has to execute, so the strictest venue on the route sets
    the floor -- bitstamp's $25 minimum, not the other leg's $10."""
    strict = _sized_opportunity(max_size_usd=15.0, venue="bitstamp")

    assert is_fillable(strict) is False
    assert is_fillable(_sized_opportunity(max_size_usd=15.0, venue="kraken")) is True


def test_legless_opportunity_is_not_fillable():
    opp = _sized_opportunity(max_size_usd=500.0)
    opp.legs = ()

    assert is_fillable(opp) is False
