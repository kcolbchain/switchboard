"""Tests for x402 server-side middleware."""

import asyncio
import json
import pytest
import time
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock, patch

from switchboard.x402_middleware import (
    X402Middleware,
    PaymentOffer,
    PaymentProof,
    PaymentScheme,
    PaymentRecord,
)


# ─── PaymentOffer Tests ──────────────────────────────────────────────────────

class TestPaymentOffer:
    def test_from_header_minimal(self):
        header = json.dumps({"amount": "1000000", "recipient": "0xABC", "chainId": 8453})
        offer = PaymentOffer.from_header(header, endpoint="/api/infer")
        assert offer.amount_wei == 1000000
        assert offer.recipient == "0xABC"
        assert offer.chain_id == 8453
        assert offer.scheme == PaymentScheme.EXACT
        assert offer.endpoint == "/api/infer"

    def test_from_header_full(self):
        header = json.dumps({
            "amount": "5000000000000000",
            "recipient": "0xDEF",
            "chainId": 1,
            "currency": "ETH",
            "scheme": "escrow",
            "description": "inference job",
            "nonce": "abc123",
            "expiresAt": int(time.time()) + 3600,
        })
        offer = PaymentOffer.from_header(header)
        assert offer.scheme == PaymentScheme.ESCROW
        assert offer.nonce == "abc123"
        assert not offer.is_expired()

    def test_expired_offer(self):
        offer = PaymentOffer(
            amount_wei=100,
            currency="ETH",
            recipient="0x1",
            chain_id=1,
            expires_at=int(time.time()) - 10,
        )
        assert offer.is_expired()

    def test_no_expiry_not_expired(self):
        offer = PaymentOffer(
            amount_wei=100, currency="ETH", recipient="0x1", chain_id=1
        )
        assert not offer.is_expired()


# ─── PaymentProof Tests ──────────────────────────────────────────────────────

class TestPaymentProof:
    def test_to_header_roundtrip(self):
        proof = PaymentProof(
            tx_hash="0xabc",
            chain_id=8453,
            payer="0x123",
            amount_wei=1000000,
            nonce="n1",
        )
        header = proof.to_header()
        data = json.loads(header)
        assert data["txHash"] == "0xabc"
        assert data["chainId"] == 8453
        assert data["payer"] == "0x123"
        assert data["amount"] == 1000000


# ─── Middleware Validation Tests ─────────────────────────────────────────────

class TestMiddlewareValidation:
    def _make_middleware(self, **kwargs):
        client = MagicMock()
        client.wallet_address = "0xPAYER"
        return X402Middleware(payment_client=client, **kwargs)

    def test_rejects_expired_offer(self):
        mw = self._make_middleware()
        offer = PaymentOffer(
            amount_wei=100, currency="ETH", recipient="0x1",
            chain_id=1, expires_at=int(time.time()) - 1,
        )
        with pytest.raises(ValueError, match="expired"):
            mw._validate_offer(offer)

    def test_rejects_over_cap(self):
        mw = self._make_middleware(max_payment_wei=1000)
        offer = PaymentOffer(
            amount_wei=2000, currency="ETH", recipient="0x1", chain_id=1,
        )
        with pytest.raises(ValueError, match="exceeds cap"):
            mw._validate_offer(offer)

    def test_rejects_unknown_recipient(self):
        mw = self._make_middleware(allowed_recipients={"0xGOOD"})
        offer = PaymentOffer(
            amount_wei=100, currency="ETH", recipient="0xBAD", chain_id=1,
        )
        with pytest.raises(ValueError, match="not in allowlist"):
            mw._validate_offer(offer)

    def test_accepts_valid_offer(self):
        mw = self._make_middleware(
            max_payment_wei=10**18,
            allowed_recipients={"0xGOOD"},
        )
        offer = PaymentOffer(
            amount_wei=10**15, currency="ETH", recipient="0xGOOD", chain_id=1,
        )
        mw._validate_offer(offer)  # Should not raise

    def test_rejects_over_gas_budget(self):
        tracker = MagicMock()
        tracker.can_send_transaction.return_value = False
        mw = self._make_middleware(gas_tracker=tracker)
        offer = PaymentOffer(
            amount_wei=100, currency="ETH", recipient="0x1", chain_id=1,
        )
        with pytest.raises(ValueError, match="gas budget"):
            mw._validate_offer(offer)


# ─── Payment Execution Tests ────────────────────────────────────────────────

class TestPaymentExecution:
    def test_exact_payment(self):
        client = MagicMock()
        client.wallet_address = "0xPAYER"
        client.sign_and_send.return_value = "0xTXHASH"
        client.wait_for_confirmations.return_value = {"status": 1}

        mw = X402Middleware(payment_client=client)
        offer = PaymentOffer(
            amount_wei=10**15, currency="ETH",
            recipient="0xRECIPIENT", chain_id=8453, nonce="n1",
        )

        proof = mw._pay_onchain(offer)
        assert proof.tx_hash == "0xTXHASH"
        assert proof.payer == "0xPAYER"
        assert proof.chain_id == 8453

        client.sign_and_send.assert_called_once()
        tx_arg = client.sign_and_send.call_args[0][0]
        assert tx_arg["to"] == "0xRECIPIENT"
        assert tx_arg["value"] == 10**15

    def test_escrow_payment(self):
        client = MagicMock()
        client.wallet_address = "0xPAYER"
        mock_req = MagicMock()
        mock_req.request_id = "req-123"
        client.create_payment.return_value = mock_req

        mw = X402Middleware(payment_client=client)
        offer = PaymentOffer(
            amount_wei=10**15, currency="ETH",
            recipient="0xRECIPIENT", chain_id=1,
            scheme=PaymentScheme.ESCROW,
        )

        proof = mw._pay_onchain(offer)
        assert proof.tx_hash == "req-123"
        client.create_payment.assert_called_once()


# ─── Spend Summary Tests ────────────────────────────────────────────────────

class TestSpendSummary:
    def test_empty_summary(self):
        client = MagicMock()
        client.wallet_address = "0x1"
        mw = X402Middleware(payment_client=client)
        summary = mw.get_spend_summary()
        assert summary["total_payments"] == 0
        assert summary["total_spent_wei"] == 0

    def test_tracks_payments(self):
        client = MagicMock()
        client.wallet_address = "0x1"
        mw = X402Middleware(payment_client=client)

        offer = PaymentOffer(amount_wei=1000, currency="ETH", recipient="0x2", chain_id=1)
        proof = PaymentProof(tx_hash="0xa", chain_id=1, payer="0x1", amount_wei=1000)
        mw.payment_history.append(PaymentRecord(
            endpoint="/api/a", offer=offer, proof=proof, response_status=200,
        ))
        mw.total_spent_wei = 1000

        summary = mw.get_spend_summary()
        assert summary["total_payments"] == 1
        assert summary["total_spent_wei"] == 1000
        assert summary["by_endpoint"]["/api/a"] == 1000
