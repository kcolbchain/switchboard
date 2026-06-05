import datetime
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Optional, Tuple

SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400

class BudgetExhausted(RuntimeError):
    pass

class GasLimitExceededError(BudgetExhausted):
    pass

@dataclass(frozen=True)
class GasLimits:
    per_hour: Optional[int] = None
    per_day: Optional[int] = None

@dataclass
class BudgetStatus:
    wallet: str
    limits: GasLimits
    spent_last_hour: int
    spent_last_day: int
    paused: bool

    @property
    def remaining_hour(self) -> Optional[int]:
        if not self.limits.per_hour: return None
        return max(0, self.limits.per_hour - self.spent_last_hour)

    @property
    def remaining_day(self) -> Optional[int]:
        if not self.limits.per_day: return None
        return max(0, self.limits.per_day - self.spent_last_day)

@dataclass
class _Ledger:
    events: Deque = field(default_factory=deque)
    sum_hour: int = 0
    sum_day: int = 0
    paused: bool = False
    last_reset_hour: float = 0.0
    last_reset_day: float = 0.0

class GasManager:
    """Unified gas manager.
    Supports rolling-window ("rolling") and calendar-bucket ("calendar") modes.
    Manages both global limits and per-wallet limits.
    """
    def __init__(
        self,
        mode: str = "rolling", # "rolling" or "calendar"
        global_limits: GasLimits = GasLimits(),
        default_wallet_limits: GasLimits = GasLimits(),
        clock: Callable[[], float] = time.time
    ):
        if not hasattr(self, '_initialized'):
            self._mode = mode
            self._global_limits = global_limits
            self._default_wallet_limits = default_wallet_limits
            self._clock = clock
            self._lock = threading.Lock()
            self._global_ledger = _Ledger()
            self._wallet_ledgers: Dict[str, _Ledger] = defaultdict(_Ledger)
            self._wallet_limits: Dict[str, GasLimits] = {}
            
            now = self._clock()
            self._global_ledger.last_reset_hour = now - (now % 3600)
            self._global_ledger.last_reset_day = self._align_day(now)
            self._initialized = True

    def _align_day(self, now: float) -> float:
        dt = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc)
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp()

    def set_global_limits(self, limits: GasLimits):
        with self._lock:
            self._global_limits = limits
            self._evict_locked(self._global_ledger, limits)

    def set_wallet_limits(self, wallet: str, limits: GasLimits):
        with self._lock:
            self._wallet_limits[wallet] = limits
            self._evict_locked(self._wallet_ledgers[wallet], limits)

    def wallet_limits_for(self, wallet: str) -> GasLimits:
        return self._wallet_limits.get(wallet, self._default_wallet_limits)

    def can_spend(self, wallet: Optional[str], estimated_gas: int) -> bool:
        if estimated_gas < 0: raise ValueError("negative gas")
        with self._lock:
            # Check global
            self._evict_locked(self._global_ledger, self._global_limits)
            if self._global_ledger.paused: return False
            if self._exceeds(self._global_ledger, self._global_limits, estimated_gas):
                return False
            
            # Check wallet
            if wallet:
                ledger = self._wallet_ledgers[wallet]
                limits = self.wallet_limits_for(wallet)
                self._evict_locked(ledger, limits)
                if ledger.paused: return False
                if self._exceeds(ledger, limits, estimated_gas):
                    return False
            return True

    def check(self, wallet: Optional[str], estimated_gas: int) -> None:
        if not self.can_spend(wallet, estimated_gas):
            # Backwards compat requires BudgetExhausted(status) for budget, and GasLimitExceededError() for tracker?
            raise BudgetExhausted(self.status(wallet))

    def _exceeds(self, ledger: _Ledger, limits: GasLimits, gas: int) -> bool:
        if limits.per_hour and ledger.sum_hour + gas > limits.per_hour:
            return True
        if limits.per_day and ledger.sum_day + gas > limits.per_day:
            return True
        return False

    def record(self, wallet: Optional[str], gas_used: int) -> BudgetStatus:
        if gas_used < 0: raise ValueError("negative gas")
        with self._lock:
            self._record_locked(self._global_ledger, self._global_limits, gas_used)
            if wallet:
                ledger = self._wallet_ledgers[wallet]
                limits = self.wallet_limits_for(wallet)
                self._record_locked(ledger, limits, gas_used)
                return self._status_locked(wallet, ledger, limits)
            return self._status_locked(None, self._global_ledger, self._global_limits)

    def _record_locked(self, ledger: _Ledger, limits: GasLimits, gas_used: int):
        self._evict_locked(ledger, limits)
        now = self._clock()
        if self._mode == "rolling":
            ledger.events.append((now, gas_used))
        ledger.sum_hour += gas_used
        ledger.sum_day += gas_used

        if limits.per_hour and ledger.sum_hour >= limits.per_hour:
            ledger.paused = True
        if limits.per_day and ledger.sum_day >= limits.per_day:
            ledger.paused = True

    def status(self, wallet: Optional[str]) -> BudgetStatus:
        with self._lock:
            if wallet:
                ledger = self._wallet_ledgers[wallet]
                limits = self.wallet_limits_for(wallet)
            else:
                ledger = self._global_ledger
                limits = self._global_limits
            self._evict_locked(ledger, limits)
            return self._status_locked(wallet or "GLOBAL", ledger, limits)

    def _status_locked(self, wallet: str, ledger: _Ledger, limits: GasLimits) -> BudgetStatus:
        return BudgetStatus(
            wallet=wallet,
            limits=limits,
            spent_last_hour=ledger.sum_hour,
            spent_last_day=ledger.sum_day,
            paused=ledger.paused
        )

    def resume(self, wallet: Optional[str]) -> None:
        with self._lock:
            if wallet:
                self._wallet_ledgers[wallet].paused = False
            else:
                self._global_ledger.paused = False

    def reset(self, wallet: Optional[str]) -> None:
        with self._lock:
            now = self._clock()
            if wallet:
                self._wallet_ledgers[wallet] = _Ledger(last_reset_hour=now - (now%3600), last_reset_day=self._align_day(now))
            else:
                self._global_ledger = _Ledger(last_reset_hour=now - (now%3600), last_reset_day=self._align_day(now))
                self._wallet_ledgers.clear()

    def _evict_locked(self, ledger: _Ledger, limits: GasLimits) -> None:
        now = self._clock()
        if self._mode == "rolling":
            day_cutoff = now - SECONDS_PER_DAY
            hour_cutoff = now - SECONDS_PER_HOUR

            while ledger.events and ledger.events[0][0] <= day_cutoff:
                ts, gas = ledger.events.popleft()
                ledger.sum_day -= gas
                if ts > hour_cutoff:
                    ledger.sum_hour -= gas

            ledger.sum_hour = sum(gas for ts, gas in ledger.events if ts > hour_cutoff)
        else:
            # Calendar mode
            # Hourly reset
            if not ledger.last_reset_hour:
                ledger.last_reset_hour = now - (now % 3600)
            if now - ledger.last_reset_hour >= 3600:
                ledger.sum_hour = 0
                ledger.last_reset_hour = now - (now % 3600)

            # Daily reset
            if not ledger.last_reset_day:
                ledger.last_reset_day = self._align_day(now)
            current_day_dt = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc).date()
            last_day_dt = datetime.datetime.fromtimestamp(ledger.last_reset_day, tz=datetime.timezone.utc).date()
            if current_day_dt > last_day_dt:
                ledger.sum_day = 0
                ledger.last_reset_day = self._align_day(now)

        # Re-evaluate paused
        if ledger.paused:
            ok_hr = (not limits.per_hour) or (ledger.sum_hour < limits.per_hour)
            ok_day = (not limits.per_day) or (ledger.sum_day < limits.per_day)
            if self._mode == "calendar":
                if ok_hr and ok_day: ledger.paused = False
