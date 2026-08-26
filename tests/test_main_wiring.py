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

from main import build_strategies
from strategies.base import Strategy


def test_build_strategies_constructs_without_error():
    strategies = build_strategies()

    assert len(strategies) > 0
    assert all(isinstance(s, Strategy) for s in strategies)
    # Every strategy must have a unique, non-default name (real strategies
    # override `Strategy.name`; a leftover base-class instance would not).
    names = [s.name for s in strategies]
    assert "base" not in names
    assert len(names) == len(set(names))
