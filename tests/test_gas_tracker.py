import pytest
import time
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

# Assume GasTracker is in switchboard/gas_tracker.py
from switchboard.gas_tracker import GasTracker, GasBudgetExhaustedError

# Mock time.time() globally for all tests in this file
@pytest.fixture(autouse=True)
def mock_time():
    """
    Fixture to mock time.time() for controlled time progression in tests.
    Starts at a fixed UTC time: 2023-10-26 10:30:00 UTC.
    """
    _current_time = datetime(2023, 10, 26, 10, 30, 0, tzinfo=timezone.utc).timestamp()

    def get_mock_time():
        return _current_time

    def advance_time(seconds):
        nonlocal _current_time
        _current_time += seconds

    with patch('time.time', side_effect=get_mock_time) as mock_time_obj:
        mock_time_obj.advance = advance_time
        yield mock_time_obj

@pytest.fixture
def gas_tracker(mock_time):
    """
    Fixture to get a fresh GasTracker instance for each test.
    Ensures the singleton is reset and reinitialized for isolated tests.
    """
    # Reset the singleton instance for each test
    GasTracker._instance = None
    # Re-initialize with mock time source
    tracker = GasTracker(time_source=mock_time)
    tracker.reset_all() # Ensure clean state
    yield tracker
    GasTracker._instance = None # Clean up after test for next test's fresh instance


