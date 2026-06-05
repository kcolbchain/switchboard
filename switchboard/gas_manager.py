"""Unified gas management for Switchboard.

This module consolidates the two legacy policies that existed in
``gas_budget.py`` and ``gas_tracker.py``:

- rolling-window, per-wallet budgets
- calendar-reset, global budgets

The public API is intentionally small and wrapper-friendly so the old imports
continue to work while the implementation lives in one place.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Optional

SECONDS_PER_HOUR = 3_600
SECONDS_PER_DAY = 86_400


class BudgetExhausted(RuntimeError):
    """Raised when a spend request would exceed the configured budget."""


@dataclass(frozen=True)
class GasLimits:
    """Gas ceilings for a wallet or global budget.

    ``None`` disables the corresponding limit.
    """

    per_hour: Optional[int] = None
    per_day: Optional[int] = None


@dataclass
class BudgetStatus:
    """Snapshot of current spend and pause state."""

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


@dataclass
class _RollingLedger:
    events: Deque = field(default_factory=deque)
    sum_hour: int = 0
    sum_day: int = 0
    paused: bool = False


@dataclass
class _CalendarLedger:
    spent_hour: int = 0
    spent_day: int = 0
    last_reset_hour: float = 0.0
    last_reset_day: float = 0.0
    paused: bool = False


class GasManager:
    """Unified gas manager.

    Parameters
    ----------
    default_limits:
        Default limits applied when a wallet has no override.
    clock:
        Injected clock used for deterministic tests.
    mode:
        ``"rolling"`` or ``"calendar"``.
    scope:
        ``"per-wallet"`` or ``"global"``.
    """

    def __init__(
        self,
        default_limits: GasLimits = GasLimits(),
        clock: Callable[[], float] = time.time,
        mode: str = "rolling",
        scope: str = "per-wallet",
    ):
        if mode not in {"rolling", "calendar"}:
            raise ValueError("mode must be 'rolling' or 'calendar'")
        if scope not in {"per-wallet", "global"}:
            raise ValueError("scope must be 'per-wallet' or 'global'")

        self._default_limits = default_limits
        self._clock = clock
        self._mode = mode
        self._scope = scope
        self._lock = threading.Lock()
        self._limits: Dict[str, GasLimits] = {}
        self._ledgers: Dict[str, object] = defaultdict(self._new_ledger)
        self._global_key = "__global__"

    # ------------------------------------------------------------------ config

    def _new_ledger(self):
        if self._mode == "rolling":
            return _RollingLedger()
        now = self._clock()
        return _CalendarLedger(
            spent_hour=0,
            spent_day=0,
            last_reset_hour=now,
            last_reset_day=self._align_day(now),
            paused=False,
        )

    def _align_day(self, now: float) -> float:
        return (now // SECONDS_PER_DAY) * SECONDS_PER_DAY

    def _key(self, wallet: Optional[str]) -> str:
        if self._scope == "global":
            return self._global_key
        if wallet is None:
            raise ValueError("wallet is required in per-wallet mode")
        return wallet

    def set_limits(self, wallet: str, limits: GasLimits) -> None:
        with self._lock:
            self._limits[wallet] = limits

    def limits_for(self, wallet: Optional[str] = None) -> GasLimits:
        if self._scope == "global":
            return self._limits.get(self._global_key, self._default_limits)
        if wallet is None:
            raise ValueError("wallet is required in per-wallet mode")
        return self._limits.get(wallet, self._default_limits)

    # ----------------------------------------------------------------- internals

    def _evict_rolling(self, ledger: _RollingLedger) -> None:
        now = self._clock()
        day_cutoff = now - SECONDS_PER_DAY
        hour_cutoff = now - SECONDS_PER_HOUR

        while ledger.events and ledger.events[0][0] <= day_cutoff:
            ts, gas = ledger.events.popleft()
            ledger.sum_day -= gas
            if ts > hour_cutoff:
                ledger.sum_hour -= gas

        ledger.sum_hour = sum(gas for ts, gas in ledger.events if ts > hour_cutoff)

    def _evict_calendar(self, ledger: _CalendarLedger) -> None:
        now = self._clock()
        if now - ledger.last_reset_hour >= SECONDS_PER_HOUR:
            ledger.spent_hour = 0
            ledger.last_reset_hour = now - (now % SECONDS_PER_HOUR)

        current_day = self._align_day(now)
        if current_day > ledger.last_reset_day:
            ledger.spent_day = 0
            ledger.last_reset_day = current_day

        # In calendar mode the pause state is always derived from current spend.
        limits = self._active_limits_for_current_scope()
        ledger.paused = self._paused_from_totals(
            spent_hour=ledger.spent_hour,
            spent_day=ledger.spent_day,
            limits=limits,
        )

    def _active_limits_for_current_scope(self, wallet: Optional[str] = None) -> GasLimits:
        return self.limits_for(wallet)

    @staticmethod
    def _paused_from_totals(
        spent_hour: int, spent_day: int, limits: GasLimits
    ) -> bool:
        hour_ok = limits.per_hour is None or spent_hour < limits.per_hour
        day_ok = limits.per_day is None or spent_day < limits.per_day
        return not (hour_ok and day_ok)

    def _status_locked(
        self, wallet: str, ledger: object, limits: GasLimits
    ) -> BudgetStatus:
        if self._mode == "rolling":
            assert isinstance(ledger, _RollingLedger)
            return BudgetStatus(
                wallet=wallet,
                limits=limits,
                spent_last_hour=ledger.sum_hour,
                spent_last_day=ledger.sum_day,
                paused=ledger.paused,
            )
        assert isinstance(ledger, _CalendarLedger)
        return BudgetStatus(
            wallet=wallet,
            limits=limits,
            spent_last_hour=ledger.spent_hour,
            spent_last_day=ledger.spent_day,
            paused=ledger.paused,
        )

    # ---------------------------------------------------------------- enforcement

    def can_spend(self, wallet: Optional[str], estimated_gas: int) -> bool:
        if estimated_gas < 0:
            raise ValueError("estimated_gas must be non-negative")

        with self._lock:
            key = self._key(wallet)
            ledger = self._ledgers[key]
            limits = self.limits_for(wallet if self._scope == "per-wallet" else None)

            if self._mode == "rolling":
                assert isinstance(ledger, _RollingLedger)
                self._evict_rolling(ledger)
                if ledger.paused:
                    return False
                if limits.per_hour is not None and ledger.sum_hour + estimated_gas > limits.per_hour:
                    return False
                if limits.per_day is not None and ledger.sum_day + estimated_gas > limits.per_day:
                    return False
                return True

            assert isinstance(ledger, _CalendarLedger)
            self._evict_calendar(ledger)
            if ledger.paused:
                return False
            if limits.per_hour is not None and ledger.spent_hour + estimated_gas > limits.per_hour:
                return False
            if limits.per_day is not None and ledger.spent_day + estimated_gas > limits.per_day:
                return False
            return True

    def check(self, wallet: Optional[str], estimated_gas: int) -> None:
        if not self.can_spend(wallet, estimated_gas):
            raise BudgetExhausted(self.status(wallet))

    def record(self, wallet: Optional[str], gas_used: int) -> BudgetStatus:
        if gas_used < 0:
            raise ValueError("gas_used must be non-negative")

        with self._lock:
            key = self._key(wallet)
            ledger = self._ledgers[key]
            limits = self.limits_for(wallet if self._scope == "per-wallet" else None)

            if self._mode == "rolling":
                assert isinstance(ledger, _RollingLedger)
                self._evict_rolling(ledger)
                now = self._clock()
                ledger.events.append((now, gas_used))
                ledger.sum_hour += gas_used
                ledger.sum_day += gas_used
                ledger.paused = self._paused_from_totals(
                    spent_hour=ledger.sum_hour,
                    spent_day=ledger.sum_day,
                    limits=limits,
                )
                return self._status_locked(key, ledger, limits)

            assert isinstance(ledger, _CalendarLedger)
            self._evict_calendar(ledger)
            ledger.spent_hour += gas_used
            ledger.spent_day += gas_used
            ledger.paused = self._paused_from_totals(
                spent_hour=ledger.spent_hour,
                spent_day=ledger.spent_day,
                limits=limits,
            )
            return self._status_locked(key, ledger, limits)

    # ---------------------------------------------------------------- introspect

    def status(self, wallet: Optional[str]) -> BudgetStatus:
        with self._lock:
            key = self._key(wallet)
            ledger = self._ledgers[key]
            limits = self.limits_for(wallet if self._scope == "per-wallet" else None)
            if self._mode == "rolling":
                self._evict_rolling(ledger)  # type: ignore[arg-type]
            else:
                self._evict_calendar(ledger)  # type: ignore[arg-type]
            return self._status_locked(key, ledger, limits)

    def resume(self, wallet: Optional[str]) -> None:
        with self._lock:
            key = self._key(wallet)
            ledger = self._ledgers[key]
            if self._mode == "rolling":
                assert isinstance(ledger, _RollingLedger)
                ledger.paused = False
            else:
                assert isinstance(ledger, _CalendarLedger)
                ledger.paused = False

    def reset(self, wallet: Optional[str]) -> None:
        with self._lock:
            key = self._key(wallet)
            self._ledgers[key] = self._new_ledger()

    # Convenience for wrappers / tests.
    def spent(self, wallet: Optional[str]) -> tuple[int, int]:
        s = self.status(wallet)
        return s.spent_last_hour, s.spent_last_day
