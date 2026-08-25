"""Live, mutable control state shared between the detection loop and the
local dashboard's control-plane API.

`ControlState` is deliberately a plain, `slots=True` dataclass mutated
in place rather than message-passed: `main.py`'s loop and
`dashboard/server.py`'s request handlers run in the same asyncio event
loop of the same process, so there is no cross-process or cross-thread
race to guard against -- a handler mutating `control.mode` takes effect
on the very next detection tick.

Switching `mode` to `"live"` has the same real-money consequences as
setting `ARB_MODE=live` in `.env`: the executor will fire real market
orders against pre-funded exchange balances. See
`execution/executor.py`'s module docstring and the README's "Before
running with real money" section before ever doing this.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

VALID_MODES = ("monitor", "paper", "live")


@dataclass(slots=True)
class ControlState:
    """Mutable run state: mode, running/paused, emergency stop, per-strategy toggles."""

    mode: str = "monitor"
    running: bool = True
    emergency_stop: bool = False
    strategy_enabled: dict[str, bool] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)

    def is_active(self) -> bool:
        """Whether the detection loop should scan/execute this tick."""
        return self.running and not self.emergency_stop