class TestGasTracker:

    def test_initialization(self, gas_tracker):
        """Test initial state after creation."""
        assert gas_tracker.get_current_spent() == (0, 0)
        assert gas_tracker.is_paused() == False
        assert gas_tracker._hourly_limit == 0
        assert gas_tracker._daily_limit == 0

    def test_set_limits_and_reset(self, gas_tracker):
        """Test setting new limits and resetting the tracker."""
        gas_tracker.set_limits(hourly_limit=1000, daily_limit=5000)
        assert gas_tracker._hourly_limit == 1000
        assert gas_tracker._daily_limit == 5000
        assert gas_tracker.get_current_spent() == (0, 0)
        assert gas_tracker.is_paused() == False

        gas_tracker.reset_all()
        assert gas_tracker.get_current_spent() == (0, 0)
        assert gas_tracker.is_paused() == False

    def test_record_gas_within_limits(self, gas_tracker):
        """Test recording gas usage that stays within configured limits."""
        gas_tracker.set_limits(hourly_limit=1000, daily_limit=5000)
        
        gas_tracker.record_gas_usage(100)
        assert gas_tracker.get_current_spent() == (100, 100)
        assert gas_tracker.is_paused() == False
        assert gas_tracker.can_send_transaction(100) == True

        gas_tracker.record_gas_usage(200)
        assert gas_tracker.get_current_spent() == (300, 300)
        assert gas_tracker.is_paused() == False
        assert gas_tracker.can_send_transaction(700) == True # Exactly hits hourly limit

    def test_hourly_limit_exceeded(self, gas_tracker):
        """Test exceeding the hourly gas limit and checking pause state."""
        gas_tracker.set_limits(hourly_limit=500, daily_limit=5000)
        gas_tracker.record_gas_usage(300)
        gas_tracker.record_gas_usage(200) # Hits exactly 500

        assert gas_tracker.get_current_spent() == (500, 500)
        assert gas_tracker.is_paused() == True # Should be paused
        assert gas_tracker.can_send_transaction(1) == False # Cannot send even 1 gas

        # Try to record more gas when paused, should still update internal state
        # but the tracker remains paused.
        gas_tracker.record_gas_usage(50)
        assert gas_tracker.get_current_spent() == (550, 550) # Still adds gas
        assert gas_tracker.is_paused() == True # Still paused

    def test_daily_limit_exceeded(self, gas_tracker):
        """Test exceeding the daily gas limit and checking pause state."""
        gas_tracker.set_limits(hourly_limit=10000, daily_limit=1000) # High hourly, low daily
        gas_tracker.record_gas_usage(500)
        gas_tracker.record_gas_usage(500) # Hits exactly 1000 daily

        assert gas_tracker.get_current_spent() == (1000, 1000)
        assert gas_tracker.is_paused() == True
        assert gas_tracker.can_send_transaction(1) == False

        gas_tracker.record_gas_usage(100)
        assert gas_tracker.get_current_spent() == (1100, 1100)
        assert gas_tracker.is_paused() == True

    def test_hourly_reset(self, gas_tracker, mock_time):
        """Test that the hourly budget resets after an hour has passed."""
        gas_tracker.set_limits(hourly_limit=500, daily_limit=5000)
        gas_tracker.record_gas_usage(400)
        assert gas_tracker.get_current_spent() == (400, 400)
        assert gas_tracker.is_paused() == False
        assert gas_tracker.can_send_transaction(100) == True

        # Advance time just under an hour
        mock_time.advance(3599)
        gas_tracker.record_gas_usage(50) # Still within the same hour
        assert gas_tracker.get_current_spent() == (450, 450)
        assert gas_tracker.is_paused() == False

        # Advance time past an hour mark
        mock_time.advance(1) # Now 3600 seconds passed since init's last_reset_hour
        # Calling any method that checks state (like record_gas_usage) will trigger reset
        gas_tracker.record_gas_usage(10) 
        assert gas_tracker.get_current_spent() == (10, 460) # Hourly reset, daily continues
        assert gas_tracker.is_paused() == False

        # Exceed hourly limit in the new hour
        gas_tracker.record_gas_usage(490)
        assert gas_tracker.get_current_spent() == (500, 950)
        assert gas_tracker.is_paused() == True
        assert gas_tracker.can_send_transaction(1) == False

    def test_daily_reset(self, gas_tracker, mock_time):
        """Test that the daily budget resets after a new UTC day has started."""
        # Initial time is 2023-10-26 10:30:00 UTC
        
        gas_tracker.set_limits(hourly_limit=1000, daily_limit=1000)
        gas_tracker.record_gas_usage(700) # Day 1 (Oct 26), Hour 1 (10:30-11:30 UTC)
        assert gas_tracker.get_current_spent() == (700, 700)
        assert gas_tracker.is_paused() == False

        # Advance almost 24 hours, but not past midnight UTC
        # Current mock time: 2023-10-26 10:30:00
        # Target time: 2023-10-27 00:00:00 UTC (13h 30m from start)
        # We'll advance just before midnight (e.g., 23:59:00 on Oct 26)
        # The `_last_reset_day` is already aligned to 2023-10-26 00:00:00 UTC
        
        # Advance to 2023-10-26 23:59:00 UTC (13 hours 29 minutes from 10:30)
        mock_time.advance(13 * 3600 + 29 * 60) # Current time: 2023-10-26 23:59:00 UTC
        
        # Record gas, this should trigger an hourly reset, but not daily
        gas_tracker.record_gas_usage(100)
        # Spent 700 (first hour) + 100 (new hour) = 800 for daily
        # Spent 100 for current (new) hour
        assert gas_tracker.get_current_spent() == (100, 800) 
        assert gas_tracker.is_paused() == False

        # Advance to next day (1 minute more to cross midnight UTC)
        mock_time.advance(60) # Current time: 2023-10-27 00:00:00 UTC

        # This will trigger both hourly and daily reset
        gas_tracker.record_gas_usage(50)
        assert gas_tracker.get_current_spent() == (50, 50) # Both hourly and daily reset
        assert gas_tracker.is_paused() == False
        
        # Exceed daily limit in new day
        gas_tracker.record_gas_usage(950)
        assert gas_tracker.get_current_spent() == (1000, 1000)
        assert gas_tracker.is_paused() == True
        assert gas_tracker.can_send_transaction(1) == False

    def test_unpausing_after_reset(self, gas_tracker, mock_time):
        """Test that the tracker unpauses automatically after a budget reset."""
        gas_tracker.set_limits(hourly_limit=100, daily_limit=200)
        gas_tracker.record_gas_usage(100) # Hourly limit hit
        assert gas_tracker.is_paused() == True
        assert gas_tracker.can_send_transaction(1) == False

        # Advance time by more than an hour
        mock_time.advance(3601)
        # Checking can_send_transaction should trigger reset and unpause
        assert gas_tracker.can_send_transaction(10) == True # Now unpaused
        assert gas_tracker.is_paused() == False
        assert gas_tracker.get_current_spent() == (0, 100) # Only hourly reset, daily accumulated stays

        # Hit daily limit
        gas_tracker.record_gas_usage(10)
        gas_tracker.record_gas_usage(90) # Hourly limit hit again (100)
        assert gas_tracker.is_paused() == True
        assert gas_tracker.can_send_transaction(1) == False
        assert gas_tracker.get_current_spent() == (100, 200)

        # Advance to next day
        mock_time.advance(24 * 3600 + 1)
        # Checking can_send_transaction should trigger both resets and unpause
        assert gas_tracker.can_send_transaction(10) == True # Both limits reset, unpaused
        assert gas_tracker.is_paused() == False
        assert gas_tracker.get_current_spent() == (0, 0) # Both reset

    def test_unpausing_with_new_limits(self, gas_tracker):
        """Test that changing limits can unpause or re-pause the tracker."""
        gas_tracker.set_limits(hourly_limit=100, daily_limit=200)
        gas_tracker.record_gas_usage(100) # Hit hourly limit
        assert gas_tracker.is_paused() == True

        # Set higher limits, should unpause as current spent is now below new limits
        gas_tracker.set_limits(hourly_limit=200, daily_limit=300)
        assert gas_tracker.is_paused() == False
        assert gas_tracker.get_current_spent() == (100, 100) # Spent gas stays
        assert gas_tracker.can_send_transaction(50) == True

        # Set limits such that it's still paused
        gas_tracker.set_limits(hourly_limit=50, daily_limit=300) # Now current spent 100 > limit 50
        assert gas_tracker.is_paused() == True

    def test_singleton_behavior(self, mock_time):
        """Test that GasTracker correctly implements the singleton pattern."""
        # Reset the instance to ensure a clean start for singleton testing
        GasTracker._instance = None
        
        # First instance with some initial limits
        tracker1 = GasTracker(hourly_limit=100, daily_limit=500, time_source=mock_time)
        tracker1.record_gas_usage(50)
        assert tracker1.get_current_spent() == (50, 50)
        assert tracker1._hourly_limit == 100

        # Second instance creation attempt should return the same object
        # Arguments (limits) passed to __init__ will be ignored for subsequent calls
        # because the _initialized flag prevents re-initialization.
        tracker2 = GasTracker(hourly_limit=500, daily_limit=1000, time_source=mock_time)
        assert tracker1 is tracker2 # Verify same object
        assert tracker2.get_current_spent() == (50, 50) # Same state
        assert tracker2._hourly_limit == 100 # Limits from first init

        # Setting limits on tracker2 should affect tracker1
        tracker2.set_limits(hourly_limit=200, daily_limit=1000)
        assert tracker1._hourly_limit == 200
        assert tracker1._daily_limit == 1000

        # Reset for subsequent tests cleanup
        GasTracker._instance = None
    
    def test_no_limits_default(self, gas_tracker):
        """Test behavior when no limits are set (default 0)."""
        # Default 0 limits mean no limits
        gas_tracker.record_gas_usage(1000000)
        assert gas_tracker.get_current_spent() == (1000000, 1000000)
        assert gas_tracker.is_paused() == False
        assert gas_tracker.can_send_transaction(1000000000) == True # Can send arbitrarily large amounts

    def test_initial_pause_on_high_spent_if_limits_set(self, gas_tracker, mock_time):
        """
        Test that if current spent gas already exceeds newly set limits,
        the tracker starts in a paused state.
        """
        # Use a fresh, uninitialized instance to mimic first-time setup or after a full reset
        GasTracker._instance = None
        
        # Manually manipulate state before full __init__ process to simulate pre-existing high spend
        # Note: In a real scenario, `record_gas_usage` would build this state up.
        # This is for testing the `_update_pause_state` on init/set_limits with a given state.
        tracker = GasTracker(time_source=mock_time) # Initialize with no limits initially
        tracker.record_gas_usage(150) # Spent 150
        assert tracker.is_paused() == False # Not paused yet, as no limits

        # Now set limits where current spent exceeds the new limits
        tracker.set_limits(hourly_limit=100, daily_limit=1000)
        
        # After setting limits, _update_pause_state should be called internally
        # which will evaluate `150 > 100` for hourly limit.
        assert tracker.is_paused() == True
        assert tracker.can_send_transaction(10) == False

        # Set limits again, higher, which should unpause it
        tracker.set_limits(hourly_limit=200, daily_limit=1000)
        assert tracker.is_paused() == False
        assert tracker.can_send_transaction(10) == True

