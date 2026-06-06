"""Tests for the unified GasManager class."""

from __future__ import annotations

import threading

import pytest

from switchboard.gas_manager import (
    BudgetExhausted,
    GasLimits,
    GasManager,
    GasStatus,
    GLOBAL_WALLET,
    WindowMode,
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


# ---------------------------------------------------------------- rolling mode


class TestRollingMode:

    def test_basic_rolling_workflow(self):
        clock = FakeClock()
        mgr = GasManager(hourly_limit=100_000, window_mode="rolling", clock=clock)

        assert mgr.can_spend(WALLET, 60_000)
        status = mgr.record(WALLET, 60_000)
        assert status.spent_hourly == 60_000
        assert status.paused is False

    def test_rolling_window_expires(self):
        clock = FakeClock()
        mgr = GasManager(hourly_limit=100_000, window_mode="rolling", clock=clock)

        mgr.record(WALLET, 90_000)
        assert mgr.can_spend(WALLET, 20_000) is False

        clock.advance(SECONDS_PER_HOUR + 1)
        # Rolling window expired: 90k aged out, so 20k now fits
        assert mgr.can_spend(WALLET, 20_000) is True

    def test_rolling_pause_stays_until_resume(self):
        """Rolling mode: pause is sticky until explicit resume()."""
        clock = FakeClock()
        mgr = GasManager(hourly_limit=100_000, window_mode="rolling", clock=clock)

        # Record enough to hit the limit exactly
        mgr.record(WALLET, 100_000)
        assert mgr.status(WALLET).paused is True
        assert mgr.can_spend(WALLET, 1) is False

        # Even after time passes, pause stays (rolling mode is sticky)
        clock.advance(SECONDS_PER_HOUR + 1)
        assert mgr.can_spend(WALLET, 1) is False

        # Only explicit resume clears it
        mgr.resume(WALLET)
        assert mgr.can_spend(WALLET, 100_000) is True

    def test_rolling_multi_wallet(self):
        mgr = GasManager(hourly_limit=10_000, window_mode="rolling")

        mgr.record("wallet_a", 8_000)
        mgr.record("wallet_b", 5_000)

        assert mgr.can_spend("wallet_a", 3_000) is False
        assert mgr.can_spend("wallet_b", 5_000) is True


# ---------------------------------------------------------------- calendar mode


class TestCalendarMode:

    def test_basic_calendar_workflow(self):
        clock = FakeClock()
        mgr = GasManager(hourly_limit=1_000, daily_limit=5_000, window_mode="calendar", clock=clock)

        assert mgr.can_spend(GLOBAL_WALLET, 500)
        mgr.record(GLOBAL_WALLET, 500)
        assert mgr.get_current_spent() == (500, 500)

    def test_calendar_hourly_reset(self):
        clock = FakeClock()
        mgr = GasManager(hourly_limit=500, daily_limit=5_000, window_mode="calendar", clock=clock)

        mgr.record(GLOBAL_WALLET, 500)
        assert mgr.is_paused() is True

        clock.advance(SECONDS_PER_HOUR + 1)
        # Hour reset should auto-unpause
        assert mgr.can_send_transaction(100) is True
        assert mgr.get_current_spent() == (0, 500)

    def test_calendar_daily_reset(self):
        clock = FakeClock()
        mgr = GasManager(hourly_limit=10_000, daily_limit=1_000, window_mode="calendar", clock=clock)

        mgr.record(GLOBAL_WALLET, 1_000)
        assert mgr.is_paused() is True

        clock.advance(SECONDS_PER_DAY + 1)
        assert mgr.can_send_transaction(100) is True
        assert mgr.get_current_spent() == (0, 0)


# ---------------------------------------------------------------- unified features


class TestUnifiedFeatures:

    def test_rolling_mode_has_global_convenience(self):
        """Rolling mode should also support global convenience methods."""
        mgr = GasManager(hourly_limit=10_000, window_mode="rolling")

        mgr.record_gas_usage(5_000)
        assert mgr.get_current_spent() == (5_000, 5_000)
        assert mgr.is_paused() is False

    def test_calendar_mode_has_per_wallet(self):
        """Calendar mode should support per-wallet tracking."""
        clock = FakeClock()
        mgr = GasManager(hourly_limit=1_000, window_mode="calendar", clock=clock)

        mgr.set_limits("vip", hourly=5_000)
        mgr.record("vip", 3_000)
        mgr.record("regular", 900)

        assert mgr.status("vip").paused is False
        assert mgr.status("regular").paused is False
        assert mgr.can_spend("regular", 200) is False

    def test_pause_resume(self):
        mgr = GasManager(window_mode="rolling")

        mgr.pause(WALLET)
        assert mgr.can_spend(WALLET, 1) is False

        mgr.resume(WALLET)
        assert mgr.can_spend(WALLET, 1) is True

    def test_reset_clears_state(self):
        mgr = GasManager(hourly_limit=100, window_mode="rolling")

        mgr.record(WALLET, 100)
        assert mgr.status(WALLET).paused is True

        mgr.reset(WALLET)
        assert mgr.status(WALLET).paused is False
        assert mgr.status(WALLET).spent_hourly == 0

    def test_check_raises_with_status(self):
        mgr = GasManager(hourly_limit=100, window_mode="rolling")

        mgr.record(WALLET, 100)
        with pytest.raises(BudgetExhausted) as exc:
            mgr.check(WALLET, 1)
        assert exc.value.status.wallet == WALLET
        assert exc.value.args[0].wallet == WALLET

    def test_thread_safety(self):
        mgr = GasManager(window_mode="rolling")
        N = 500
        workers = 8

        def run():
            for _ in range(N):
                mgr.record(WALLET, 1)

        threads = [threading.Thread(target=run) for _ in range(workers)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert mgr.status(WALLET).spent_hourly == N * workers

    def test_negative_values_rejected(self):
        mgr = GasManager(window_mode="rolling")
        with pytest.raises(ValueError):
            mgr.record(WALLET, -1)
        with pytest.raises(ValueError):
            mgr.can_spend(WALLET, -1)

    def test_singleton_mode(self):
        GasManager.reset_singleton()
        m1 = GasManager(hourly_limit=100, singleton=True)
        m2 = GasManager(hourly_limit=200, singleton=True)
        assert m1 is m2
        GasManager.reset_singleton()
