"""Gas budget tracker for agent wallets.

Compatibility wrapper around :mod:`switchboard.gas_manager`.
"""

from __future__ import annotations

import time

from .gas_manager import (
    BudgetExhausted,
    BudgetStatus,
    GasLimits,
    GasManager,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
)


class GasBudgetTracker(GasManager):
    """Per-wallet rolling-window gas budgets.

    This preserves the legacy API used by the rest of the package and tests.
    """

    def __init__(
        self,
        default_limits: GasLimits = GasLimits(),
        clock=time.time,
    ):
        super().__init__(default_limits=default_limits, clock=clock, mode="rolling", scope="per-wallet")


# Backward-friendly alias used in a few docs.
GasBudget = GasBudgetTracker
