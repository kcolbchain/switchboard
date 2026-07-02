"""Integration tests for the agent-wallet / multi-token-settlement wiring pass.

Each ``class`` here corresponds to one reconciled *seam* between units that were
built in parallel.  These are integration tests: they assert the units compose
into one working system, not the internal behavior of any single unit (that is
covered by the per-unit suites).

Seams
-----
1. Canonical ``PaymentRequest`` — ``AgentWallet`` uses ``src.payment_protocol``'s
   ``PaymentRequest`` (with ``settlement_token``), not a private copy.
2. Single ``WalletOpEvent`` — ``access_policy`` emits ``metrics.WalletOpEvent`` so
   denials flow to the ⑳ dashboard.
3. ``AccessPolicy`` satisfies the MCP ``AccessPolicy`` Protocol and the real
   engine gates MCP calls.
4. ``Router`` sits in the ``AgentWallet.pay`` path: it picks (token, rail,
   wallet), consults ``access_policy``, and emits a ``WalletOpEvent``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest


# Token addresses used across the seams
ETH = "0x0000000000000000000000000000000000000000"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
CHAIN_1 = 1
PAYEE = "0xPayee000000000000000000000000000000000001"


# ===========================================================================
# Seam 1 — Canonical PaymentRequest
# ===========================================================================


class TestSeam1CanonicalPaymentRequest:
    def test_agent_wallet_reexports_canonical_payment_request(self):
        """``switchboard.agent_wallet.PaymentRequest`` IS the canonical protocol type."""
        from switchboard.agent_wallet import PaymentRequest as WalletPR
        from src.payment_protocol import PaymentRequest as CanonicalPR

        assert WalletPR is CanonicalPR

    def test_canonical_request_carries_settlement_token_field(self):
        from switchboard.agent_wallet import PaymentRequest

        req = PaymentRequest(chain_id=CHAIN_1, token=USDC, amount_wei=100, payee=PAYEE)
        # settlement_token is the v1.2 negotiation result field.
        assert hasattr(req, "settlement_token")
        assert req.settlement_token is None

    def test_amount_alias_reads_amount_wei(self):
        from switchboard.agent_wallet import PaymentRequest

        req = PaymentRequest(chain_id=CHAIN_1, token=USDC, amount_wei=42, payee=PAYEE)
        assert req.amount == 42
        assert req.amount_wei == 42

    def test_delegation_shares_the_same_request_type(self):
        """Delegation imports PaymentRequest transitively; must be the canonical one."""
        from switchboard.delegation import PaymentRequest as DelegationPR
        from src.payment_protocol import PaymentRequest as CanonicalPR

        assert DelegationPR is CanonicalPR

    def test_pay_end_to_end_with_canonical_request(self):
        from switchboard.agent_wallet import AgentWallet, PaymentRequest, EscrowClient
        from switchboard.mpc_wallet import MPCWallet
        from switchboard.treasury import Treasury

        treasury = Treasury()
        treasury.credit(CHAIN_1, USDC, 1_000_000)
        escrow = MagicMock(spec=EscrowClient)
        escrow.create_payment.return_value = "0xescrow_seam1"
        escrow.release_payment.return_value = True
        wallet = AgentWallet(mpc=MPCWallet(), treasury=treasury, escrow=escrow)

        req = PaymentRequest(chain_id=CHAIN_1, token=USDC, amount_wei=250_000, payee=PAYEE)
        receipt = wallet.pay(req)

        assert receipt.token == USDC
        assert receipt.amount == 250_000
        assert receipt.escrow_id == "0xescrow_seam1"
        assert treasury.balance(CHAIN_1, USDC) == 750_000

    def test_token_field_off_the_v1_wire_and_hash(self):
        """The multi-token ``token`` field must not perturb the frozen wire/hash."""
        from switchboard.agent_wallet import PaymentRequest

        bare = PaymentRequest(request_id="w", payer="0xA", payee="0xB", amount_wei=10**18)
        with_tok = PaymentRequest(
            request_id="w", payer="0xA", payee="0xB", amount_wei=10**18, token=USDC
        )
        # token at default ("") is omitted from the wire entirely
        assert "token" not in bare.to_json()
        # a set token never changes the content hash (it is a wallet-side selection)
        assert bare.content_hash() == with_tok.content_hash()
