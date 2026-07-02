"""Unit ⑬ — Rebalancer.

Computes *intended* swap operations to move the treasury allocation toward a
target ratio.  Emits ``SwapIntent`` objects; does NOT execute swaps — the
adapter layer (lucidly / SwapSettlementAdapter) handles execution.

Algorithm:
  1. Validate that target percentages sum to 100 (±0.01 float tolerance).
  2. Compute total treasury value on the chain as the sum of all token balances.
  3. For each target token, compute the ideal amount and the delta
     (current - ideal).  Positive delta = overweight → sell; negative = underweight → buy.
  4. Emit one ``SwapIntent`` per overweight token (from_token → least
     underweight to_token) if the absolute delta exceeds ``min_rebalance_pct``
     of the total.

Notes
-----
* The Rebalancer works in raw balance units.  If tokens have different
  decimals the caller must normalise before supplying balances, or provide a
  price oracle.  For the initial unit this simplification is intentional
  (spec §9 open decision 3).
* ``SwapIntent`` carries only the recommendation; the adapter decides slippage
  bounds and execution timing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from switchboard.treasury import Treasury


@dataclass(frozen=True)
class RebalanceTarget:
    """A token allocation target.

    Parameters
    ----------
    token:
        EVM address of the token.
    target_pct:
        Target percentage of total portfolio value for this token (0–100).
    """

    token: str
    target_pct: float


@dataclass(frozen=True)
class SwapIntent:
    """An intended swap to rebalance the treasury.

    The adapter is responsible for executing (or declining) this swap.

    Parameters
    ----------
    from_token:
        Token to sell (overweight).
    to_token:
        Token to buy (underweight).
    amount:
        Raw amount of ``from_token`` to sell.
    chain_id:
        Chain on which the swap should be executed.
    """

    from_token: str
    to_token: str
    amount: int
    chain_id: int


_PCT_TOLERANCE = 0.01  # float tolerance for "sums to 100"


class Rebalancer:
    """Computes rebalancing swap intents for a treasury.

    Parameters
    ----------
    treasury:
        The Treasury to read balances from.
    chain_id:
        The chain to consider for rebalancing.
    min_rebalance_pct:
        Minimum imbalance (as a percentage of total portfolio) before a swap
        is emitted.  Default is 1 % — avoids trivial swaps.
    """

    def __init__(
        self,
        treasury: Treasury,
        chain_id: int,
        min_rebalance_pct: float = 1.0,
    ) -> None:
        self._treasury = treasury
        self._chain_id = chain_id
        self._min_rebalance_pct = min_rebalance_pct

    def rebalance_targets(self, targets: List[RebalanceTarget]) -> List[SwapIntent]:
        """Compute the swap intents needed to reach ``targets``.

        Parameters
        ----------
        targets:
            Desired allocation; percentages must sum to 100 (±tolerance).

        Returns
        -------
        List of ``SwapIntent`` objects, possibly empty if already balanced.

        Raises
        ------
        ValueError
            If target percentages do not sum to 100.
        """
        total_pct = sum(t.target_pct for t in targets)
        if abs(total_pct - 100.0) > _PCT_TOLERANCE:
            raise ValueError(
                f"Target percentages must sum to 100, got {total_pct:.4f}"
            )

        # Snapshot current balances for the relevant tokens.
        balances = {
            t.token: self._treasury.balance(self._chain_id, t.token)
            for t in targets
        }
        total_value = sum(balances.values())

        if total_value == 0:
            # Nothing to rebalance — emit buy intents for all underweight tokens
            # only if there is actually something to sell (which there isn't).
            return []

        threshold = total_value * self._min_rebalance_pct / 100.0

        # Compute deltas: positive = overweight (should sell), negative = underweight.
        deltas: dict[str, float] = {}
        for t in targets:
            ideal = total_value * t.target_pct / 100.0
            deltas[t.token] = balances[t.token] - ideal

        overweight  = {tok: d for tok, d in deltas.items() if d > threshold}
        underweight = {tok: -d for tok, d in deltas.items() if -d > threshold}

        if not overweight or not underweight:
            return []

        intents: List[SwapIntent] = []
        # Pair each overweight token's surplus proportionally across all
        # underweight tokens, so every underweight token gets at least one
        # buy intent.  In the common case (one overweight, multiple underweight)
        # this produces one SwapIntent per underweight token.
        underweight_items = sorted(underweight.items(), key=lambda x: -x[1])
        overweight_items  = sorted(overweight.items(),  key=lambda x: -x[1])

        total_deficit  = sum(underweight.values())
        total_surplus  = sum(overweight.values())

        for to_token, deficit in underweight_items:
            # Allocate across overweight sources proportionally.
            remaining = int(deficit)
            for from_token, surplus in overweight_items:
                if surplus <= 0 or remaining <= 0:
                    continue
                # Sell min(remaining deficit, available surplus) of from_token.
                sell = min(remaining, int(surplus))
                if sell > 0:
                    intents.append(
                        SwapIntent(
                            from_token=from_token,
                            to_token=to_token,
                            amount=sell,
                            chain_id=self._chain_id,
                        )
                    )
                    remaining -= sell
                    # Update surplus tracking (mutable copy of dict value).
                    overweight_items = [
                        (ft, s - sell if ft == from_token else s)
                        for ft, s in overweight_items
                    ]

        return intents
