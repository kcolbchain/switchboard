"""Tests for switchboard.gas_manager — issue #97 unified GasManager."""

from __future__ import annotations

import threading

import pytest

from switchboard.gas_manager import (
    BudgetExhausted,
    GasManager,
    GasLimits,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
)


class FakeClock:
    """Deterministic monotonically-controllable clock."""

    def __init__(self, start: float = 1_700_000_000.0):
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


WALLET = "0xAgent"

# ======================================================================
# GasBudgetTracker compatibility tests
# ======================================================================


class TestGasBudgetCompat:

    def test_default_limits_allow_everything(self):
        m = GasManager()
        assert m.can_spend(WALLET, 10**12) is True
        m.record(WALLET, 10**12)
        status = m.status(WALLET)
        assert status.paused is False
        assert status.remaining_hour is None
        assert status.remaining_day is None

    def test_record_rejects_negative(self):
        m = GasManager()
        with pytest.raises(ValueError):
            m.record(WALLET, -1)
        with pytest.raises(ValueError):
            m.can_spend(WALLET, -1)

    def test_hourly_limit_blocks_overspend(self):
        clock = FakeClock()
        m = GasManager(default_limits=GasLimits(per_hour=100_000), clock=clock)
        assert m.can_spend(WALLET, 60_000)
        m.record(WALLET, 60_000)
        assert m.can_spend(WALLET, 30_000) is True
        assert m.can_spend(WALLET, 50_000) is False

    def test_hourly_window_rolls_forward_and_auto_unpauses(self):
        clock = FakeClock()
        m = GasManager(default_limits=GasLimits(per_hour=100_000), clock=clock)

        m.record(WALLET, 90_000)
        assert m.can_spend(WALLET, 20_000) is False  # would exceed

        # Slide past the hour boundary — wallet should auto-unpause.
        clock.advance(SECONDS_PER_HOUR + 1)
        assert m.can_spend(WALLET, 90_000) is True
        # Verify it's no longer paused:
        assert m.status(WALLET).paused is False

    def test_daily_limit_independent_of_hourly(self):
        clock = FakeClock()
        m = GasManager(
            default_limits=GasLimits(per_hour=50_000, per_day=120_000),
            clock=clock,
        )

        for _ in range(3):
            assert m.can_spend(WALLET, 40_000)
            m.record(WALLET, 40_000)
            clock.advance(SECONDS_PER_HOUR + 1)
            # After window roll, hourly spend evicted — auto-unpaused.
            # However daily total accumulates.
            m.resume(WALLET)

        # Day total now 120k == limit exactly; anything more should fail.
        assert m.can_spend(WALLET, 1) is False

    def test_daily_window_rolls_forward_and_auto_unpauses(self):
        clock = FakeClock()
        m = GasManager(default_limits=GasLimits(per_day=100_000), clock=clock)

        m.record(WALLET, 100_000)
        assert m.status(WALLET).paused is True

        clock.advance(SECONDS_PER_DAY + 1)
        assert m.can_spend(WALLET, 100_000) is True
        assert m.status(WALLET).paused is False
        assert m.status(WALLET).spent_last_day == 0

    def test_pause_on_exhaustion_blocks_further_spending(self):
        m = GasManager(default_limits=GasLimits(per_hour=1_000))
        m.record(WALLET, 1_000)
        status = m.status(WALLET)
        assert status.paused is True
        assert m.can_spend(WALLET, 1) is False

    def test_check_raises_when_exhausted(self):
        m = GasManager(default_limits=GasLimits(per_hour=500))
        m.record(WALLET, 500)
        with pytest.raises(BudgetExhausted) as exc:
            m.check(WALLET, 1)
        assert exc.value.args[0].wallet == WALLET

    def test_resume_clears_pause_without_resetting_counters(self):
        clock = FakeClock()
        m = GasManager(default_limits=GasLimits(per_hour=1_000), clock=clock)
        m.record(WALLET, 1_000)
        assert m.status(WALLET).paused is True

        m.resume(WALLET)
        status = m.status(WALLET)
        assert status.paused is False
        assert status.spent_last_hour == 1_000  # counters intact

    def test_per_wallet_limits_override_default(self):
        m = GasManager(default_limits=GasLimits(per_hour=1_000))
        m.set_limits("0xVIP", GasLimits(per_hour=10_000))

        m.record("0xVIP", 5_000)
        m.record(WALLET, 900)
        assert m.status("0xVIP").paused is False
        assert m.can_spend(WALLET, 500) is False  # default limit binds

    def test_wallets_are_isolated(self):
        m = GasManager(default_limits=GasLimits(per_hour=1_000))
        m.record("a", 1_000)
        assert m.status("a").paused is True
        assert m.status("b").paused is False
        assert m.can_spend("b", 999) is True

    def test_reset_clears_history(self):
        m = GasManager(default_limits=GasLimits(per_hour=100))
        m.record(WALLET, 100)
        m.reset(WALLET)
        s = m.status(WALLET)
        assert s.spent_last_hour == 0
        assert s.paused is False
        assert m.can_spend(WALLET, 100) is True

    def test_thread_safety_sum_is_exact(self):
        m = GasManager()
        N = 500
        workers = 8

        def run():
            for _ in range(N):
                m.record(WALLET, 1)

        threads = [threading.Thread(target=run) for _ in range(workers)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert m.status(WALLET).spent_last_hour == N * workers

    def test_status_reports_remaining(self):
        m = GasManager(default_limits=GasLimits(per_hour=10_000, per_day=100_000))
        m.record(WALLET, 3_000)
        s = m.status(WALLET)
        assert s.remaining_hour == 7_000
        assert s.remaining_day == 97_000


# ======================================================================
# GasTracker compatibility tests
# ======================================================================


class TestGasTrackerCompat:

    def test_initialization(self):
        m = GasManager()
        assert m.get_current_spent("test") == (0, 0)
        assert m.is_paused("test") is False

    def test_set_limits(self):
        m = GasManager()
        m.set_limits("test", GasLimits(per_hour=1000, per_day=5000))
        assert m.limits_for("test") == GasLimits(per_hour=1000, per_day=5000)
        assert m.get_current_spent("test") == (0, 0)
        assert m.is_paused("test") is False

    def test_can_send_transaction_within_limits(self):
        m = GasManager(default_limits=GasLimits(per_hour=1000, per_day=5000))

        m.record_gas_usage(WALLET, 100)
        assert m.get_current_spent(WALLET) == (100, 100)
        assert m.is_paused(WALLET) is False
        assert m.can_send_transaction(WALLET, 100) is True

        m.record_gas_usage(WALLET, 200)
        assert m.get_current_spent(WALLET) == (300, 300)
        assert m.is_paused(WALLET) is False
        assert m.can_send_transaction(WALLET, 700) is True

    def test_hourly_limit_exceeded(self):
        m = GasManager(default_limits=GasLimits(per_hour=500, per_day=5000))
        m.record_gas_usage(WALLET, 300)
        m.record_gas_usage(WALLET, 200)

        assert m.get_current_spent(WALLET) == (500, 500)
        assert m.is_paused(WALLET) is True
        assert m.can_send_transaction(WALLET, 1) is False

        # Recording more gas when paused still updates the counters
        m.record_gas_usage(WALLET, 50)
        assert m.get_current_spent(WALLET) == (550, 550)
        assert m.is_paused(WALLET) is True

    def test_daily_limit_exceeded(self):
        m = GasManager(default_limits=GasLimits(per_hour=10000, per_day=1000))
        m.record_gas_usage(WALLET, 500)
        m.record_gas_usage(WALLET, 500)

        assert m.get_current_spent(WALLET) == (1000, 1000)
        assert m.is_paused(WALLET) is True
        assert m.can_send_transaction(WALLET, 1) is False

        m.record_gas_usage(WALLET, 100)
        assert m.get_current_spent(WALLET) == (1100, 1100)
        assert m.is_paused(WALLET) is True

    def test_hourly_auto_reset(self):
        clock = FakeClock()
        m = GasManager(default_limits=GasLimits(per_hour=500, per_day=5000), clock=clock)

        m.record_gas_usage(WALLET, 400)
        assert m.get_current_spent(WALLET) == (400, 400)
        assert m.is_paused(WALLET) is False
        assert m.can_send_transaction(WALLET, 100) is True

        # Advance past the hour boundary so the 400 is evicted from hour window
        clock.advance(SECONDS_PER_HOUR + 1)
        assert m.get_current_spent(WALLET) == (0, 400)
        assert m.is_paused(WALLET) is False

        # Use full budget in the new hour
        m.record_gas_usage(WALLET, 490)
        assert m.get_current_spent(WALLET) == (490, 890)
        assert m.is_paused(WALLET) is False  # 490 < 500

        m.record_gas_usage(WALLET, 10)
        assert m.get_current_spent(WALLET) == (500, 900)
        assert m.is_paused(WALLET) is True

    def test_daily_auto_reset(self):
        clock = FakeClock()
        m = GasManager(default_limits=GasLimits(per_hour=1000, per_day=1000), clock=clock)

        m.record_gas_usage(WALLET, 700)
        assert m.get_current_spent(WALLET) == (700, 700)
        assert m.is_paused(WALLET) is False

        # Advance past the day boundary — the 700 is evicted from both windows
        clock.advance(SECONDS_PER_DAY + 1)
        assert m.get_current_spent(WALLET) == (0, 0)
        assert m.is_paused(WALLET) is False

        # Use budget in new day
        m.record_gas_usage(WALLET, 300)
        assert m.get_current_spent(WALLET) == (300, 300)

        m.record_gas_usage(WALLET, 700)
        assert m.get_current_spent(WALLET) == (1000, 1000)
        assert m.is_paused(WALLET) is True

    def test_auto_unpause_after_reset(self):
        clock = FakeClock()
        m = GasManager(default_limits=GasLimits(per_hour=100, per_day=200), clock=clock)

        m.record_gas_usage(WALLET, 100)
        assert m.is_paused(WALLET) is True
        assert m.can_send_transaction(WALLET, 1) is False

        # Advance past hour boundary
        clock.advance(SECONDS_PER_HOUR + 1)
        # Auto-unpaused
        assert m.can_send_transaction(WALLET, 10) is True
        assert m.is_paused(WALLET) is False
        assert m.get_current_spent(WALLET) == (0, 100)

        # Hit hourly limit again
        m.record_gas_usage(WALLET, 10)
        m.record_gas_usage(WALLET, 90)
        assert m.is_paused(WALLET) is True

        # Advance past day boundary — both reset
        clock.advance(SECONDS_PER_DAY + 1)
        assert m.can_send_transaction(WALLET, 10) is True
        assert m.is_paused(WALLET) is False
        assert m.get_current_spent(WALLET) == (0, 0)

    def test_limits_change_unpauses(self):
        m = GasManager(default_limits=GasLimits(per_hour=100, per_day=200))
        m.record_gas_usage(WALLET, 100)
        assert m.is_paused(WALLET) is True

        m.set_limits(WALLET, GasLimits(per_hour=200, per_day=300))
        assert m.is_paused(WALLET) is False
        assert m.get_current_spent(WALLET) == (100, 100)
        assert m.can_send_transaction(WALLET, 50) is True

    def test_no_limits_default(self):
        m = GasManager()
        m.record_gas_usage(WALLET, 1_000_000)
        assert m.get_current_spent(WALLET) == (1_000_000, 1_000_000)
        assert m.is_paused(WALLET) is False
        assert m.can_send_transaction(WALLET, 1_000_000_000) is True

    def test_reset_all(self):
        m = GasManager(default_limits=GasLimits(per_hour=100))
        m.record_gas_usage("a", 99)
        m.record_gas_usage("b", 99)
        assert m.is_paused("a") is False
        assert m.is_paused("b") is False

        m.record_gas_usage("a", 1)
        assert m.is_paused("a") is True

        m.reset_all()
        assert m.get_current_spent("a") == (0, 0)
        assert m.is_paused("a") is False
        assert m.get_current_spent("b") == (0, 0)

    def test_singleton_not_required(self):
        """GasManager is NOT a singleton — unlike GasTracker.
        This is by design: each component manages its own budget."""
        m1 = GasManager(default_limits=GasLimits(per_hour=100))
        m2 = GasManager(default_limits=GasLimits(per_hour=200))
        assert m1 is not m2
        m1.record_gas_usage(WALLET, 50)
        assert m2.get_current_spent(WALLET) == (0, 0)


# ======================================================================
# Unified GasManager-specific tests
# ======================================================================


class TestGasManager:

    def test_auto_unpause_characteristic(self):
        """Core characteristic of the unified manager: auto-unpause when
        rolling windows free up capacity (from GasTracker)."""
        clock = FakeClock()
        m = GasManager(default_limits=GasLimits(per_hour=100), clock=clock)

        m.record(WALLET, 100)
        status = m.status(WALLET)
        assert status.paused is True
        # Without explicit resume(), the wallet stays paused
        assert m.can_spend(WALLET, 1) is False

        # After window rollover, auto-unpauses
        clock.advance(SECONDS_PER_HOUR + 1)
        assert m.can_spend(WALLET, 50) is True
        assert m.status(WALLET).paused is False

    def test_both_api_styles_equivalent(self):
        """can_spend/check/record == can_send_transaction/record_gas_usage."""
        clock = FakeClock()
        m1 = GasManager(default_limits=GasLimits(per_hour=1000), clock=clock)
        m2 = GasManager(default_limits=GasLimits(per_hour=1000), clock=clock)

        # Both APIs should produce equivalent results
        assert m1.can_spend(WALLET, 500) == m2.can_send_transaction(WALLET, 500)
        m1.record(WALLET, 500)
        m2.record_gas_usage(WALLET, 500)
        assert m1.status(WALLET) == m2.status(WALLET)

    def test_pause_on_hit_release_on_set_limits(self):
        m = GasManager(default_limits=GasLimits(per_hour=100))
        m.record(WALLET, 100)
        assert m.is_paused(WALLET) is True

        # Raising the limit should unpause
        m.set_limits(WALLET, GasLimits(per_hour=200))
        assert m.is_paused(WALLET) is False

    def test_pause_stays_if_new_limits_still_exceeded(self):
        m = GasManager(default_limits=GasLimits(per_hour=100))
        m.record(WALLET, 150)
        assert m.is_paused(WALLET) is True

        # New limit still below current spend
        m.set_limits(WALLET, GasLimits(per_hour=120))
        assert m.is_paused(WALLET) is True

    def test_evict_respects_hourly_boundary(self):
        clock = FakeClock()
        m = GasManager(default_limits=GasLimits(per_hour=1000), clock=clock)

        # Old spend that falls outside the hour window should be evicted
        m.record(WALLET, 900)
        clock.advance(SECONDS_PER_HOUR + 1)
        m.record(WALLET, 100)

        s = m.status(WALLET)
        assert s.spent_last_hour == 100
        assert s.spent_last_day == 1000

    def test_check_raises_budget_exhausted_with_status(self):
        m = GasManager(default_limits=GasLimits(per_hour=100))
        m.record(WALLET, 100)
        with pytest.raises(BudgetExhausted) as exc:
            m.check(WALLET, 1)
        assert isinstance(exc.value.args[0], m.status(WALLET).__class__)
