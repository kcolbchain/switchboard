"""
Unified gas budget manager — reconciles ``gas_budget.py`` and ``gas_tracker.py``.

Combines per-wallet accounting (GasBudgetTracker) with auto-unpause on window
rollover (GasTracker), providing a single drop-in replacement for both.

Design
------
- Per-wallet rolling-window enforcement (deque-based, not calendar buckets).
- Optional per-agent *epoch* cap (issue #78) keyed off an injectable epoch
  provider (block number / finalized timestamp), NEVER raw wall-clock.
- Thread-safe via single reentrant lock.
- Auto-unpauses a wallet when the rolling window frees up capacity, or when
  the epoch advances (see "Epoch auto-unpause semantics" below).
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

Epoch budgets (issue #78)
-------------------------
The per-agent epoch cap is a chain-aligned budget surfaced to the block
builder. It is enforced only when BOTH a ``per_epoch`` limit is configured AND
an ``epoch_provider`` is injected; otherwise it is completely inert (the
default), so existing callers see zero behavioural change.

    epoch_provider = lambda: w3.eth.block_number  # or a finalized timestamp
    manager = GasManager(
        default_limits=GasLimits(per_epoch=5_000_000),
        epoch_provider=epoch_provider,
    )

    # Non-blocking, read-only runtime hint for the block builder:
    hint = manager.get_hint("0xAgent")
    if not hint.will_fit(estimated_gas):
        defer_to_next_epoch(tx)   # hint, not consensus

The epoch source MUST be reorg-safe (block number or finalized timestamp), not
``time.time()``. A wall-clock epoch can roll backward under a reorg and
prematurely reset spend — see the "Premature epoch reset by time or reorg
manipulation" threat in docs/security/gas-budget-threat-model.md.

Epoch auto-unpause semantics
----------------------------
Spend is tracked cumulatively per epoch and auto-resets to zero the first time
the provider reports a *different* epoch id (any change, including a multi-epoch
jump after downtime). On that reset a wallet paused *solely* by its epoch cap is
auto-unpaused — this matches the existing hour/day auto-unpause-on-rollover
contract. A wallet that is also over its hour or day cap stays paused until
those rolling windows free up; the epoch reset never overrides a still-binding
hour/day constraint. This mirrors the on-chain ``resetEpoch()`` rule "reset may
open a new epoch but must not raise spend capacity beyond the published caps".

References
----------
- Issue #97: https://github.com/kcolbchain/switchboard/issues/97
- Issue #78: https://github.com/kcolbchain/switchboard/issues/78
- docs/security/gas-budget-threat-model.md
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
    """Per-wallet gas ceilings. ``None`` disables the corresponding window.

    ``per_epoch`` is the issue #78 per-agent epoch cap. It is enforced only
    when the manager is constructed with an ``epoch_provider`` (see
    :class:`GasManager`). Left at its ``None`` default it is inert, so adding
    the field is an opt-in, zero-breaking-change extension.
    """

    per_hour: Optional[int] = None
    per_day: Optional[int] = None
    per_epoch: Optional[int] = None


@dataclass
class BudgetStatus:
    """Snapshot of a wallet's current spend vs. its limits."""

    wallet: str
    limits: GasLimits
    spent_last_hour: int
    spent_last_day: int
    paused: bool
    spent_this_epoch: int = 0
    epoch_id: Optional[int] = None

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

    @property
    def remaining_epoch(self) -> Optional[int]:
        if self.limits.per_epoch is None or self.epoch_id is None:
            return None
        return max(0, self.limits.per_epoch - self.spent_this_epoch)


@dataclass(frozen=True)
class EpochHint:
    """Non-blocking, read-only runtime hint for the block builder (issue #78).

    Returned by :meth:`GasManager.get_hint`. Querying a hint never mutates
    manager state. ``epoch_id`` / ``remaining`` are ``None`` when epoch
    enforcement is not active (no provider, or ``per_epoch`` unset).
    """

    wallet: str
    epoch_id: Optional[int]
    spent_this_epoch: int
    remaining: Optional[int]

    def will_fit(self, estimated_gas: int) -> bool:
        """Return whether ``estimated_gas`` fits within the remaining epoch cap.

        Unbounded (``remaining is None``) always fits. Read-only — no mutation.
        """
        if estimated_gas < 0:
            raise ValueError("estimated_gas must be non-negative")
        if self.remaining is None:
            return True
        return estimated_gas <= self.remaining


