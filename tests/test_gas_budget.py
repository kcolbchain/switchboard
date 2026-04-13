"""Tests for switchboard.gas_budget — see issue #5."""

from __future__ import annotations

import threading

import pytest

from switchboard.gas_budget import (
    BudgetExhausted,
    GasBudgetTracker,
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


# ---------------------------------------------------------------- basics


def test_default_limits_allow_everything():
    t = GasBudgetTracker()
    assert t.can_spend(WALLET, 10**12) is True
    t.record(WALLET, 10**12)
    status = t.status(WALLET)
    assert status.paused is False
    assert status.remaining_hour is None
    assert status.remaining_day is None


def test_record_rejects_negative():
    t = GasBudgetTracker()
    with pytest.raises(ValueError):
        t.record(WALLET, -1)
    with pytest.raises(ValueError):
        t.can_spend(WALLET, -1)


# ---------------------------------------------------------------- hour


def test_hourly_limit_blocks_overspend():
    clock = FakeClock()
    t = GasBudgetTracker(
        default_limits=GasLimits(per_hour=100_000),
        clock=clock,
    )

    assert t.can_spend(WALLET, 60_000)
    t.record(WALLET, 60_000)

    # 60k of 100k used — 50k more would exceed.
    assert t.can_spend(WALLET, 30_000) is True
    assert t.can_spend(WALLET, 50_000) is False


def test_hourly_window_rolls_forward():
    clock = FakeClock()
    t = GasBudgetTracker(
        default_limits=GasLimits(per_hour=100_000),
        clock=clock,
    )

    t.record(WALLET, 90_000)
    assert t.can_spend(WALLET, 20_000) is False

    # Slide past the hour boundary.
    clock.advance(SECONDS_PER_HOUR + 1)

    # Spend should now fit — but wallet remains paused until operator resumes
    # (prior spend exhausted the limit, pausing it). Resume and retry.
    t.resume(WALLET)
    assert t.can_spend(WALLET, 90_000) is True


# ---------------------------------------------------------------- day


def test_daily_limit_independent_of_hourly():
    clock = FakeClock()
    t = GasBudgetTracker(
        default_limits=GasLimits(per_hour=50_000, per_day=120_000),
        clock=clock,
    )

    for _ in range(3):
        assert t.can_spend(WALLET, 40_000)
        t.record(WALLET, 40_000)
        clock.advance(SECONDS_PER_HOUR + 1)
        t.resume(WALLET)  # re-enable after each hourly pause

    # Day total now 120k == limit exactly; anything more should fail.
    assert t.can_spend(WALLET, 1) is False


def test_daily_window_rolls_forward():
    clock = FakeClock()
    t = GasBudgetTracker(
        default_limits=GasLimits(per_day=100_000),
        clock=clock,
    )

    t.record(WALLET, 100_000)
    assert t.status(WALLET).paused is True

    clock.advance(SECONDS_PER_DAY + 1)
    t.resume(WALLET)
    assert t.can_spend(WALLET, 100_000) is True
    assert t.status(WALLET).spent_last_day == 0


# ---------------------------------------------------------------- pause


def test_pause_on_exhaustion_blocks_further_spending():
    t = GasBudgetTracker(default_limits=GasLimits(per_hour=1_000))
    t.record(WALLET, 1_000)
    status = t.status(WALLET)
    assert status.paused is True
    assert t.can_spend(WALLET, 1) is False


def test_check_raises_when_exhausted():
    t = GasBudgetTracker(default_limits=GasLimits(per_hour=500))
    t.record(WALLET, 500)
    with pytest.raises(BudgetExhausted) as exc:
        t.check(WALLET, 1)
    assert exc.value.args[0].wallet == WALLET


def test_resume_clears_pause_without_resetting_counters():
    clock = FakeClock()
    t = GasBudgetTracker(default_limits=GasLimits(per_hour=1_000), clock=clock)
    t.record(WALLET, 1_000)
    assert t.status(WALLET).paused is True

    t.resume(WALLET)
    status = t.status(WALLET)
    assert status.paused is False
    assert status.spent_last_hour == 1_000  # counters intact


# ---------------------------------------------------------------- per-wallet


def test_per_wallet_limits_override_default():
    t = GasBudgetTracker(default_limits=GasLimits(per_hour=1_000))
    t.set_limits("0xVIP", GasLimits(per_hour=10_000))

    t.record("0xVIP", 5_000)
    t.record(WALLET, 900)
    assert t.status("0xVIP").paused is False
    assert t.can_spend(WALLET, 500) is False  # default limit binds


def test_wallets_are_isolated():
    t = GasBudgetTracker(default_limits=GasLimits(per_hour=1_000))
    t.record("a", 1_000)
    assert t.status("a").paused is True
    assert t.status("b").paused is False
    assert t.can_spend("b", 999) is True


# ---------------------------------------------------------------- reset


def test_reset_clears_history():
    t = GasBudgetTracker(default_limits=GasLimits(per_hour=100))
    t.record(WALLET, 100)
    t.reset(WALLET)
    s = t.status(WALLET)
    assert s.spent_last_hour == 0
    assert s.paused is False
    assert t.can_spend(WALLET, 100) is True


# ---------------------------------------------------------------- threads


def test_thread_safety_sum_is_exact():
    t = GasBudgetTracker()  # no limits; just exercise locking
    N = 500
    workers = 8

    def run():
        for _ in range(N):
            t.record(WALLET, 1)

    threads = [threading.Thread(target=run) for _ in range(workers)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert t.status(WALLET).spent_last_hour == N * workers


# ---------------------------------------------------------------- status


def test_status_reports_remaining():
    t = GasBudgetTracker(
        default_limits=GasLimits(per_hour=10_000, per_day=100_000),
    )
    t.record(WALLET, 3_000)
    s = t.status(WALLET)
    assert s.remaining_hour == 7_000
    assert s.remaining_day == 97_000
