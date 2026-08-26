"""Regression test: every configured CEX id must exist in the installed ccxt.

An invalid id (e.g. the "bitmart" id that ccxt later dropped) doesn't fail
loudly at import time -- it only surfaces as a "ccxt has no exchange
named ..." warning when `RestManager.connect_all()` actually tries to
connect, which is exactly the class of bug this test catches before a
real deployment does.
"""

from __future__ import annotations

import ccxt.async_support as ccxt

from config.venues import CEX_VENUES


def test_every_cex_venue_id_is_a_valid_ccxt_exchange():
    invalid = [venue_id for venue_id in CEX_VENUES if not hasattr(ccxt, venue_id)]

    assert invalid == [], f"CEX_VENUES has ids ccxt.async_support doesn't recognize: {invalid}"


def test_at_least_fifteen_cex_venues_configured():
    assert len(CEX_VENUES) >= 15
