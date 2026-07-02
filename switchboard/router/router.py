"""Top-level Router — composes the four strategies into a single routing call.

``Router.route(...)`` is the primary entry-point.  It:
  1. Calls ``TokenSelector.select()`` to pick a source token.
  2. Calls ``RailSelector.select()`` to pick the settlement rail.
  3. Calls ``FleetBalancer.pick()`` to pick the signing wallet.
  4. Emits a ``WalletOpEvent`` to the supplied event sink.
  5. Returns a ``Plan(token, rail, wallet)``.

If no solvent token is found, a denied ``WalletOpEvent`` is emitted and a
``ValueError`` is raised (spec §6 "Insufficient balance in chosen token").
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Callable, List, Optional

from switchboard.metrics import WalletOpEvent
from switchboard.router.token_selector import TokenSelector, TokenCandidate
from switchboard.router.rail_selector import RailSelector
from switchboard.router.fleet_balancer import FleetBalancer


@dataclass(frozen=True)
class Plan:
    """The routing decision produced by ``Router.route()``.

    Parameters
    ----------
    token:
        EVM address of the token to spend.
    rail:
        Settlement rail: ``"x402"``, ``"escrow"``, or ``"mpp"``.
    wallet:
        EVM address of the signing wallet chosen by the FleetBalancer.
    """

    token: str
    rail: str
    wallet: str


class Router:
    """Composes TokenSelector, RailSelector, and FleetBalancer.

    Parameters
    ----------
    token_selector:
        Strategy for picking the source token.
    rail_selector:
        Strategy for picking the settlement rail.
    fleet_balancer:
        Strategy for picking the signing wallet.
    events:
        Optional callable that receives each ``WalletOpEvent``.  Defaults to
        a no-op so the Router is usable without a metrics backend.
    """

    def __init__(
        self,
        token_selector: TokenSelector,
        rail_selector: RailSelector,
        fleet_balancer: FleetBalancer,
        events: Optional[Callable[[WalletOpEvent], None]] = None,
    ) -> None:
        self._token_sel = token_selector
        self._rail_sel = rail_selector
        self._fleet = fleet_balancer
        self._events: Callable[[WalletOpEvent], None] = events if events is not None else _noop

    def route(
        self,
        chain_id: int,
        amount: int,
        candidates: List[TokenCandidate],
        agent_id: str = "",
        force_rail: Optional[str] = None,
    ) -> Plan:
        """Route a payment and return a ``Plan``.

        Parameters
        ----------
        chain_id:
            EVM chain ID for the payment.
        amount:
            Payment amount in the token's smallest unit.
        candidates:
            Token candidates (with optional fee/slippage metadata) for the
            ``TokenSelector`` to rank.
        agent_id:
            Logical agent identifier — included in the emitted ``WalletOpEvent``.
        force_rail:
            If set, bypasses rail selection and uses the specified rail.

        Returns
        -------
        Plan

        Raises
        ------
        ValueError
            If no candidate token has sufficient spendable balance.
        """
        # ── Step 1: Token ────────────────────────────────────────────────────
        best_candidate = self._token_sel.select(amount=amount, candidates=candidates)

        if best_candidate is None:
            ev = WalletOpEvent(
                op_type="pay",
                token="",
                rail="",
                amount=float(amount),
                agent_id=agent_id,
                wallet_id="",
                denied=True,
                denial_reason="insufficient_balance",
                timestamp=time.time(),
            )
            self._events(ev)
            raise ValueError(
                f"No solvent token found for chain_id={chain_id} amount={amount}"
            )

        token = best_candidate.token

        # ── Step 2: Rail ─────────────────────────────────────────────────────
        rail = self._rail_sel.select(amount=amount, force_rail=force_rail)

        # ── Step 3: Wallet ───────────────────────────────────────────────────
        wallet = self._fleet.pick(chain_id=chain_id)

        # ── Step 4: Emit event ───────────────────────────────────────────────
        ev = WalletOpEvent(
            op_type="pay",
            token=token,
            rail=rail,
            amount=float(amount),
            agent_id=agent_id,
            wallet_id=wallet,
            denied=False,
            denial_reason=None,
            timestamp=time.time(),
        )
        self._events(ev)

        return Plan(token=token, rail=rail, wallet=wallet)


def _noop(_: WalletOpEvent) -> None:
    """No-op event sink used when no metrics backend is wired."""
