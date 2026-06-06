"""
Unified gas management for agent wallets.

Combines the functionality of gas_budget.py (rolling-window, per-wallet)
and gas_tracker.py (calendar-reset, global singleton) into a single,
configurable GasManager class.

Design doc: docs/gas_manager_design.md
"""

from __future__ import annotations

import datetime
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Deque, Dict, Optional, Tuple


SECONDS_PER_HOUR = 3_600
SECONDS_PER_DAY = 86_400
GLOBAL_WALLET = "__global__"


class WindowMode(Enum):
    ROLLING = "rolling"
    CALENDAR = "calendar"


class BudgetExhausted(RuntimeError):
    """Raised when a wallet would exceed its configured gas budget."""

    def __init__(self, status: "GasStatus"):
        self.status = status
        # args[0] is the status object for backward compatibility
        # (old tests check exc.value.args[0].wallet)
        super().__init__(status)


@dataclass(frozen=True)
class GasLimits:
    """Per-wallet gas ceilings. 0 or None disables the corresponding window."""

    hourly: int = 0
    daily: int = 0


@dataclass
class GasStatus:
    """Snapshot of a wallet's current spend vs. its limits."""

    wallet: str
    limits: GasLimits
    spent_hourly: int
    spent_daily: int
    paused: bool
    window_mode: WindowMode

    @property
    def remaining_hourly(self) -> int:
        if self.limits.hourly <= 0:
            return -1  # unlimited
        return max(0, self.limits.hourly - self.spent_hourly)

    @property
    def remaining_daily(self) -> int:
        if self.limits.daily <= 0:
            return -1  # unlimited
        return max(0, self.limits.daily - self.spent_daily)


@dataclass
class _WalletState:
    """Internal per-wallet state. Protected by the manager lock."""

    # Rolling mode: (timestamp, gas_used) entries
    events: Deque = field(default_factory=deque)
    # Calendar mode: simple counters
    spent_hourly: int = 0
    spent_daily: int = 0
    last_reset_hour: float = 0.0
    last_reset_day: float = 0.0
    # Common
    paused: bool = False
    limits: Optional[GasLimits] = None


