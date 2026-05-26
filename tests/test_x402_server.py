"""Tests for x402 server middleware."""

import json
from switchboard.x402.server import (
    X402Server,
    PaymentVerifier,
    PaymentRequirements,
)


def test_payment_requirements_header_roundtrip():
    reqs = PaymentRequirements(
        scheme="exact",
        network="base",
        asset="USDC",
        amount="1.00",
        pay_to="0xabc",
        nonce="test-nonce",
    )
    header = reqs.to_header()
    parsed = PaymentRequirements.from_header(header)
    assert parsed.scheme == "exact"
    assert parsed.network == "base"
    assert parsed.asset == "USDC"
    assert parsed.amount == "1.00"
    assert parsed.pay_to == "0xabc"
    assert parsed.nonce == "test-nonce"


def test_verifier_accepts_valid_payload():
    verifier = PaymentVerifier(secret_key="test-secret")
    reqs = PaymentRequirements(pay_to="0xabc", amount="1.00", nonce="n1")
    valid_payload = json.dumps({
        "txHash": "0xtx",
        "payer": "0xpayer",
        "amount": "1.00",
        "nonce": "n1",
        "sig": "",
    })
    assert verifier.verify(valid_payload, reqs)


def test_verifier_rejects_empty_payload():
    verifier = PaymentVerifier()
    reqs = PaymentRequirements()
    assert not verifier.verify("", reqs)


def test_verifier_rejects_duplicate_nonce():
    verifier = PaymentVerifier(secret_key="test")
    reqs = PaymentRequirements(pay_to="0xabc", amount="1.00", nonce="dup")
    payload = json.dumps({
        "txHash": "0xtx", "payer": "0xp", "amount": "1.00", "nonce": "dup", "sig": "",
    })
    assert verifier.verify(payload, reqs)
    assert not verifier.verify(payload, reqs)


def test_server_builds_402_response():
    server = X402Server(pay_to_address="0xpay", amount_usdc="5.00")
    status, headers, body = server.build_402_response()
    assert status == 402
    assert "X-Payment-Required" in headers
    parsed = json.loads(body)
    assert parsed["error"] == "payment_required"


def test_flask_app_creation():
    server = X402Server(pay_to_address="0xpay")
    app = server.flask_app()
    assert app is not None
    assert app.name == "x402.server"


def test_x402_verifier_idempotent():
    verifier = PaymentVerifier(secret_key="test")
    assert not verifier.is_idempotent("nonexistent")
    reqs = PaymentRequirements(pay_to="0xa", amount="1", nonce="n2")
    payload = json.dumps({
        "txHash": "0xtx", "payer": "0xp", "amount": "1", "nonce": "n2", "sig": "",
    })
    verifier.verify(payload, reqs)
    assert verifier.is_idempotent("n2")
