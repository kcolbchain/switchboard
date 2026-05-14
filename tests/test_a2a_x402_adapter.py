"""Round-trip tests for the A2A x402 adapter."""

import pytest

from switchboard.adapters.a2a_x402 import (
    A2AX402AdapterError,
    chain_id_from_network,
    from_a2a_request,
    from_a2a_response,
    network_from_chain_id,
    to_a2a_receipt,
    to_a2a_request,
    to_a2a_submission,
)
from switchboard.x402_middleware import PaymentOffer, PaymentProof


def test_offer_to_a2a_request_uses_spec_shape():
    offer = PaymentOffer(
        amount_wei=48_240_000,
        currency="0x833589fCD6eDb6E08f4c7C32D4f71b54bda02913",
        recipient="0xServerWalletAddressHere",
        chain_id=8453,
        description="Generate an image",
        endpoint="https://api.example.com/generate-image",
        nonce="nonce-1",
        expires_at=1_800_000_000,
    )

    payload = to_a2a_request(offer)

    assert payload["x402Version"] == 1
    requirement = payload["accepts"][0]
    assert requirement["scheme"] == "exact"
    assert requirement["network"] == "base"
    assert requirement["resource"] == "https://api.example.com/generate-image"
    assert requirement["asset"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bda02913"
    assert requirement["payTo"] == "0xServerWalletAddressHere"
    assert requirement["maxAmountRequired"] == "48240000"
    assert requirement["extra"]["nonce"] == "nonce-1"
    assert requirement["extra"]["expiresAt"] == 1_800_000_000


def test_a2a_request_roundtrips_to_payment_offer():
    original = PaymentOffer(
        amount_wei=1000,
        currency="USDC",
        recipient="0xabc",
        chain_id=84532,
        endpoint="/paid/task",
        nonce="n1",
    )

    parsed = from_a2a_request(to_a2a_request(original))

    assert parsed.amount_wei == original.amount_wei
    assert parsed.currency == original.currency
    assert parsed.recipient == original.recipient
    assert parsed.chain_id == original.chain_id
    assert parsed.endpoint == original.endpoint
    assert parsed.nonce == original.nonce


def test_a2a_receipt_converts_to_payment_proof():
    payload = {
        "metadata": {
            "x402.payment.status": "payment-completed",
            "x402.payment.receipts": [
                {
                    "success": True,
                    "transaction": "0xabc123",
                    "network": "base",
                    "payer": "0xpayer",
                    "amount": "48240000",
                    "nonce": "nonce-1",
                    "timestamp": 1_700_000_000,
                }
            ],
        }
    }

    proof = from_a2a_response(payload)

    assert proof.tx_hash == "0xabc123"
    assert proof.chain_id == 8453
    assert proof.payer == "0xpayer"
    assert proof.amount_wei == 48_240_000
    assert proof.nonce == "nonce-1"
    assert proof.timestamp == 1_700_000_000


def test_a2a_payment_payload_aliases_convert_to_payment_proof():
    payload = {
        "x402.payment.payload": {
            "x402Version": 1,
            "network": "base-sepolia",
            "scheme": "exact",
            "payload": {
                "txHash": "0xdef456",
                "payer": "0xpayer",
                "amount": "2500",
            },
        }
    }

    proof = from_a2a_response(payload)

    assert proof.tx_hash == "0xdef456"
    assert proof.chain_id == 84532
    assert proof.amount_wei == 2500


def test_direct_payment_payload_preserves_outer_network():
    payload = {
        "x402Version": 1,
        "network": "base",
        "scheme": "exact",
        "payload": {
            "txHash": "0xfeed",
            "payer": "0xpayer",
            "amount": "12",
        },
    }

    proof = from_a2a_response(payload)

    assert proof.tx_hash == "0xfeed"
    assert proof.chain_id == 8453


def test_switchboard_proof_exports_submission_and_receipt():
    proof = PaymentProof(
        tx_hash="0xabc",
        chain_id=8453,
        payer="0xpayer",
        amount_wei=999,
        nonce="n1",
        timestamp=1_700_000_000,
    )

    submission = to_a2a_submission(proof)
    receipt = to_a2a_receipt(proof)

    assert submission["x402.payment.status"] == "payment-submitted"
    assert submission["x402.payment.payload"]["network"] == "base"
    assert submission["x402.payment.payload"]["payload"]["txHash"] == "0xabc"
    assert receipt["success"] is True
    assert receipt["transaction"] == "0xabc"
    assert receipt["network"] == "base"


def test_unknown_network_is_rejected():
    with pytest.raises(A2AX402AdapterError, match="Unknown"):
        chain_id_from_network("unknown-chain")


def test_eip155_network_roundtrip():
    assert network_from_chain_id(10) == "eip155:10"
    assert chain_id_from_network("eip155:10") == 10


def test_response_without_transaction_is_rejected():
    with pytest.raises(A2AX402AdapterError, match="transaction"):
        from_a2a_response({"network": "base", "amount": "1"})
