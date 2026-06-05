from __future__ import annotations

import time
from typing import Callable, Optional
from switchboard.gas_manager import GasManager, GasLimits, BudgetStatus, BudgetExhausted, GasLimitExceededError

class GasBudgetTracker:
    def __init__(
        self,
        default_limits: GasLimits = GasLimits(),
        clock: Callable[[], float] = time.time,
    ):
        self._manager = GasManager(
            mode="rolling",
            default_wallet_limits=default_limits,
            clock=clock
        )

    def set_limits(self, wallet: str, limits: GasLimits) -> None:
        self._manager.set_wallet_limits(wallet, limits)

    def limits_for(self, wallet: str) -> GasLimits:
        return self._manager.wallet_limits_for(wallet)

    def can_spend(self, wallet: str, estimated_gas: int) -> bool:
        return self._manager.can_spend(wallet, estimated_gas)

    def check(self, wallet: str, estimated_gas: int) -> None:
        if not self.can_spend(wallet, estimated_gas):
            raise BudgetExhausted(self.status(wallet))

    def record(self, wallet: str, gas_used: int) -> BudgetStatus:
        return self._manager.record(wallet, gas_used)

    def status(self, wallet: str) -> BudgetStatus:
        return self._manager.status(wallet)

    def resume(self, wallet: str) -> None:
        self._manager.resume(wallet)

    def reset(self, wallet: str) -> None:
        self._manager.reset(wallet)
