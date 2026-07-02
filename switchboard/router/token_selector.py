"""Unit ⑩ — TokenSelector.

Picks the source token to spend for a given payment amount.

Selection criteria (ordered priority):
  1. Solvency — spendable(chain_id, token) >= amount.
  2. Lowest fee_bps.
  3. Lowest expected_slippage_bps (tie-break on fee).
  4. Lexicographic token address (deterministic final tie-break).

LUX, ZOO, and other partner tokens are first-class — no special-casing;
balance and fee/slippage drive the pick naturally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from switchboard.treasury import Treasury


@dataclass
class TokenCandidate:
    """A token the router may choose as the settlement currency.

    Parameters
    ----------
    token:
        EVM address of the token (``address(0)`` = native ETH).
    fee_bps:
        Expected swap/bridge fee in basis points. 0 = no fee.
    expected_slippage_bps:
        Expected slippage in basis points. 0 = no slippage.
    """

    token: str
    fee_bps: int = 0
    expected_slippage_bps: int = 0


class TokenSelector:
    """Selects the best source token for a payment.

    Parameters
    ----------
    treasury:
        The Treasury to query for spendable balances.
    chain_id:
        The chain on which the payment will be executed.
    """

    def __init__(self, treasury: Treasury, chain_id: int) -> None:
        self._treasury = treasury
        self._chain_id = chain_id

    def select(
        self,
        amount: int,
        candidates: List[TokenCandidate],
    ) -> Optional[TokenCandidate]:
        """Return the best token candidate, or ``None`` if none are solvent.

        Parameters
        ----------
        amount:
            Required amount in the token's smallest unit.
        candidates:
            Ordered list of ``TokenCandidate`` objects to consider.
        """
        solvent = [
            c for c in candidates
            if self._treasury.spendable(self._chain_id, c.token) >= amount
        ]
        if not solvent:
            return None

        # Sort ascending by (fee_bps, expected_slippage_bps, token) for a
        # fully deterministic, stable pick.
        solvent.sort(key=lambda c: (c.fee_bps, c.expected_slippage_bps, c.token))
        return solvent[0]
