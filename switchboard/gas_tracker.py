"""
Gas tracker for agent wallets.

Backward-compatible wrapper around GasManager (calendar-reset, singleton mode).
Preserves the original API for existing imports.

See switchboard/gas_manager.py for the unified implementation.
"""

from __future__ import annotations

import time
import threading
from typing import Optional, Callable, Tuple

from switchboard.gas_manager import (
    GasManager,
    GLOBAL_WALLET,
)


class GasBudgetExhaustedError(Exception):
    """Raised when gas budget is exhausted."""
    pass


class GasTracker:
    """
    Tracks cumulative gas spent and enforces configurable hourly and daily limits.
    Backward-compatible wrapper around GasManager (calendar mode, singleton).
    """

    _instance: Optional["GasTracker"] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        hourly_limit: int = 0,
        daily_limit: int = 0,
        time_source: Callable[[], float] = time.time,
    ):
        if not hasattr(self, "_initialized"):
            self._manager = GasManager(
                hourly_limit=hourly_limit,
                daily_limit=daily_limit,
                window_mode="calendar",
                clock=time_source,
            )
            self._hourly_limit = hourly_limit
            self._daily_limit = daily_limit
            self._time_source = time_source
            self._initialized = True

    def record_gas_usage(self, gas_used: int) -> None:
        self._manager.record_gas_usage(gas_used)

    def can_send_transaction(self, estimated_gas_cost: int) -> bool:
        return self._manager.can_send_transaction(estimated_gas_cost)

    def is_paused(self) -> bool:
        return self._manager.is_paused()

    def set_limits(self, hourly_limit: int = 0, daily_limit: int = 0) -> None:
        self._hourly_limit = hourly_limit
        self._daily_limit = daily_limit
        self._manager.set_limits(GLOBAL_WALLET, hourly_limit, daily_limit)

    def get_current_spent(self) -> Tuple[int, int]:
        return self._manager.get_current_spent()

    def reset_all(self) -> None:
        self._manager.reset_all()

    @classmethod
    def reset_singleton(cls) -> None:
        """Reset singleton for testing."""
        cls._instance = None
        GasManager.reset_singleton()
