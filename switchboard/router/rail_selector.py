"""Unit ⑪ — RailSelector.

Picks the cheapest suitable settlement rail for a given payment amount.

Rails (cheapest → most capable):
  x402    — HTTP micro-payment; lowest cost, instant; capped at ``x402_max_amount``.
  escrow  — On-chain trustless escrow; for mid-range amounts.
  mpp     — Multi-party payment; for large / multi-party flows.

The caller can force a specific rail via ``force_rail``; the RailSelector
trusts the caller in that case (policy enforcement is upstream).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


_RAILS = ("x402", "escrow", "mpp")

# Sensible production defaults.
_DEFAULT_X402_MAX   = 100_000         # ~0.1 USDC (6 dec) or ~$0.01 worth of ETH
_DEFAULT_ESCROW_MAX = 10_000_000_000  # ~$10 000 USDC (6 dec)


@dataclass
class RailConfig:
    """Threshold configuration for RailSelector.

    Parameters
    ----------
    x402_max_amount:
        Inclusive upper bound (in smallest token units) for the x402 rail.
    escrow_max_amount:
        Inclusive upper bound for the escrow rail.  Amounts above this use mpp.
    """

    x402_max_amount: int = _DEFAULT_X402_MAX
    escrow_max_amount: int = _DEFAULT_ESCROW_MAX


class RailSelector:
    """Selects the cheapest suitable rail for a payment amount.

    Parameters
    ----------
    config:
        Threshold configuration.  Defaults to ``RailConfig()`` if omitted.
    """

    def __init__(self, config: Optional[RailConfig] = None) -> None:
        self._config = config if config is not None else RailConfig()

    def select(self, amount: int, force_rail: Optional[str] = None) -> str:
        """Return the rail name for ``amount``.

        Parameters
        ----------
        amount:
            Payment amount in the token's smallest unit.
        force_rail:
            If provided, skip threshold logic and return this rail directly.
            One of ``"x402"``, ``"escrow"``, ``"mpp"``.
        """
        if force_rail is not None:
            return force_rail

        cfg = self._config
        if amount <= cfg.x402_max_amount:
            return "x402"
        if amount <= cfg.escrow_max_amount:
            return "escrow"
        return "mpp"
