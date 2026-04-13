"""
Gas budget tracker for agent wallets.

Tracks cumulative gas spent per wallet over rolling hour and day windows,
enforces configurable limits, and pauses execution when a budget is exhausted.

Implements issue #5:
    https://github.com/kcolbchain/switchboard/issues/5

Design goals
------------
- Monotonic, thread-safe accounting — safe from multiple agent worker threads.
- Rolling-window enforcement (not calendar buckets), so a burst at 23:59 does
  not reset to zero one minute later.
- Pluggable clock for deterministic tests.
- Pure Python, zero new runtime deps.

Typical usage::

    tracker = GasBudgetTracker(
        default_limits=GasLimits(per_hour=2_000_000, per_day=20_000_000),
    )

    if not tracker.can_spend(wallet, estimated_gas):
        raise BudgetExhausted(tracker.status(wallet))

    # ... send tx ...
    tracker.record(wallet, gas_used=receipt.gasUsed)
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

    # (timestamp_seconds, gas_used) entries, oldest first.
    events: Deque = field(default_factory=deque)
    sum_hour: int = 0
    sum_day: int = 0
    paused: bool = False


class GasBudgetTracker:
    """Tracks cumulative gas per wallet and enforces rolling-window limits.

    Parameters
    ----------
    default_limits:
        Applied to any wallet that does not have explicit limits set via
        :meth:`set_limits`.
    clock:
        Injectable seconds-resolution clock. Defaults to :func:`time.time`.
        Tests should pass a controllable clock to avoid real sleeps.
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

    def limits_for(self, wallet: str) -> GasLimits:
        return self._limits.get(wallet, self._default_limits)

    # ---- enforcement ---------------------------------------------------

    def can_spend(self, wallet: str, estimated_gas: int) -> bool:
        """Return ``True`` if ``estimated_gas`` fits within every active window."""
        if estimated_gas < 0:
            raise ValueError("estimated_gas must be non-negative")

        with self._lock:
            ledger = self._ledgers[wallet]
            self._evict_locked(ledger)
            limits = self.limits_for(wallet)

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
            self._evict_locked(ledger)

            now = self._clock()
            ledger.events.append((now, gas_used))
            ledger.sum_hour += gas_used
            ledger.sum_day += gas_used

            limits = self.limits_for(wallet)
            if (
                limits.per_hour is not None and ledger.sum_hour >= limits.per_hour
            ) or (
                limits.per_day is not None and ledger.sum_day >= limits.per_day
            ):
                ledger.paused = True

            return self._status_locked(wallet, ledger, limits)

    # ---- introspection -------------------------------------------------

    def status(self, wallet: str) -> BudgetStatus:
        with self._lock:
            ledger = self._ledgers[wallet]
            self._evict_locked(ledger)
            return self._status_locked(wallet, ledger, self.limits_for(wallet))

    def resume(self, wallet: str) -> None:
        """Manually unpause a wallet. The operator is responsible for ensuring
        the underlying budget has freed up — this does not reset counters."""
        with self._lock:
            self._ledgers[wallet].paused = False

    def reset(self, wallet: str) -> None:
        """Clear all recorded spend for ``wallet`` (e.g. after a new funding round)."""
        with self._lock:
            self._ledgers[wallet] = _WalletLedger()

    # ---- internals -----------------------------------------------------

    def _evict_locked(self, ledger: _WalletLedger) -> None:
        """Drop events that have aged out of both windows and refresh sums."""
        now = self._clock()
        day_cutoff = now - SECONDS_PER_DAY
        hour_cutoff = now - SECONDS_PER_HOUR

        # Evict from the daily window (which also removes from hourly).
        while ledger.events and ledger.events[0][0] <= day_cutoff:
            ts, gas = ledger.events.popleft()
            ledger.sum_day -= gas
            if ts > hour_cutoff:
                # Shouldn't happen — hour window is a subset of day — but keep
                # sums consistent defensively.
                ledger.sum_hour -= gas

        # Rebuild sum_hour from events (cheap: bounded by day window size).
        ledger.sum_hour = sum(gas for ts, gas in ledger.events if ts > hour_cutoff)

        # Auto-unpause if limits have freed up again.
        if ledger.paused:
            limits_ok_hour = True
            limits_ok_day = True
            # We don't know wallet limits here; caller re-checks before spending.
            # We keep paused sticky until explicit resume() or a fresh record()
            # re-evaluates. See docstring on resume().
            del limits_ok_hour, limits_ok_day

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
