import pytest
from web3 import Web3
from eth_tester import EthereumTester
import json
import time

from switchboard.gas_budget import GasBudgetTracker, GasLimits, BudgetExhausted

@pytest.fixture
def tester():
    return EthereumTester()

@pytest.fixture
def w3(tester):
    return Web3(Web3.EthereumTesterProvider(tester))

@pytest.fixture
def account(w3):
    return w3.eth.accounts[0]

@pytest.fixture
def contract_address(w3, account):
    with open("tests/AgentBudget_compiled.json") as f:
        compiled = json.load(f)
    
    AgentBudget = w3.eth.contract(abi=compiled["abi"], bytecode=compiled["bytecode"])
    tx_hash = AgentBudget.constructor().transact({'from': account})
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    
    # Authorize account as updater
    contract = w3.eth.contract(address=receipt.contractAddress, abi=compiled["abi"])
    contract.functions.setUpdater(account, True).transact({'from': account})
    
    return receipt.contractAddress

WALLET = "0x" + "1" * 40

def test_default_limits_allow_everything(w3, contract_address, account):
    t = GasBudgetTracker(w3=w3, contract_address=contract_address, account=account)
    # Check that can_spend returns True when there's no limit
    assert t.can_spend(WALLET, 10**12) is True
    t.record(WALLET, 10**12)
    status = t.status(WALLET)
    assert status.paused is False
    assert status.remaining_hour is None

def test_hourly_limit_blocks_overspend(w3, contract_address, account):
    t = GasBudgetTracker(
        w3=w3,
        contract_address=contract_address,
        account=account,
        default_limits=GasLimits(per_hour=100_000)
    )

    assert t.can_spend(WALLET, 60_000)
    t.record(WALLET, 60_000)

    assert t.can_spend(WALLET, 30_000) is True
    assert t.can_spend(WALLET, 50_000) is False

def test_hourly_window_rolls_forward(w3, contract_address, account, tester):
    t = GasBudgetTracker(
        w3=w3,
        contract_address=contract_address,
        account=account,
        default_limits=GasLimits(per_hour=100_000)
    )

    t.record(WALLET, 90_000)
    assert t.can_spend(WALLET, 20_000) is False

    # Slide past the hour boundary.
    tester.time_travel(int(time.time()) + 3601)

    assert t.can_spend(WALLET, 90_000) is True
    
def test_multi_process_race(w3, contract_address, account):
    # Two trackers simulate two processes
    t1 = GasBudgetTracker(w3=w3, contract_address=contract_address, account=account, default_limits=GasLimits(per_hour=100_000))
    t2 = GasBudgetTracker(w3=w3, contract_address=contract_address, account=account, default_limits=GasLimits(per_hour=100_000))
    
    assert t1.can_spend(WALLET, 60_000)
    assert t2.can_spend(WALLET, 60_000)
    
    # Process 1 spends 60k
    t1.record(WALLET, 60_000)
    
    # Process 2 should now NOT be able to spend 60k because the on-chain state reflects 60k spent
    assert t2.can_spend(WALLET, 60_000) is False

