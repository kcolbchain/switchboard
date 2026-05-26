"""Lucidly syUSD auto-park for idle agent balances.

Parks idle USDC balances into Lucidly's syUSD vault to earn yield
between agent actions. Auto-rebalance hook runs after every agent
settlement. Unpark-on-demand when agent needs liquidity.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


class LucidlyAdapterError(Exception):
    """Base error for Lucidly adapter operations."""


@dataclass
class LucidlyConfig:
    """Per-wallet configuration for auto-park."""
    idle_target_bps: int = 8000
    max_parked_usd: float = 100_000.0
    per_chain_targets: dict[str, int] = field(default_factory=lambda: {
        "ethereum": 5000,
        "base": 8000,
        "arbitrum": 8000,
    })
    enabled: bool = True

    @property
    def idle_target_pct(self) -> float:
        return self.idle_target_bps / 10_000.0


@dataclass
class ParkedPosition:
    """A single parked position in the syUSD vault."""
    chain: str
    amount_usd: float
    syUSD_shares: float
    parked_at: float
    yield_accrued_usd: float = 0.0

    @property
    def total_value_usd(self) -> float:
        return self.amount_usd + self.yield_accrued_usd


class MockLucidlyVault:
    """Mock ILucidlyVault interface for testing (no on-chain calls)."""

    def __init__(self):
        self._positions: dict[str, float] = {}
        self._yield_rate_apy: float = 0.05

    def deposit(self, chain: str, amount_usd: float) -> float:
        shares = amount_usd * (1 + self._yield_rate_apy / 36500)
        self._positions[chain] = self._positions.get(chain, 0) + shares
        return shares

    def withdraw(self, chain: str, amount_usd: float) -> float:
        current = self._positions.get(chain, 0)
        if current < amount_usd:
            amount_usd = current
        self._positions[chain] = current - amount_usd
        return amount_usd

    def balance(self, chain: str) -> float:
        return self._positions.get(chain, 0)

    def simulate_yield(self, days: int):
        for chain in self._positions:
            self._positions[chain] *= (1 + self._yield_rate_apy * days / 365)


class LucidlyAutoPark:
    """Auto-parks idle agent balances into Lucidly syUSD vault.

    Rebalance hook: runs after every agent settlement. Checks liquid
    buffer vs target and moves excess into vault.

    Unpark-on-demand: when agent's next tx would exceed liquid buffer,
    pulls from vault before broadcasting.
    """

    def __init__(
        self,
        vault: MockLucidlyVault | None = None,
        config: LucidlyConfig | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.vault = vault or MockLucidlyVault()
        self.config = config or LucidlyConfig()
        self.clock = clock

        self._lock = threading.Lock()
        self._positions: dict[str, ParkedPosition] = {}
        self._liquid_buffer: dict[str, float] = {}
        self._total_parked: float = 0.0
        self._total_yield: float = 0.0

    def rebalance(self, chain: str, liquid_balance_usd: float) -> dict[str, Any]:
        """After settlement, move excess liquid above target into vault.

        Returns a dict with the rebalance action taken.
        """
        if not self.config.enabled:
            return {"action": "disabled", "chain": chain}

        target_pct = self.config.per_chain_targets.get(chain, self.config.idle_target_pct) / 10_000
        target_liquid = liquid_balance_usd * (1 - target_pct)

        with self._lock:
            current_liquid = self._liquid_buffer.get(chain, liquid_balance_usd)
            excess = max(0.0, current_liquid - target_liquid)

            if excess < 1.0:
                return {"action": "skip", "chain": chain, "excess": excess}

            if self._total_parked + excess > self.config.max_parked_usd:
                excess = self.config.max_parked_usd - self._total_parked
                if excess < 1.0:
                    return {"action": "cap_reached", "chain": chain}

            shares = self.vault.deposit(chain, excess)
            position = ParkedPosition(
                chain=chain,
                amount_usd=excess,
                syUSD_shares=shares,
                parked_at=self.clock(),
            )
            self._positions[f"{chain}:{self.clock()}"] = position
            self._liquid_buffer[chain] = current_liquid - excess
            self._total_parked += excess

            return {
                "action": "parked",
                "chain": chain,
                "amount_usd": excess,
                "shares": shares,
                "total_parked": self._total_parked,
            }

    def unpark(self, chain: str, amount_usd: float) -> float:
        """Withdraw from vault before broadcasting a tx.

        Returns the amount actually withdrawn.
        """
        if not self.config.enabled or amount_usd <= 0:
            return 0.0

        with self._lock:
            available = self.vault.balance(chain)
            to_withdraw = min(amount_usd, available, self._total_parked)
            if to_withdraw < 1.0:
                return 0.0

            withdrawn = self.vault.withdraw(chain, to_withdraw)
            self._liquid_buffer[chain] = self._liquid_buffer.get(chain, 0) + withdrawn
            self._total_parked -= withdrawn

            return withdrawn

    def ensure_liquid(self, chain: str, required_usd: float, liquid_balance_usd: float) -> float:
        """Ensure enough liquid balance for a tx. Unpark if needed."""
        if liquid_balance_usd >= required_usd:
            return 0.0
        deficit = required_usd - liquid_balance_usd
        returned = self.unpark(chain, deficit)
        return returned

    def yield_report(self, chain: str = "") -> dict[str, Any]:
        """Report yield accrued per wallet/chain."""
        if chain:
            total = sum(
                p.yield_accrued_usd
                for k, p in self._positions.items()
                if k.startswith(f"{chain}:")
            )
            return {"chain": chain, "total_yield_usd": total}

        by_chain: dict[str, float] = {}
        for k, p in self._positions.items():
            c = k.split(":")[0]
            by_chain[c] = by_chain.get(c, 0) + p.yield_accrued_usd

        return {
            "total_yield_usd": sum(by_chain.values()),
            "by_chain": by_chain,
            "total_parked_usd": self._total_parked,
            "positions": len(self._positions),
        }

    def status(self, chain: str) -> dict[str, Any]:
        """Return current status for a chain."""
        with self._lock:
            return {
                "chain": chain,
                "liquid_buffer_usd": self._liquid_buffer.get(chain, 0.0),
                "vault_balance_usd": self.vault.balance(chain),
                "total_parked_usd": self._total_parked,
                "enabled": self.config.enabled,
                "idle_target_pct": self.config.per_chain_targets.get(chain, self.config.idle_target_bps) / 100.0,
            }
