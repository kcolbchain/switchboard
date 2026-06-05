"""
Unified gas budget manager — reconciles ``gas_budget.py`` and ``gas_tracker.py``.

Combines per-wallet accounting (GasBudgetTracker) with auto-unpause on window
rollover (GasTracker), providing a single drop-in replacement for both.

Design
------
- Per-wallet rolling-window enforcement (deque-based, not calendar buckets).
- Thread-safe via single reentrant lock.
- Auto-unpauses a wallet when the rolling window frees up capacity.
- Pluggable clock for deterministic tests.
- Pure Python, zero new runtime deps.

Usage
-----
    manager = GasManager(
        default_limits=GasLimits(per_hour=2_000_000, per_day=20_000_000),
    )

    if not manager.can_spend("0xAgent", estimated_gas):
        raise BudgetExhausted(manager.status("0xAgent"))

    # ... send tx ...
    manager.record("0xAgent", gas_used=receipt.gasUsed)

    # GasTracker-compatible alias:
    manager.record_gas_usage("0xAgent", gas_used)

References
----------
- Issue #97: https://github.com/kcolbchain/switchboard/issues/97
- Original modules: gas_budget.py (GasBudgetTracker), gas_tracker.py (GasTracker)
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Optional, Tuple


SECONDS_PER_HOUR = 3_600
SECONDS_PER_DAY = 86_400


class BudgetExhausted(RuntimeError):
    """Raised when a wallet would exceed its configured gas budget."""


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


@dataclass
class _WalletLedger:
    """Internal per-wallet state. Protected by the tracker lock."""

    events: Deque = field(default_factory=deque)
    sum_hour: int = 0
    sum_day: int = 0
    paused: bool = False


class GasManager:
    """Tracks cumulative gas per wallet and enforces rolling-window limits.

    Unifies the API of :class:`GasBudgetTracker` and :class:`GasTracker`
    so that either codebase can migrate without behavioural surprises.

    Parameters
    ----------
    default_limits:
        Applied to any wallet that does not have explicit limits set via
        :meth:`set_limits`.
    clock:
        Injectable seconds-resolution clock.  Defaults to :func:`time.time`.
        Tests should pass a controllable clock.
    """

    def __init__(
        self,
        default_limits: GasLimits = GasLimits(),
        clock: Callable[[], float] = time.time,
    ):
        self._default_limits = default_limits
        self._clock = clock
        self._lock = threading.Lock()
        self._ledgers: Dict[str, _WalletLedger] = defaultdict(_WalletLedger)
        self._limits: Dict[str, GasLimits] = {}

    # ---- configuration -------------------------------------------------

    def set_limits(self, wallet: str, limits: GasLimits) -> None:
        """Override the default limits for ``wallet``."""
        with self._lock:
            self._limits[wallet] = limits
            self._update_pause_state_locked(self._ledgers[wallet], limits)

    def limits_for(self, wallet: str) -> GasLimits:
        return self._limits.get(wallet, self._default_limits)

    # ---- GasBudgetTracker-compatible API --------------------------------

    def can_spend(self, wallet: str, estimated_gas: int) -> bool:
        """Return ``True`` if ``estimated_gas`` fits within every active window."""
        if estimated_gas < 0:
            raise ValueError("estimated_gas must be non-negative")

        with self._lock:
            ledger = self._ledgers[wallet]
            limits = self.limits_for(wallet)
            self._evict_locked(ledger, limits)

            if ledger.paused:
                return False
            if limits.per_hour is not None and ledger.sum_hour + estimated_gas > limits.per_hour:
                return False
            if limits.per_day is not None and ledger.sum_day + estimated_gas > limits.per_day:
                return False
            return True

    def check(self, wallet: str, estimated_gas: int) -> None:
        """Raise :class:`BudgetExhausted` if ``estimated_gas`` cannot be spent."""
        if not self.can_spend(wallet, estimated_gas):
            raise BudgetExhausted(self.status(wallet))

    def record(self, wallet: str, gas_used: int) -> BudgetStatus:
        """Record a post-confirmation gas spend and return the new status.

        Auto-pauses the wallet if a limit is crossed after this record.
        """
        if gas_used < 0:
            raise ValueError("gas_used must be non-negative")

        with self._lock:
            ledger = self._ledgers[wallet]
            limits = self.limits_for(wallet)
            self._evict_locked(ledger, limits)

            now = self._clock()
            ledger.events.append((now, gas_used))
            ledger.sum_hour += gas_used
            ledger.sum_day += gas_used
            crossed = (
                (limits.per_hour is not None and ledger.sum_hour >= limits.per_hour)
                or (limits.per_day is not None and ledger.sum_day >= limits.per_day)
            )
            if crossed:
                ledger.paused = True

            return self._status_locked(wallet, ledger, limits)

    # ---- GasTracker-compatible API --------------------------------------

    def can_send_transaction(self, wallet: str, estimated_gas: int) -> bool:
        """Alias for :meth:`can_spend` — GasTracker compatibility."""
        return self.can_spend(wallet, estimated_gas)

    def record_gas_usage(self, wallet: str, gas_used: int) -> None:
        """Alias for ``record()`` — GasTracker compatibility (no return)."""
        self.record(wallet, gas_used)

    def is_paused(self, wallet: str) -> bool:
        """Return whether ``wallet`` is currently paused."""
        return self.status(wallet).paused

    def get_current_spent(self, wallet: str) -> Tuple[int, int]:
        """Return ``(spent_last_hour, spent_last_day)`` — GasTracker compatibility."""
        s = self.status(wallet)
        return (s.spent_last_hour, s.spent_last_day)

    # ---- introspection -------------------------------------------------

    def status(self, wallet: str) -> BudgetStatus:
        with self._lock:
            ledger = self._ledgers[wallet]
            limits = self.limits_for(wallet)
            self._evict_locked(ledger, limits)
            return self._status_locked(wallet, ledger, limits)

    def resume(self, wallet: str) -> None:
        """Manually unpause a wallet."""
        with self._lock:
            self._ledgers[wallet].paused = False

    def reset(self, wallet: str) -> None:
        """Clear all recorded spend for ``wallet``."""
        with self._lock:
            self._ledgers[wallet] = _WalletLedger()

    def reset_all(self) -> None:
        """Clear all wallets — GasTracker compatibility."""
        with self._lock:
            self._ledgers.clear()

    # ---- internals -----------------------------------------------------

    def _evict_locked(
        self, ledger: _WalletLedger, limits: Optional[GasLimits] = None
    ) -> None:
        """Drop events that have aged out of both windows and refresh sums.

        Auto-unpauses if the remaining spend is below every active limit
        (requires ``limits`` to be passed for the check).
        """
        now = self._clock()
        day_cutoff = now - SECONDS_PER_DAY
        hour_cutoff = now - SECONDS_PER_HOUR

        while ledger.events and ledger.events[0][0] <= day_cutoff:
            ts, gas = ledger.events.popleft()
            ledger.sum_day -= gas

        # Rebuild sum_hour from events that fall within the hour window
        ledger.sum_hour = 0
        for ts, gas in ledger.events:
            if ts > hour_cutoff:
                ledger.sum_hour += gas

        if ledger.paused and limits is not None:
            still_exhausted = (
                (limits.per_hour is not None and ledger.sum_hour >= limits.per_hour)
                or (limits.per_day is not None and ledger.sum_day >= limits.per_day)
            )
            if not still_exhausted:
                ledger.paused = False

    def _update_pause_state_locked(
        self, ledger: _WalletLedger, limits: GasLimits
    ) -> None:
        """Re-evaluate pause flag after limit changes."""
        if not ledger.paused:
            return
        still_exhausted = (
            (limits.per_hour is not None and ledger.sum_hour >= limits.per_hour)
            or (limits.per_day is not None and ledger.sum_day >= limits.per_day)
        )
        if not still_exhausted:
            ledger.paused = False

    def _status_locked(
        self, wallet: str, ledger: _WalletLedger, limits: GasLimits
    ) -> BudgetStatus:
        return BudgetStatus(
            wallet=wallet,
            limits=limits,
            spent_last_hour=ledger.sum_hour,
            spent_last_day=ledger.sum_day,
            paused=ledger.paused,
        )
