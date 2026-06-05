"""Legacy singleton gas tracker.

Compatibility wrapper around :mod:`switchboard.gas_manager` that preserves the
old calendar-reset, global-budget behavior and the legacy method names used by
existing callers and tests.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from .gas_manager import GasLimits, GasManager, SECONDS_PER_DAY, SECONDS_PER_HOUR


class GasBudgetExhaustedError(Exception):
    """Raised when a transaction would exceed the current gas budget."""


class GasTracker:
    """Singleton global gas tracker with calendar resets.

    ``0`` limits mean "no limit" to preserve the legacy semantics.
    """

    _instance: Optional["GasTracker"] = None
    _lock = time.thread_time if hasattr(time, "thread_time") else None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, hourly_limit: int = 0, daily_limit: int = 0, time_source: Callable[[], float] = time.time):
        if getattr(self, "_initialized", False):
            return

        self._time_source = time_source
        self._manager = GasManager(
            default_limits=GasLimits(
                per_hour=hourly_limit or None,
                per_day=daily_limit or None,
            ),
            clock=self._time_source,
            mode="calendar",
            scope="global",
        )
        self._hourly_limit = hourly_limit
        self._daily_limit = daily_limit
        now = self._time_source()
        self._last_reset_hour = now
        self._last_reset_day = now
        self._spent_gas_hourly = 0
        self._spent_gas_daily = 0
        self._is_paused = False
        self._tracker_lock = threading.Lock()
        self._initialized = True
        self._sync_from_manager()

    # ----------------------------------------------------------------- helpers

    def _limits_to_manager(self) -> GasLimits:
        return GasLimits(
            per_hour=self._hourly_limit or None,
            per_day=self._daily_limit or None,
        )

    def _sync_from_manager(self) -> None:
        status = self._manager.status(None)
        self._spent_gas_hourly = status.spent_last_hour
        self._spent_gas_daily = status.spent_last_day
        self._is_paused = status.paused

    # ---------------------------------------------------------------- methods

    def _align_last_reset_day(self):
        now = self._time_source()
        self._last_reset_day = now - (now % SECONDS_PER_DAY)

    def _reset_if_needed(self):
        # The unified GasManager handles resets internally. Keep this method for
        # legacy callers and synchronize the mirrored attributes.
        self._sync_from_manager()

    def record_gas_usage(self, gas_used: int):
        with self._tracker_lock:
            self._manager.record(None, gas_used)
            self._sync_from_manager()

    def can_send_transaction(self, estimated_gas_cost: int) -> bool:
        with self._tracker_lock:
            allowed = self._manager.can_spend(None, estimated_gas_cost)
            self._sync_from_manager()
            return allowed

    def is_paused(self) -> bool:
        with self._tracker_lock:
            self._sync_from_manager()
            return self._is_paused

    def set_limits(self, hourly_limit: int = 0, daily_limit: int = 0):
        with self._tracker_lock:
            self._hourly_limit = hourly_limit
            self._daily_limit = daily_limit
            self._manager._default_limits = self._limits_to_manager()
            self._sync_from_manager()

    def get_current_spent(self) -> tuple[int, int]:
        with self._tracker_lock:
            self._sync_from_manager()
            return self._spent_gas_hourly, self._spent_gas_daily

    def reset_all(self):
        with self._tracker_lock:
            self._manager.reset(None)
            now = self._time_source()
            self._last_reset_hour = now
            self._align_last_reset_day()
            self._is_paused = False
            self._sync_from_manager()

