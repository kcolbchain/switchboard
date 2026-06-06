"""
Gas budget tracker for agent wallets.

Backward-compatible wrapper around GasManager (rolling-window mode).
Preserves the original API for existing imports.

See switchboard/gas_manager.py for the unified implementation.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Optional

from switchboard.gas_manager import (
    BudgetExhausted,
    GasLimits as _GasLimits,
    GasManager,
    GasStatus,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
)


# Re-export for backward compatibility
BudgetExhausted = BudgetExhausted
SECONDS_PER_HOUR = SECONDS_PER_HOUR
SECONDS_PER_DAY = SECONDS_PER_DAY


@dataclass(frozen=True)
class GasLimits:
    """Per-wallet gas ceilings. ``None`` disables the corresponding window."""

    per_hour: Optional[int] = None
    per_day: Optional[int] = None


@dataclass
class BudgetStatus:
    """Snapshot of a wallet's current spend vs. its limits."""

    wallet: str
    limits: GasLimits
    spent_last_hour: int
    spent_last_day: int
    paused: bool

    @property
    def remaining_hour(self) -> Optional[int]:
        if self.limits.per_hour is None:
            return None
        return max(0, self.limits.per_hour - self.spent_last_hour)

    @property
    def remaining_day(self) -> Optional[int]:
        if self.limits.per_day is None:
            return None
        return max(0, self.limits.per_day - self.spent_last_day)


class GasBudgetTracker:
    """Tracks cumulative gas per wallet and enforces rolling-window limits.

    This is a backward-compatible wrapper around GasManager.
    """

    def __init__(
        self,
        default_limits: GasLimits = GasLimits(),
        clock: Callable[[], float] = time.time,
    ):
        self._manager = GasManager(
            hourly_limit=default_limits.per_hour or 0,
            daily_limit=default_limits.per_day or 0,
            window_mode="rolling",
            clock=clock,
        )
        self._default_limits = default_limits

    def set_limits(self, wallet: str, limits: GasLimits) -> None:
        self._manager.set_limits(
            wallet,
            hourly=limits.per_hour or 0,
            daily=limits.per_day or 0,
        )

    def limits_for(self, wallet: str) -> GasLimits:
        mlim = self._manager.limits_for(wallet)
        return GasLimits(per_hour=mlim.hourly or None, per_day=mlim.daily or None)

    def can_spend(self, wallet: str, estimated_gas: int) -> bool:
        return self._manager.can_spend(wallet, estimated_gas)

    def check(self, wallet: str, estimated_gas: int) -> None:
        self._manager.check(wallet, estimated_gas)

    def record(self, wallet: str, gas_used: int) -> BudgetStatus:
        status = self._manager.record(wallet, gas_used)
        return self._to_budget_status(status)

    def status(self, wallet: str) -> BudgetStatus:
        status = self._manager.status(wallet)
        return self._to_budget_status(status)

    def resume(self, wallet: str) -> None:
        self._manager.resume(wallet)

    def reset(self, wallet: str) -> None:
        self._manager.reset(wallet)

    def _to_budget_status(self, s: GasStatus) -> BudgetStatus:
        return BudgetStatus(
            wallet=s.wallet,
            limits=GasLimits(
                per_hour=s.limits.hourly or None,
                per_day=s.limits.daily or None,
            ),
            spent_last_hour=s.spent_hourly,
            spent_last_day=s.spent_daily,
            paused=s.paused,
        )