class GasManager:
    """Unified gas manager supporting rolling-window and calendar modes.

    Parameters
    ----------
    hourly_limit:
        Default hourly gas limit. 0 means no limit.
    daily_limit:
        Default daily gas limit. 0 means no limit.
    window_mode:
        "rolling" for deque-based sliding window (from gas_budget.py),
        "calendar" for hour-aligned resets (from gas_tracker.py).
    singleton:
        If True, behaves as a global singleton (like GasTracker).
    clock:
        Injectable seconds-resolution clock. Defaults to time.time.
    """

    _singleton_instance: Optional["GasManager"] = None
    _singleton_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        """Support singleton mode when requested."""
        if kwargs.get("singleton", False):
            if cls._singleton_instance is None:
                with cls._singleton_lock:
                    if cls._singleton_instance is None:
                        instance = super().__new__(cls)
                        instance._is_singleton = True
                        cls._singleton_instance = instance
            return cls._singleton_instance
        return super().__new__(cls)

    def __init__(
        self,
        hourly_limit: int = 0,
        daily_limit: int = 0,
        window_mode: str = "rolling",
        singleton: bool = False,
        clock: Callable[[], float] = time.time,
    ):
        if hasattr(self, "_initialized"):
            return

        self._default_limits = GasLimits(hourly=hourly_limit, daily=daily_limit)
        self._window_mode = WindowMode(window_mode)
        self._clock = clock
        self._lock = threading.Lock()
        self._wallets: Dict[str, _WalletState] = defaultdict(_WalletState)
        self._initialized = True

        # Calendar mode: align initial day to UTC midnight
        if self._window_mode == WindowMode.CALENDAR:
            now = self._clock()
            for state in self._wallets.values():
                state.last_reset_hour = now
                state.last_reset_day = self._align_to_day_start(now)

    # ---- Configuration ---------------------------------------------------

    def set_limits(self, wallet: str, hourly: int = 0, daily: int = 0) -> None:
        """Set per-wallet limits, overriding defaults."""
        with self._lock:
            state = self._wallets[wallet]
            state.limits = GasLimits(hourly=hourly, daily=daily)
            if self._window_mode == WindowMode.CALENDAR:
                self._maybe_reset_calendar(state)
            self._update_pause_state(state, state.limits or self._default_limits)

    def limits_for(self, wallet: str) -> GasLimits:
        """Return effective limits for a wallet."""
        with self._lock:
            state = self._wallets[wallet]
            return state.limits if state.limits is not None else self._default_limits

    # ---- Core Operations -------------------------------------------------

    def can_spend(self, wallet: str, estimated_gas: int) -> bool:
        """Return True if estimated_gas fits within all active windows."""
        if estimated_gas < 0:
            raise ValueError("estimated_gas must be non-negative")

        with self._lock:
            state = self._wallets[wallet]
            limits = state.limits if state.limits is not None else self._default_limits

            if self._window_mode == WindowMode.ROLLING:
                self._evict_rolling(state)
            else:
                self._maybe_reset_calendar(state)

            if state.paused:
                return False

            if limits.hourly > 0:
                spent = self._get_hourly_spent(state)
                if spent + estimated_gas > limits.hourly:
                    return False

            if limits.daily > 0:
                spent = self._get_daily_spent(state)
                if spent + estimated_gas > limits.daily:
                    return False

            return True

    def check(self, wallet: str, estimated_gas: int) -> None:
        """Raise BudgetExhausted if estimated_gas cannot be spent."""
        if not self.can_spend(wallet, estimated_gas):
            raise BudgetExhausted(self.status(wallet))

    def record(self, wallet: str, gas_used: int) -> GasStatus:
        """Record gas spend and return new status. Auto-pauses if limit crossed."""
        if gas_used < 0:
            raise ValueError("gas_used must be non-negative")

        with self._lock:
            state = self._wallets[wallet]
            limits = state.limits if state.limits is not None else self._default_limits

            if self._window_mode == WindowMode.ROLLING:
                self._evict_rolling(state)
                now = self._clock()
                state.events.append((now, gas_used))
            else:
                self._maybe_reset_calendar(state)
                state.spent_hourly += gas_used
                state.spent_daily += gas_used

            # Check if we should pause
            if limits.hourly > 0 and self._get_hourly_spent(state) >= limits.hourly:
                state.paused = True
            if limits.daily > 0 and self._get_daily_spent(state) >= limits.daily:
                state.paused = True

            return self._make_status(wallet, state, limits)

    # ---- Introspection ---------------------------------------------------

    def status(self, wallet: str) -> GasStatus:
        """Return current status for a wallet."""
        with self._lock:
            state = self._wallets[wallet]
            limits = state.limits if state.limits is not None else self._default_limits

            if self._window_mode == WindowMode.ROLLING:
                self._evict_rolling(state)
            else:
                self._maybe_reset_calendar(state)

            return self._make_status(wallet, state, limits)

    # ---- Control ---------------------------------------------------------

    def pause(self, wallet: str) -> None:
        """Manually pause a wallet."""
        with self._lock:
            self._wallets[wallet].paused = True

    def resume(self, wallet: str) -> None:
        """Manually unpause a wallet."""
        with self._lock:
            self._wallets[wallet].paused = False

    def reset(self, wallet: str) -> None:
        """Clear all recorded spend for a wallet."""
        with self._lock:
            self._wallets[wallet] = _WalletState()
            if self._window_mode == WindowMode.CALENDAR:
                now = self._clock()
                self._wallets[wallet].last_reset_hour = now
                self._wallets[wallet].last_reset_day = self._align_to_day_start(now)

    # ---- Global/singleton convenience methods ----------------------------

    def can_send_transaction(self, estimated_gas_cost: int) -> bool:
        """Global mode: check if transaction fits (uses GLOBAL_WALLET)."""
        return self.can_spend(GLOBAL_WALLET, estimated_gas_cost)

    def record_gas_usage(self, gas_used: int) -> None:
        """Global mode: record gas usage (uses GLOBAL_WALLET)."""
        self.record(GLOBAL_WALLET, gas_used)

    def is_paused(self) -> bool:
        """Global mode: check if paused (uses GLOBAL_WALLET)."""
        return self.status(GLOBAL_WALLET).paused

    def get_current_spent(self) -> Tuple[int, int]:
        """Global mode: return (hourly_spent, daily_spent)."""
        s = self.status(GLOBAL_WALLET)
        return (s.spent_hourly, s.spent_daily)

    def reset_all(self) -> None:
        """Global mode: reset all state."""
        with self._lock:
            self._wallets.clear()
            if self._window_mode == WindowMode.CALENDAR:
                now = self._clock()
                state = self._wallets[GLOBAL_WALLET]
                state.last_reset_hour = now
                state.last_reset_day = self._align_to_day_start(now)

    # ---- Internals -------------------------------------------------------

    def _get_hourly_spent(self, state: _WalletState) -> int:
        """Get hourly spent based on window mode."""
        if self._window_mode == WindowMode.ROLLING:
            cutoff = self._clock() - SECONDS_PER_HOUR
            return sum(gas for ts, gas in state.events if ts > cutoff)
        return state.spent_hourly

    def _get_daily_spent(self, state: _WalletState) -> int:
        """Get daily spent based on window mode."""
        if self._window_mode == WindowMode.ROLLING:
            cutoff = self._clock() - SECONDS_PER_DAY
            return sum(gas for ts, gas in state.events if ts > cutoff)
        return state.spent_daily

    def _evict_rolling(self, state: _WalletState) -> None:
        """Drop events that have aged out of the daily window."""
        cutoff = self._clock() - SECONDS_PER_DAY
        while state.events and state.events[0][0] <= cutoff:
            state.events.popleft()

    def _maybe_reset_calendar(self, state: _WalletState) -> None:
        """Reset hourly/daily counters if calendar boundaries crossed."""
        now = self._clock()

        # Hourly reset
        if now - state.last_reset_hour >= SECONDS_PER_HOUR:
            state.spent_hourly = 0
            state.last_reset_hour = now - (now % SECONDS_PER_HOUR)

        # Daily reset
        current_day = self._timestamp_to_day(now)
        last_day = self._timestamp_to_day(state.last_reset_day)
        if current_day > last_day:
            state.spent_daily = 0
            state.last_reset_day = self._align_to_day_start(now)

        # Auto-unpause if limits are now satisfied
        limits = state.limits if state.limits is not None else self._default_limits
        self._update_pause_state(state, limits)

    def _update_pause_state(self, state: _WalletState, limits: GasLimits) -> None:
        """Update pause flag based on current spending and limits.

        Pauses if any limit is exceeded. In calendar mode, also unpauses when
        limits are satisfied (auto-reset). In rolling mode, stays paused until
        explicit resume() — this matches the original GasBudgetTracker behavior.
        """
        over_hourly = limits.hourly > 0 and self._get_hourly_spent(state) >= limits.hourly
        over_daily = limits.daily > 0 and self._get_daily_spent(state) >= limits.daily

        if over_hourly or over_daily:
            state.paused = True
        elif self._window_mode == WindowMode.CALENDAR:
            # Calendar mode: auto-unpause when limits are satisfied
            state.paused = False
        # Rolling mode: don't auto-unpause (stay sticky until resume())

    def _make_status(self, wallet: str, state: _WalletState, limits: GasLimits) -> GasStatus:
        return GasStatus(
            wallet=wallet,
            limits=limits,
            spent_hourly=self._get_hourly_spent(state),
            spent_daily=self._get_daily_spent(state),
            paused=state.paused,
            window_mode=self._window_mode,
        )

    @staticmethod
    def _align_to_day_start(timestamp: float) -> float:
        """Align timestamp to start of UTC day."""
        dt = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp()

    @staticmethod
    def _timestamp_to_day(timestamp: float) -> datetime.date:
        """Convert timestamp to UTC date."""
        return datetime.datetime.fromtimestamp(
            timestamp, tz=datetime.timezone.utc
        ).date()

    @classmethod
    def reset_singleton(cls) -> None:
        """Reset the singleton instance (for testing)."""
        with cls._singleton_lock:
            cls._singleton_instance = None
