"""Regressions for three bugs that together inflated paper-mode PnL.

Found by reading a live dashboard showing $331.17 across 244 trades, with
feed entries like:

    CROSS_EXCHANGE  SOL/USDT · htx -> bingx   +0.162%   $0.81
    LATENCY_ARB     SOL/USDT · htx -> bingx   +0.162%   $0.81

Identical route, identical edge, identical PnL, counted twice.

1. `latency_arb` computes the same net edge as `cross_exchange` and adds
   one filter, so its hits are a strict subset -- guaranteed duplicates.
2. Paper mode seeded 1,000,000 *units* of every asset on every venue
   (~$200M of SOL), so the router's inventory check could never fail and
   both copies were funded.
3. Reservations were never settled or released, so inventory neither
   depleted (hiding rebalancing cost) nor recovered after a rejected
   trade.
"""

from __future__ import annotations

from execution.inventory import InventoryManager
from execution.router import Router
from main import dedupe_by_route, route_key
from strategies.base import Leg, Opportunity


def _cross_exchange(strategy: str, net_pct: float = 0.162) -> Opportunity:
    """The same physical trade, however it was found."""
    return Opportunity(
        strategy=strategy,
        symbol="SOL/USDT",
        legs=(
            Leg("htx", "SOL/USDT", "buy", 100.0, 5.0, 0.001),
            Leg("bingx", "SOL/USDT", "sell", 100.2, 5.0, 0.001),
        ),
        gross_profit_pct=net_pct + 0.2,
        net_profit_pct=net_pct,
        max_size_usd=500.0,
    )


def test_the_same_route_found_by_two_strategies_is_one_opportunity():
    kept, duplicates = dedupe_by_route(
        [_cross_exchange("cross_exchange"), _cross_exchange("latency_arb")]
    )

    assert len(kept) == 1
    assert duplicates == 1


def test_the_higher_edge_wins_a_duplicate_route():
    kept, _ = dedupe_by_route(
        [_cross_exchange("cross_exchange", net_pct=0.10), _cross_exchange("latency_arb", net_pct=0.18)]
    )

    assert kept[0].net_profit_pct == 0.18


def test_genuinely_different_routes_both_survive():
    other = _cross_exchange("cross_exchange")
    other.legs[1].venue_id = "bitget"

    kept, duplicates = dedupe_by_route([_cross_exchange("cross_exchange"), other])

    assert len(kept) == 2
    assert duplicates == 0


def test_route_key_ignores_leg_order_but_not_direction():
    """Same legs listed in either order are one route; swapping which
    venue is bought and which is sold is a different trade."""
    forward = _cross_exchange("cross_exchange")
    reordered = _cross_exchange("latency_arb")
    reordered.legs = tuple(reversed(reordered.legs))

    assert route_key(forward) == route_key(reordered)

    flipped = _cross_exchange("cross_exchange")
    flipped.legs[0].side, flipped.legs[1].side = "sell", "buy"
    assert route_key(flipped) != route_key(forward)


# --- inventory settlement ----------------------------------------------------


def _funded_router(usdt: float = 1000.0, sol: float = 10.0) -> tuple[Router, InventoryManager]:
    inventory = InventoryManager()
    inventory.set_balance("htx", "USDT", usdt)
    inventory.set_balance("bingx", "SOL", sol)
    return Router(inventory), inventory


def test_a_filled_trade_moves_balance_between_venues_and_assets():
    """Buying SOL on htx spends htx's USDT and leaves SOL there; selling
    on bingx spends bingx's SOL and leaves USDT. Repeat and htx runs out
    of USDT -- which is precisely the drift that forces a real transfer,
    and precisely what unsettled fills hid."""
    router, inventory = _funded_router()
    opportunity = _cross_exchange("cross_exchange")

    accepted = router.select([opportunity])
    assert accepted == [opportunity]
    router.settle_fill(opportunity)

    # Spent: 5 SOL x $100 = $500 of htx USDT, and 5 SOL on bingx.
    assert inventory.get_balance("htx", "USDT").free == 500.0
    assert inventory.get_balance("bingx", "SOL").free == 5.0
    # Received: 5 SOL on htx, 5 x $100.2 = $501 of USDT on bingx.
    assert inventory.get_balance("htx", "SOL").free == 5.0
    assert inventory.get_balance("bingx", "USDT").free == 501.0
    # Nothing left dangling.
    assert inventory.get_balance("htx", "USDT").locked == 0.0


def test_inventory_actually_runs_out():
    """The property that makes paper mode honest: finite capital stops
    funding the same one-directional route."""
    router, _ = _funded_router(usdt=1000.0, sol=10.0)

    filled = 0
    for _ in range(5):
        opportunity = _cross_exchange("cross_exchange")
        if router.select([opportunity]):
            router.settle_fill(opportunity)
            filled += 1

    # $1000 of USDT funds two $500 buys; bingx only holds 10 SOL for two
    # 5-SOL sells. The third attempt has nothing left to reserve.
    assert filled == 2


def test_a_rejected_trade_gives_its_reservation_back():
    """`router.select` locks balance before the executor re-checks
    profitability. When that check rejects the trade, the lock has to be
    undone or the engine slowly starves itself."""
    router, inventory = _funded_router()
    opportunity = _cross_exchange("cross_exchange")

    router.select([opportunity])
    assert inventory.get_balance("htx", "USDT").free == 500.0

    router.release_unfilled(opportunity)

    assert inventory.get_balance("htx", "USDT").free == 1000.0
    assert inventory.get_balance("htx", "USDT").locked == 0.0


def test_settling_twice_does_not_double_credit():
    router, inventory = _funded_router()
    opportunity = _cross_exchange("cross_exchange")
    router.select([opportunity])

    router.settle_fill(opportunity)
    router.settle_fill(opportunity)

    assert inventory.get_balance("htx", "SOL").free == 5.0
