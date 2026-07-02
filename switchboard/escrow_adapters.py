"""In-memory escrow client and swap settlement adapter.

Used in tests and the thinking-chain demo as a pure-Python stand-in
for the on-chain ``MultiTokenAgentEscrow`` contract.

``InMemoryEscrowClient`` satisfies ``switchboard.agent_wallet.EscrowClient``
and additionally exposes ``refund_payment`` and ``get_escrow`` for inspection.

``SwapSettlementAdapter`` wraps ``InMemoryEscrowClient`` and simulates a
token swap at 1:1 rate before creating the escrow — letting a USDC payer
settle a DAI payee in-process.
"""
from __future__ import annotations

import uuid
from typing import Dict


class InMemoryEscrowClient:
    """Pure-Python multi-token escrow store.

    Escrow lifecycle: open -> released | refunded
    """

    def __init__(self) -> None:
        self._escrows: Dict[str, dict] = {}

    # ── EscrowClient Protocol ──────────────────────────────────────────────

    def create_payment(
        self,
        chain_id: int,
        token: str,
        amount: int,
        payee: str,
    ) -> str:
        """Create an escrow entry; return an opaque escrow_id."""
        eid = f"escrow-{uuid.uuid4().hex[:8]}"
        self._escrows[eid] = {
            "escrow_id": eid,
            "chain_id": chain_id,
            "token": token,
            "amount": amount,
            "payee": payee,
            "state": "open",
        }
        return eid

    def release_payment(self, escrow_id: str) -> bool:
        """Release escrowed funds to the payee; return True on success."""
        escrow = self._get_open(escrow_id)
        escrow["state"] = "released"
        return True

    # ── Extended interface ─────────────────────────────────────────────────

    def refund_payment(self, escrow_id: str) -> bool:
        """Refund escrowed funds to the payer; return True on success."""
        escrow = self._get_open(escrow_id)
        escrow["state"] = "refunded"
        return True

    def get_escrow(self, escrow_id: str) -> dict:
        """Return the escrow record dict (for inspection / demo output)."""
        if escrow_id not in self._escrows:
            raise KeyError(f"Unknown escrow_id: {escrow_id!r}")
        return dict(self._escrows[escrow_id])

    # ── Internal ───────────────────────────────────────────────────────────

    def _get_open(self, escrow_id: str) -> dict:
        if escrow_id not in self._escrows:
            raise KeyError(f"Unknown escrow_id: {escrow_id!r}")
        escrow = self._escrows[escrow_id]
        if escrow["state"] != "open":
            raise ValueError(
                f"Escrow {escrow_id!r} is not open (state={escrow['state']!r})"
            )
        return escrow


class SwapSettlementAdapter:
    """Wraps InMemoryEscrowClient; simulates a swap then creates an escrow.

    For testing: uses a 1:1 rate so USDC -> DAI is amount-preserving.
    In production this would call a DEX aggregator.
    """

    def __init__(self, escrow_client: InMemoryEscrowClient) -> None:
        self._escrow = escrow_client

    def swap_and_create(
        self,
        chain_id: int,
        from_token: str,
        to_token: str,
        amount: int,
        payee: str,
    ) -> str:
        """Swap ``from_token`` to ``to_token`` then create an escrow.

        Returns the escrow_id of the created escrow (denominated in ``to_token``).
        """
        # Simulated 1:1 swap (no slippage, no fees in test harness).
        out_amount = amount
        return self._escrow.create_payment(
            chain_id=chain_id,
            token=to_token,
            amount=out_amount,
            payee=payee,
        )