@dataclass
class _WalletLedger:
    """Internal per-wallet state. Protected by the tracker lock."""

    events: Deque = field(default_factory=deque)
    sum_hour: int = 0
    sum_day: int = 0
    paused: bool = False
    # Issue #78 epoch accounting. ``epoch_id`` is None until the first spend
    # under an active epoch provider; ``spent_this_epoch`` resets on transition.
    spent_this_epoch: int = 0
    epoch_id: Optional[int] = None


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
    epoch_provider:
        Optional ``Callable[[], int]`` returning the current epoch id (block
        number or finalized timestamp). When ``None`` (the default) the
        ``per_epoch`` cap is never enforced, preserving legacy behaviour. MUST
        be reorg-safe — do NOT pass raw wall-clock (``time.time``); see the
        module docstring and the threat model.
    """

    def __init__(
        self,
        default_limits: GasLimits = GasLimits(),
        clock: Callable[[], float] = time.time,
        epoch_provider: Optional[Callable[[], int]] = None,
    ):
        self._default_limits = default_limits
        self._clock = clock
        self._epoch_provider = epoch_provider
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
            self._roll_epoch_locked(ledger, limits)
            self._evict_locked(ledger, limits)

            if ledger.paused:
                return False
            if limits.per_hour is not None and ledger.sum_hour + estimated_gas > limits.per_hour:
                return False
            if limits.per_day is not None and ledger.sum_day + estimated_gas > limits.per_day:
                return False
            if (
                self._epoch_enabled(limits)
                and ledger.spent_this_epoch + estimated_gas > limits.per_epoch
            ):
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
            self._roll_epoch_locked(ledger, limits)
            self._evict_locked(ledger, limits)

            now = self._clock()
            ledger.events.append((now, gas_used))
            ledger.sum_hour += gas_used
            ledger.sum_day += gas_used
            # Track epoch spend whenever a provider exists (even with no cap)
            # so get_hint() can report spend-for-visibility; the cap itself is
            # enforced separately via _epoch_enabled().
            if self._epoch_provider is not None:
                ledger.spent_this_epoch += gas_used
            crossed = (
                (limits.per_hour is not None and ledger.sum_hour >= limits.per_hour)
                or (limits.per_day is not None and ledger.sum_day >= limits.per_day)
                or (
                    self._epoch_enabled(limits)
                    and ledger.spent_this_epoch >= limits.per_epoch
                )
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
            self._roll_epoch_locked(ledger, limits)
            self._evict_locked(ledger, limits)
            return self._status_locked(wallet, ledger, limits)

    def get_hint(self, wallet: str) -> EpochHint:
        """Return the non-blocking epoch runtime hint for ``wallet`` (issue #78).

        Read-only: it rolls the epoch and evicts aged events to report a current
        view, but never records spend or changes the pause flag. Intended for a
        block builder to consult per mempool tx tagged with this agent. When
        epoch enforcement is inactive, ``epoch_id`` and ``remaining`` are
        ``None`` and :meth:`EpochHint.will_fit` returns ``True`` for any amount.
        """
        with self._lock:
            ledger = self._ledgers[wallet]
            limits = self.limits_for(wallet)
            self._roll_epoch_locked(ledger, limits)
            remaining: Optional[int]
            if self._epoch_enabled(limits):
                remaining = max(0, limits.per_epoch - ledger.spent_this_epoch)
            else:
                remaining = None
            return EpochHint(
                wallet=wallet,
                epoch_id=ledger.epoch_id,
                spent_this_epoch=ledger.spent_this_epoch,
                remaining=remaining,
            )

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

    def _epoch_enabled(self, limits: GasLimits) -> bool:
        """Epoch enforcement is active only with BOTH a provider and a cap."""
        return self._epoch_provider is not None and limits.per_epoch is not None

    def _roll_epoch_locked(self, ledger: _WalletLedger, limits: GasLimits) -> None:
        """Detect an epoch transition and reset cumulative epoch spend.

        Reads the injected epoch provider (block number / finalized timestamp).
        On ANY change of epoch id — including a multi-epoch jump after downtime
        — ``spent_this_epoch`` resets to zero and the new id is stored. A wallet
        paused *solely* by its epoch cap is auto-unpaused on reset; a wallet
        still over its hour/day cap stays paused (re-evaluated in
        :meth:`_evict_locked`). No-op when no provider is injected, so legacy
        callers never touch epoch state.
        """
        if self._epoch_provider is None:
            return
        current = self._epoch_provider()
        if ledger.epoch_id == current:
            return

        was_epoch_only_pause = (
            ledger.paused
            and self._epoch_enabled(limits)
            and ledger.spent_this_epoch >= limits.per_epoch
            and not (
                (limits.per_hour is not None and ledger.sum_hour >= limits.per_hour)
                or (limits.per_day is not None and ledger.sum_day >= limits.per_day)
            )
        )
        ledger.epoch_id = current
        ledger.spent_this_epoch = 0
        if was_epoch_only_pause:
            ledger.paused = False

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
                or (
                    self._epoch_enabled(limits)
                    and ledger.spent_this_epoch >= limits.per_epoch
                )
            )
            if not still_exhausted:
                ledger.paused = False

    def _update_pause_state_locked(
        self, ledger: _WalletLedger, limits: GasLimits
    ) -> None:
        """Re-evaluate the pause flag after a limit change, in BOTH directions.

        Mirrors ``GasTracker._update_pause_state``: tightening a wallet's limit
        below its current spend must *pause* it, and loosening the limit above
        the current spend must *unpause* it. The previous implementation only
        cleared an existing pause and never set one, so lowering a limit below
        the recorded spend left ``is_paused()`` reporting ``False`` while
        ``can_spend()`` (correctly) returned ``False`` — an inconsistency that
        breaks the documented drop-in parity with ``GasTracker``.
        """
        self._roll_epoch_locked(ledger, limits)
        self._evict_locked(ledger, limits)
        ledger.paused = (
            (limits.per_hour is not None and ledger.sum_hour >= limits.per_hour)
            or (limits.per_day is not None and ledger.sum_day >= limits.per_day)
            or (
                self._epoch_enabled(limits)
                and ledger.spent_this_epoch >= limits.per_epoch
            )
        )

    def _status_locked(
        self, wallet: str, ledger: _WalletLedger, limits: GasLimits
    ) -> BudgetStatus:
        return BudgetStatus(
            wallet=wallet,
            limits=limits,
            spent_last_hour=ledger.sum_hour,
            spent_last_day=ledger.sum_day,
            paused=ledger.paused,
            spent_this_epoch=ledger.spent_this_epoch,
            epoch_id=ledger.epoch_id,
        )
