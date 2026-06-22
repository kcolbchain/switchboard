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
    unpark_threshold_bps: int = 1500
    max_entry_slippage_bps: int = 25
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
        preview = self.preview_deposit(chain, amount_usd)
        shares = preview["shares"]
        self._positions[chain] = self._positions.get(chain, 0) + shares
        return shares

    def preview_deposit(self, chain: str, amount_usd: float) -> dict[str, float]:
        shares = amount_usd * (1 + self._yield_rate_apy / 36500)
        return {"shares": shares, "slippage_bps": 0.0}

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
        self._wallet_total_usd: dict[str, float] = {}
        self._total_parked: float = 0.0
        self._total_yield: float = 0.0
        # Monotonic suffix so two parks on the same chain at the same clock
        # value get distinct keys instead of silently overwriting each other.
        self._position_seq: int = 0

    def _target_bps(self, chain: str) -> int:
        return self.config.per_chain_targets.get(chain, self.config.idle_target_bps)

    def rebalance(self, chain: str, liquid_balance_usd: float) -> dict[str, Any]:
        """After settlement, move excess liquid above target into vault.

        Returns a dict with the rebalance action taken.
        """
        if not self.config.enabled:
            return {"action": "disabled", "chain": chain}

        wallet_total = liquid_balance_usd + self.vault.balance(chain)
        self._wallet_total_usd[chain] = wallet_total
        target_pct = self._target_bps(chain) / 10_000
        target_liquid = wallet_total * (1 - target_pct)

        with self._lock:
            current_liquid = liquid_balance_usd
            excess = max(0.0, current_liquid - target_liquid)

            if excess < 1.0:
                self._liquid_buffer[chain] = current_liquid
                return {"action": "skip", "chain": chain, "excess": excess}

            if self._total_parked + excess > self.config.max_parked_usd:
                excess = self.config.max_parked_usd - self._total_parked
                if excess < 1.0:
                    self._liquid_buffer[chain] = current_liquid
                    return {"action": "cap_reached", "chain": chain}

            preview = self.vault.preview_deposit(chain, excess)
            slippage_bps = preview.get("slippage_bps", 0.0)
            if slippage_bps > self.config.max_entry_slippage_bps:
                self._liquid_buffer[chain] = current_liquid
                return {
                    "action": "skip_slippage",
                    "chain": chain,
                    "slippage_bps": slippage_bps,
                    "max_entry_slippage_bps": self.config.max_entry_slippage_bps,
                }

            shares = self.vault.deposit(chain, excess)
            position = ParkedPosition(
                chain=chain,
                amount_usd=excess,
                syUSD_shares=shares,
                parked_at=self.clock(),
            )
            self._position_seq += 1
            self._positions[f"{chain}:{self.clock()}:{self._position_seq}"] = position
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
        self._liquid_buffer[chain] = liquid_balance_usd
        threshold_usd = self._wallet_total_usd.get(chain, liquid_balance_usd)
        threshold_usd *= self.config.unpark_threshold_bps / 10_000
        target_liquid_usd = max(required_usd, threshold_usd)
        if liquid_balance_usd >= target_liquid_usd:
            return 0.0
        deficit = target_liquid_usd - liquid_balance_usd
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

    def weekly_yield_report(self, window_days: int = 30) -> dict[str, Any]:
        """Per-wallet realized-APY card for the weekly disclosure cron (issue #80 AC #4).

        Emits a JSON-serializable blob with a ``realized_30d_apy`` figure
        derived from accrued yield over the parked principal, annualized across
        ``window_days``. This is the public-surface disclosure that closes the
        loop on the Lucidly co-marketing. The shape is intentionally flat and
        free of non-JSON types so a cron can write it straight to a static
        surface.
        """
        base = self.yield_report()
        total_yield = base["total_yield_usd"]
        total_parked = base["total_parked_usd"]

        # realized APY = (yield / principal) annualized over the window.
        if total_parked > 0 and window_days > 0:
            realized_apy = (total_yield / total_parked) * (365.0 / window_days)
        else:
            realized_apy = 0.0

        per_chain_parked: dict[str, float] = {}
        for k, p in self._positions.items():
            c = k.split(":")[0]
            per_chain_parked[c] = per_chain_parked.get(c, 0.0) + p.amount_usd

        by_chain: dict[str, dict[str, float]] = {}
        for chain, parked in per_chain_parked.items():
            chain_yield = base["by_chain"].get(chain, 0.0)
            apy = (
                (chain_yield / parked) * (365.0 / window_days)
                if parked > 0 and window_days > 0
                else 0.0
            )
            by_chain[chain] = {
                "parked_usd": parked,
                "yield_usd": chain_yield,
                "realized_30d_apy": apy,
            }

        return {
            "window_days": window_days,
            "realized_30d_apy": realized_apy,
            "total_parked_usd": total_parked,
            "total_yield_usd": total_yield,
            "positions": base["positions"],
            "by_chain": by_chain,
            "generated_at": self.clock(),
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
                "idle_target_pct": self._target_bps(chain) / 10_000.0,
            }
