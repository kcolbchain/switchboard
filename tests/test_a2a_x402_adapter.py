from __future__ import annotations

import pytest

from switchboard.adapters.a2a_x402 import (
    PAYMENT_PAYLOAD_KEY,
    PAYMENT_REQUIRED_KEY,
    PAYMENT_STATUS_KEY,
    from_a2a_response,
    to_a2a_request,
)
from switchboard.x402_middleware import PaymentOffer, PaymentProof, PaymentScheme

RECIPIENT = "0x1111111111111111111111111111111111111111"
PAYER = "0x2222222222222222222222222222222222222222"
SIGNATURE = "0x" + "ab" * 32


def sample_offer() -> PaymentOffer:
    return PaymentOffer(
        amount_wei=1_000_000,
        currency="USDC",
        recipient=RECIPIENT,
        chain_id=8453,
        scheme=PaymentScheme.EXACT,
        description="Generate one image",
        endpoint="/v1/images",
        nonce="offer-nonce-1",
        expires_at=2_000_000_000,
    )


def sample_payment_payload() -> dict:
    return {
        "x402Version": 1,
        "scheme": "exact",
        "network": "base",
        "payload": {
            "signature": SIGNATURE,
            "authorization": {
                "from": PAYER,
                "to": RECIPIENT,
                "value": "1000000",
                "validAfter": "0",
                "validBefore": "2000000000",
                "nonce": "proof-nonce-1",
            },
        },
    }


def test_to_a2a_request_places_payment_required_metadata():
    request = to_a2a_request(sample_offer())

    assert request["jsonrpc"] == "2.0"
    assert request["method"] == "message/send"
    metadata = request["params"]["message"]["metadata"]
    assert metadata[PAYMENT_STATUS_KEY] == "payment-required"

    required = metadata[PAYMENT_REQUIRED_KEY]
    assert required["x402Version"] == 1
    assert required["error"] == "Payment required"
    assert len(required["accepts"]) == 1


def test_to_a2a_request_maps_offer_to_payment_requirements():
    request = to_a2a_request(sample_offer())
    requirement = request["params"]["message"]["metadata"][PAYMENT_REQUIRED_KEY]["accepts"][0]

    assert requirement["scheme"] == "exact"
    assert requirement["network"] == "base"
    assert requirement["payTo"] == RECIPIENT
    assert requirement["maxAmountRequired"] == "1000000"
    assert requirement["asset"] == "USDC"
    assert requirement["resource"] == "/v1/images"
    assert requirement["description"] == "Generate one image"
    assert requirement["mimeType"] == "application/json"
    assert requirement["extra"]["switchboard"]["chainId"] == 8453
    assert requirement["extra"]["switchboard"]["nonce"] == "offer-nonce-1"


def test_to_a2a_request_uses_eip155_network_for_unknown_chain():
    offer = sample_offer()
    offer.chain_id = 999999

    requirement = to_a2a_request(offer)["params"]["message"]["metadata"][PAYMENT_REQUIRED_KEY][
        "accepts"
    ][0]

    assert requirement["network"] == "eip155:999999"


def test_from_a2a_response_accepts_full_jsonrpc_message_send_body():
    response = {
        "jsonrpc": "2.0",
        "method": "message/send",
        "params": {
            "message": {
                "taskId": "task-123",
                "role": "user",
                "parts": [{"kind": "text", "text": "Here is the payment authorization."}],
                "metadata": {
                    PAYMENT_STATUS_KEY: "payment-submitted",
                    PAYMENT_PAYLOAD_KEY: sample_payment_payload(),
                },
            }
        },
    }

    proof = from_a2a_response(response)

    assert isinstance(proof, PaymentProof)
    assert proof.tx_hash == SIGNATURE
    assert proof.chain_id == 8453
    assert proof.payer == PAYER
    assert proof.amount_wei == 1_000_000
    assert proof.nonce == "proof-nonce-1"


def test_from_a2a_response_accepts_metadata_dict_directly():
    proof = from_a2a_response({PAYMENT_PAYLOAD_KEY: sample_payment_payload()})

    assert proof.tx_hash == SIGNATURE
    assert proof.chain_id == 8453
    assert proof.payer == PAYER


def test_from_a2a_response_prefers_facilitator_transaction_reference_when_present():
    payload = sample_payment_payload()
    payload["payload"]["transaction"] = "0xsettled"

    proof = from_a2a_response(payload)

    assert proof.tx_hash == "0xsettled"


def test_from_a2a_response_supports_eip155_networks_and_top_level_amounts():
    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": "eip155:11155111",
        "payload": {
            "txHash": "0xabc123",
            "payer": PAYER,
            "amount": 42,
            "nonce": "n1",
        },
    }

    proof = from_a2a_response(payload)

    assert proof.chain_id == 11155111
    assert proof.tx_hash == "0xabc123"
    assert proof.amount_wei == 42
    assert proof.nonce == "n1"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"network": "base"}, "missing payment payload"),
        ({"network": "unknown", "payload": {"signature": SIGNATURE}}, "Unsupported x402 network"),
        ({"network": "base", "payload": {"signature": SIGNATURE}}, "missing payer/from"),
        (
            {"network": "base", "payload": {"signature": SIGNATURE, "payer": PAYER}},
            "missing numeric amount/value",
        ),
    ],
)
def test_from_a2a_response_rejects_malformed_payloads(payload: dict, message: str):
    with pytest.raises(ValueError, match=message):
        from_a2a_response(payload)
