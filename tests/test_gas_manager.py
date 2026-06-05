import pytest
import time
from switchboard.gas_manager import GasManager, GasLimits, BudgetStatus

def test_unified_gas_manager_global():
    mgr = GasManager(mode="rolling", global_limits=GasLimits(per_hour=100))
    assert mgr.can_spend(None, 50)
    mgr.record(None, 50)
    assert mgr.can_spend(None, 60) == False
    assert mgr.can_spend("wallet_a", 60) == False # Global limits apply to wallets too if we check wallet

def test_unified_gas_manager_wallet():
    mgr = GasManager(mode="rolling", default_wallet_limits=GasLimits(per_hour=100))
    assert mgr.can_spend("wallet_a", 50)
    mgr.record("wallet_a", 50)
    assert mgr.can_spend("wallet_a", 60) == False
    assert mgr.can_spend("wallet_b", 60) == True
