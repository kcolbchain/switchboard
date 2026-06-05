from __future__ import annotations

from switchboard.gas_manager import GasLimits, GasManager, SECONDS_PER_DAY, SECONDS_PER_HOUR


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0):
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def test_rolling_per_wallet_isolated_budget():
    clock = FakeClock()
    gm = GasManager(default_limits=GasLimits(per_hour=100, per_day=300), clock=clock, mode="rolling", scope="per-wallet")

    assert gm.can_spend("a", 50)
    gm.record("a", 50)
    assert gm.can_spend("a", 50)
    assert not gm.can_spend("a", 51)
    assert gm.can_spend("b", 100)


def test_calendar_global_resets_hour_and_day():
    clock = FakeClock(start=0.0)
    gm = GasManager(default_limits=GasLimits(per_hour=100, per_day=200), clock=clock, mode="calendar", scope="global")

    gm.record(None, 100)
    assert not gm.can_spend(None, 1)

    clock.advance(SECONDS_PER_HOUR + 1)
    assert gm.can_spend(None, 100)
    gm.record(None, 50)
    assert gm.spent(None) == (50, 150)

    clock.advance(SECONDS_PER_DAY + 1)
    assert gm.can_spend(None, 100)
    assert gm.spent(None) == (0, 0)


def test_check_raises_budget_exhausted():
    gm = GasManager(default_limits=GasLimits(per_hour=10), mode="rolling", scope="per-wallet")
    gm.record("wallet", 10)
    try:
        gm.check("wallet", 1)
        raised = False
    except Exception as exc:
        raised = True
        assert exc.__class__.__name__ == "BudgetExhausted"
    assert raised
