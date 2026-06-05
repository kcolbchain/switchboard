from __future__ import annotations

import time
import threading
from typing import Callable, Optional
from switchboard.gas_manager import GasManager, GasLimits, GasLimitExceededError

class GasTracker:
    _instance: Optional['GasTracker'] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, hourly_limit: int = 0, daily_limit: int = 0, time_source: Callable[[], float] = time.time):
        if not hasattr(self, '_initialized'):
            limits = GasLimits(
                per_hour=hourly_limit if hourly_limit > 0 else None,
                per_day=daily_limit if daily_limit > 0 else None
            )
            self._manager = GasManager(mode="calendar", global_limits=limits, clock=time_source)
            self._initialized = True

    def record_gas_usage(self, gas_used: int):
        self._manager.record(None, gas_used)

    def can_send_transaction(self, estimated_gas_cost: int) -> bool:
        return self._manager.can_spend(None, estimated_gas_cost)

    def is_paused(self) -> bool:
        return self._manager.status(None).paused

    def set_limits(self, hourly_limit: int = 0, daily_limit: int = 0):
        limits = GasLimits(
            per_hour=hourly_limit if hourly_limit > 0 else None,
            per_day=daily_limit if daily_limit > 0 else None
        )
        self._manager.set_global_limits(limits)

    def get_current_spent(self) -> tuple[int, int]:
        status = self._manager.status(None)
        return status.spent_last_hour, status.spent_last_day

    def reset_all(self):
        self._manager.reset(None)
        self._manager.resume(None)

    @property
    def _hourly_limit(self):
        limit = self._manager._global_limits.per_hour
        return limit if limit is not None else 0
        
    @property
    def _daily_limit(self):
        limit = self._manager._global_limits.per_day
        return limit if limit is not None else 0
