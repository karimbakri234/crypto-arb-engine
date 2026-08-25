"""Global tunables, run mode, and latency budgets.

Everything that shapes behaviour (thresholds, poll intervals, latency
budgets, feature toggles) lives here so it can be changed without
touching strategy or execution code. Credentials are loaded separately
via `.env` (python-dotenv) and never hardcoded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

VALID_MODES = ("monitor", "paper", "live")

MODE: str = os.getenv("ARB_MODE", "monitor").strip().lower()
if MODE not in VALID_MODES:
    raise ValueError(f"Invalid ARB_MODE={MODE!r}; must be one of {VALID_MODES}")


@dataclass(frozen=True, slots=True)
class TierConfig:
    """Poll priority and profit threshold for one universe tier."""

    name: str
    poll_interval_sec: float
    min_profit_pct: float


# Scan cost scales with universe size, so higher tiers (majors) get polled
# more frequently and can tolerate a slightly lower profit threshold since
# their books are deeper and quotes are more reliable.
TIER_CONFIG: dict[str, TierConfig] = {
    "tier1": TierConfig(name="tier1", poll_interval_sec=0.5, min_profit_pct=0.15),
    "tier2": TierConfig(name="tier2", poll_interval_sec=2.0, min_profit_pct=0.30),
    "tier3": TierConfig(name="tier3", poll_interval_sec=5.0, min_profit_pct=0.50),
    "stable": TierConfig(name="stable", poll_interval_sec=1.0, min_profit_pct=0.05),
    "wrapped": TierConfig(name="wrapped", poll_interval_sec=2.0, min_profit_pct=0.10),
}

# ---------------------------------------------------------------------------
# General tunables
# ---------------------------------------------------------------------------

MAX_TRADE_USD: float = float(os.getenv("MAX_TRADE_USD", "500"))
TAKER_FEE_FALLBACK: float = 0.001
MAKER_FEE_FALLBACK: float = 0.0002
PRICE_STALENESS_SEC: float = 3.0
REST_POLL_INTERVAL_SEC: float = 2.0

# Depegs below this are noise; at/above this a stablecoin pair is flagged
# and, past the kill-switch threshold, treated as a solvency event rather
# than an opportunity.
STABLE_DEPEG_THRESHOLD_PCT: float = 0.15
STABLE_DEPEG_KILL_SWITCH_PCT: float = 3.0

# Statistical-arbitrage (pairs trading) tunables.
STAT_ARB_LOOKBACK: int = 500
STAT_ARB_ENTRY_Z: float = 2.0
STAT_ARB_EXIT_Z: float = 0.5
STAT_ARB_STOP_LOSS_Z: float = 4.0
STAT_ARB_ADF_PVALUE_MAX: float = 0.05

# Triangular / multi-leg graph search.
MAX_CYCLE_LENGTH: int = 5

# On-chain execution.
DEFAULT_SLIPPAGE_TOLERANCE_PCT: float = 0.5
MEV_PRIORITY_FEE_USD_FALLBACK: float = 2.0

# ---------------------------------------------------------------------------
# Latency budgets (nanoseconds), used by analytics.metrics to flag stages
# that are exceeding their allotted time.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LatencyBudgets:
    feed_to_state_ns: int = 2_000_000       # 2ms
    state_to_detect_ns: int = 5_000_000     # 5ms
    detect_to_decision_ns: int = 2_000_000  # 2ms
    decision_to_ack_ns: int = 250_000_000   # 250ms (network round trip)


LATENCY_BUDGETS = LatencyBudgets()

# ---------------------------------------------------------------------------
# Risk defaults (overridable per-deployment; see risk/limits.py)
# ---------------------------------------------------------------------------

DAILY_LOSS_LIMIT_USD: float = float(os.getenv("DAILY_LOSS_LIMIT_USD", "1000"))
MAX_TRADES_PER_DAY: int = int(os.getenv("MAX_TRADES_PER_DAY", "2000"))
MAX_CONSECUTIVE_FAILURES: int = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "5"))

# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

RECORDER_OUTPUT_DIR: str = os.getenv("RECORDER_OUTPUT_DIR", "data/opportunities")
DECAY_CHECK_DELAYS_SEC: tuple[float, ...] = (0.1, 0.5, 2.0)
METRICS_HTTP_PORT: int = int(os.getenv("METRICS_HTTP_PORT", "9100"))
METRICS_LOG_DUMP_INTERVAL_SEC: float = 30.0

# ---------------------------------------------------------------------------
# Local dashboard (dashboard/server.py) -- a control plane for a running
# engine process, not a hosted service. Binds to localhost by default;
# only change DASHBOARD_HOST if you understand the risk of exposing a
# mode-switching, order-arming API beyond your own machine.
# ---------------------------------------------------------------------------

DASHBOARD_ENABLED: bool = os.getenv("DASHBOARD_ENABLED", "true").strip().lower() != "false"
DASHBOARD_HOST: str = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8420"))


@dataclass(slots=True)
class Settings:
    """Bundle of all engine-wide settings, useful for passing to components."""

    mode: str = MODE
    max_trade_usd: float = MAX_TRADE_USD
    taker_fee_fallback: float = TAKER_FEE_FALLBACK
    maker_fee_fallback: float = MAKER_FEE_FALLBACK
    price_staleness_sec: float = PRICE_STALENESS_SEC
    tiers: dict[str, TierConfig] = field(default_factory=lambda: dict(TIER_CONFIG))
    latency_budgets: LatencyBudgets = field(default_factory=LatencyBudgets)


def get_credentials(exchange_id: str) -> dict[str, str | None]:
    """Read API credentials for `exchange_id` from environment variables.

    Looks up `{EXCHANGE_ID}_API_KEY`, `{EXCHANGE_ID}_SECRET`, and
    `{EXCHANGE_ID}_PASSPHRASE`. Missing values resolve to `None` since
    public market data does not require credentials.
    """
    prefix = exchange_id.upper()
    return {
        "apiKey": os.getenv(f"{prefix}_API_KEY") or None,
        "secret": os.getenv(f"{prefix}_SECRET") or None,
        "password": os.getenv(f"{prefix}_PASSPHRASE") or None,
    }


def get_hot_wallet_credentials() -> dict[str, str | None]:
    """Read the DEX hot-wallet address/private key from the environment.

    Documented expectation (see README): this must be a dedicated hot
    wallet holding only funds you can afford to risk, never a main wallet.
    """
    return {
        "address": os.getenv("DEX_HOT_WALLET_ADDRESS") or None,
        "private_key": os.getenv("DEX_HOT_WALLET_PRIVATE_KEY") or None,
    }
