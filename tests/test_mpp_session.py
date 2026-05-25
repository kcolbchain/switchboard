"""Tests for MPP sessions adapter."""

import pytest
from switchboard.mpp.session import MPPSession, MPPSessionError, SessionState


def test_session_open_close():
    s = MPPSession()
    session_id = s.open(limit_usd=10.0)
    assert s.state.status == "open"
    assert s.state.session_id == session_id
    result = s.close()
    assert result["total_spent_usd"] == 0.0
    assert s.state.status == "closed"


def test_charge_within_limit():
    s = MPPSession()
    s.open(limit_usd=10.0)
    charge = s.charge(2.50, "inference call")
    assert charge.amount_usd == 2.50
    assert s.state.spent_usd == 2.50
    assert s.state.remaining_usd == 7.50


def test_charge_exceeds_limit():
    s = MPPSession()
    s.open(limit_usd=5.0)
    with pytest.raises(MPPSessionError):
        s.charge(10.0)


def test_charge_on_closed_session():
    s = MPPSession()
    with pytest.raises(MPPSessionError):
        s.charge(1.0)


def test_double_open():
    s = MPPSession()
    s.open(limit_usd=10.0)
    with pytest.raises(MPPSessionError):
        s.open(limit_usd=20.0)


def test_double_close():
    s = MPPSession()
    s.open(limit_usd=10.0)
    s.close()
    with pytest.raises(MPPSessionError):
        s.close()


def test_status_report():
    s = MPPSession()
    s.open(limit_usd=10.0)
    s.charge(3.0)
    status = s.status()
    assert status["status"] == "open"
    assert status["spent_usd"] == 3.0
    assert status["remaining_usd"] == 7.0


def test_multiple_charges():
    s = MPPSession()
    s.open(limit_usd=10.0)
    amounts = [1.0, 2.0, 3.0]
    for a in amounts:
        s.charge(a, f"charge-{a}")
    assert s.state.spent_usd == 6.0
    assert len(s.charges()) == 3


def test_session_pauses_on_budget_exhausted():
    tracker = MockBudgetTracker()
    s = MPPSession(budget_tracker=tracker, wallet="0xwallet")
    s.open(limit_usd=10.0)
    s.charge(5.0)
    assert s.state.status == "open"
    tracker.pause()
    s.charge(1.0)
    assert s.state.status == "paused"


def test_close_reconciles_with_budget():
    tracker = MockBudgetTracker()
    s = MPPSession(budget_tracker=tracker, wallet="0xwallet")
    s.open(limit_usd=10.0)
    s.charge(5.0)
    result = s.close()
    assert result["total_spent_usd"] == 5.0
    assert tracker.recorded_gas > 0


class MockBudgetTracker:
    def __init__(self):
        self._paused = False
        self.recorded_gas = 0

    def can_spend(self, wallet, gas):
        return not self._paused

    def record(self, wallet, gas):
        self.recorded_gas = gas

    def pause(self):
        self._paused = True
