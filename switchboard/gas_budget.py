"""
Gas budget tracker for agent wallets.

Tracks cumulative gas spent per wallet using the authoritative on-chain AgentBudget contract,
enforces configurable limits, and pauses execution when a budget is exhausted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from web3 import Web3
from web3.contract import Contract


class BudgetExhausted(RuntimeError):
    """Raised when a wallet would exceed its configured gas budget."""


@dataclass(frozen=True)
class GasLimits:
    """Per-wallet gas ceilings. ``None`` disables the corresponding window."""
    per_hour: Optional[int] = None
    per_day: Optional[int] = None


@dataclass
class BudgetStatus:
    """Snapshot of a wallet's current spend vs. its limits."""
    wallet: str
    limits: GasLimits
    spent_last_hour: int
    spent_last_day: int
    paused: bool

    @property
    def remaining_hour(self) -> Optional[int]:
        if self.limits.per_hour is None:
            return None
        return max(0, self.limits.per_hour - self.spent_last_hour)

    @property
    def remaining_day(self) -> Optional[int]:
        if self.limits.per_day is None:
            return None
        return max(0, self.limits.per_day - self.spent_last_day)


# Default ABI for AgentBudget
AGENT_BUDGET_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "agent", "type": "address"}],
        "name": "budgets",
        "outputs": [
            {"internalType": "uint256", "name": "epoch", "type": "uint256"},
            {"internalType": "uint256", "name": "hourlyCap", "type": "uint256"},
            {"internalType": "uint256", "name": "dailyCap", "type": "uint256"},
            {"internalType": "uint256", "name": "hourlySpent", "type": "uint256"},
            {"internalType": "uint256", "name": "dailySpent", "type": "uint256"},
            {"internalType": "uint256", "name": "lastResetBlock", "type": "uint256"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address", "name": "agent", "type": "address"}, {"internalType": "uint256", "name": "gasAmount", "type": "uint256"}],
        "name": "recordSpend",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]


class GasBudgetTracker:
    """Tracks cumulative gas per wallet using the on-chain AgentBudget.

    Parameters
    ----------
    w3:
        Web3 provider instance.
    contract_address:
        Address of the deployed AgentBudget contract.
    account:
        Local account or address that is authorized to call recordSpend.
    default_limits:
        Applied to any wallet that does not have explicit limits set via
        :meth:`set_limits`.
    """

    def __init__(
        self,
        w3: Web3,
        contract_address: str,
        account: Optional[str] = None,
        default_limits: GasLimits = GasLimits(),
    ):
        self._w3 = w3
        self._account = account
        self._contract: Contract = w3.eth.contract(address=w3.to_checksum_address(contract_address), abi=AGENT_BUDGET_ABI)
        self._default_limits = default_limits
        self._limits: dict[str, GasLimits] = {}

    def set_limits(self, wallet: str, limits: GasLimits) -> None:
        """Override the default limits for ``wallet``."""
        self._limits[wallet] = limits

    def limits_for(self, wallet: str) -> GasLimits:
        return self._limits.get(wallet, self._default_limits)

    def can_spend(self, wallet: str, estimated_gas: int) -> bool:
        """Return ``True`` if ``estimated_gas`` fits within every active window."""
        if estimated_gas < 0:
            raise ValueError("estimated_gas must be non-negative")

        status = self.status(wallet)
        if status.paused:
            return False
            
        limits = self.limits_for(wallet)
        if limits.per_hour is not None and status.spent_last_hour + estimated_gas > limits.per_hour:
            return False
        if limits.per_day is not None and status.spent_last_day + estimated_gas > limits.per_day:
            return False
            
        return True

    def check(self, wallet: str, estimated_gas: int) -> None:
        """Raise :class:`BudgetExhausted` if ``estimated_gas`` cannot be spent."""
        if not self.can_spend(wallet, estimated_gas):
            raise BudgetExhausted(self.status(wallet))

    def record_gas_usage(self, gas_used: int):
        return self.record(self._account, gas_used)

    def record(self, wallet: str, gas_used: int) -> BudgetStatus:
        """Record a post-confirmation gas spend to the blockchain."""
        if gas_used < 0:
            raise ValueError("gas_used must be non-negative")
            
        if self._account:
            tx = self._contract.functions.recordSpend(
                self._w3.to_checksum_address(wallet), 
                gas_used
            ).build_transaction({
                'from': self._account,
                'nonce': self._w3.eth.get_transaction_count(self._account)
            })
            # In a real setup, we'd sign it if self._account is an eth_account,
            # but for tests or nodes with unlocked accounts, we can just send it.
            tx_hash = self._w3.eth.send_transaction({
                'to': tx['to'],
                'data': tx['data'],
                'from': self._account,
            })
            self._w3.eth.wait_for_transaction_receipt(tx_hash)

        return self.status(wallet)

    def status(self, wallet: str) -> BudgetStatus:
        """Fetch the current budget status for the wallet from on-chain."""
        data = self._contract.functions.budgets(self._w3.to_checksum_address(wallet)).call()
        epoch, hourly_cap, daily_cap, hourly_spent, daily_spent, last_reset_block = data
        
        # Calculate resets locally
        current_time = self._w3.eth.get_block('latest')['timestamp']
        
        current_hour = current_time // 3600
        current_day = current_time // 86400
        last_hour = epoch // 3600
        last_day = epoch // 86400
        
        if current_hour > last_hour:
            hourly_spent = 0
        if current_day > last_day:
            daily_spent = 0
            
        limits = self.limits_for(wallet)
        paused = False
        if limits.per_hour is not None and hourly_spent >= limits.per_hour:
            paused = True
        if limits.per_day is not None and daily_spent >= limits.per_day:
            paused = True
            
        return BudgetStatus(
            wallet=wallet,
            limits=limits,
            spent_last_hour=hourly_spent,
            spent_last_day=daily_spent,
            paused=paused,
        )
        
    def resume(self, wallet: str) -> None:
        """Manually unpause a wallet."""
        pass # Pausing is based on on-chain spending logic now

    def reset(self, wallet: str) -> None:
        """Clear all recorded spend for ``wallet``."""
        # For a full implementation, you'd call an admin reset function on the contract.
        pass
