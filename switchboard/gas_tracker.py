import time
import threading
from typing import Optional, Callable
import datetime

class GasBudgetExhaustedError(Exception):
    """
    Custom exception raised for informational purposes when gas budget is exhausted.
    The `GasTracker` itself will pause subsequent `can_send_transaction` calls,
    but it's up to the caller to handle this immediate exhaustion event.
    """
    pass

class GasTracker:
    """
    Tracks cumulative gas spent and enforces configurable hourly and daily limits.
    If a limit is exceeded, the tracker's `is_paused()` method will return True,
    and `can_send_transaction()` will return False until the budget resets.
    This class is implemented as a singleton to ensure a single, consistent
    gas budget is managed across the application.
    """
    _instance: Optional['GasTracker'] = None
    _lock = threading.Lock() # For singleton instantiation

    def __new__(cls, *args, **kwargs):
        """
        Ensures that only one instance of GasTracker is created (singleton pattern).
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, hourly_limit: int = 0, daily_limit: int = 0, time_source: Callable[[], float] = time.time):
        """
        Initializes the GasTracker.
        
        Args:
            hourly_limit (int): Maximum gas allowed per hour. 0 means no hourly limit.
            daily_limit (int): Maximum gas allowed per day. 0 means no daily limit.
            time_source (Callable[[], float]): A function that returns the current time
                                              as a float timestamp. Defaults to `time.time`.
        """
        if not hasattr(self, '_initialized'):
            self._hourly_limit = hourly_limit
            self._daily_limit = daily_limit
            self._spent_gas_hourly = 0
            self._spent_gas_daily = 0
            self._time_source = time_source # Callable to get current timestamp
            self._last_reset_hour = self._time_source()
            self._last_reset_day = self._time_source()
            self._is_paused = False # True if any limit is currently exceeded
            self._tracker_lock = threading.Lock() # For internal state changes
            self._initialized = True
            
            self._align_last_reset_day() # Ensure daily timestamp is at start of day
            self._update_pause_state() # Check initial limits based on current spent (if any)

    def _align_last_reset_day(self):
        """
        Aligns `_last_reset_day` to the start of the current UTC day.
        This ensures daily limits reset consistently at midnight UTC.
        """
        current_datetime_utc = datetime.datetime.fromtimestamp(self._time_source(), tz=datetime.timezone.utc)
        start_of_day_utc = current_datetime_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        self._last_reset_day = start_of_day_utc.timestamp()

    def _update_pause_state(self):
        """
        Internal method to update the `_is_paused` flag based on current spending and limits.
        Sets `_is_paused` to True if any limit is currently exceeded.
        """
        can_proceed_hourly = (self._hourly_limit == 0) or (self._spent_gas_hourly < self._hourly_limit)
        can_proceed_daily = (self._daily_limit == 0) or (self._spent_gas_daily < self._daily_limit)
        self._is_paused = not (can_proceed_hourly and can_proceed_daily)

    def _reset_if_needed(self):
        """
        Checks if an hour or day has passed since the last reset and resets counters.
        Also updates the pause state. This method should be called before any
        interaction with the tracker's state (e.g., recording gas, checking limits).
        """
        now = self._time_source()
        
        # Hourly reset
        # Aligns _last_reset_hour to the start of the current full hour.
        if now - self._last_reset_hour >= 3600: # 1 hour
            self._spent_gas_hourly = 0
            self._last_reset_hour = now - (now % 3600) # Align to start of current hour

        # Daily reset
        current_day_dt = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc).date()
        last_reset_day_dt = datetime.datetime.fromtimestamp(self._last_reset_day, tz=datetime.timezone.utc).date()
        
        if current_day_dt > last_reset_day_dt:
            self._spent_gas_daily = 0
            self._align_last_reset_day()

        self._update_pause_state()

    def record_gas_usage(self, gas_used: int):
        """
        Records actual gas used for a confirmed transaction.
        Updates internal spending totals and the pause state.
        This method should be called after a transaction has successfully
        completed and its actual gas usage is known.
        
        Args:
            gas_used (int): The amount of gas used by the transaction.
        """
        with self._tracker_lock:
            self._reset_if_needed() # Always check for resets before recording

            self._spent_gas_hourly += gas_used
            self._spent_gas_daily += gas_used
            
            self._update_pause_state() # Recalculate pause state after adding gas

    def can_send_transaction(self, estimated_gas_cost: int) -> bool:
        """
        Checks if a transaction with the given estimated gas cost can be sent
        without exceeding current limits. This method should be called
        before attempting to send a transaction.
        
        Args:
            estimated_gas_cost (int): The estimated gas cost for the transaction.
            
        Returns:
            bool: True if the transaction can be sent, False otherwise.
        """
        with self._tracker_lock:
            self._reset_if_needed() # Always check for resets before deciding

            if self._is_paused:
                return False

            if self._hourly_limit > 0 and (self._spent_gas_hourly + estimated_gas_cost) > self._hourly_limit:
                return False
            
            if self._daily_limit > 0 and (self._spent_gas_daily + estimated_gas_cost) > self._daily_limit:
                return False
            
            return True

    def is_paused(self) -> bool:
        """
        Returns True if the tracker is currently paused due to budget exhaustion.
        This flag is updated automatically on resets and when gas is recorded.
        
        Returns:
            bool: True if paused, False otherwise.
        """
        with self._tracker_lock:
            self._reset_if_needed() # Ensure current state is up-to-date
            return self._is_paused
            
    def set_limits(self, hourly_limit: int = 0, daily_limit: int = 0):
        """
        Sets new hourly and daily gas limits.
        Updates the pause state based on new limits and current spending.
        
        Args:
            hourly_limit (int): The new hourly gas limit.
            daily_limit (int): The new daily gas limit.
        """
        with self._tracker_lock:
            self._hourly_limit = hourly_limit
            self._daily_limit = daily_limit
            self._reset_if_needed() # Apply potential resets based on time
            self._update_pause_state() # Update pause state considering new limits

    def get_current_spent(self) -> tuple[int, int]:
        """
        Returns current (hourly_spent, daily_spent) gas totals.
        
        Returns:
            tuple[int, int]: A tuple containing current hourly spent gas and daily spent gas.
        """
        with self._tracker_lock:
            self._reset_if_needed()
            return self._spent_gas_hourly, self._spent_gas_daily

    def reset_all(self):
        """
        Resets all internal counters to zero, unpauses the tracker,
        and sets reset timestamps to the current time.
        Useful for testing or complete reconfiguration.
        """
        with self._tracker_lock:
            self._spent_gas_hourly = 0
            self._spent_gas_daily = 0
            self._last_reset_hour = self._time_source()
            self._align_last_reset_day()
            self._is_paused = False # Explicitly unpause
            self._update_pause_state() # Re-evaluate pause state (should be unpaused)

