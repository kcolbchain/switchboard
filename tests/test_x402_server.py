"""Tests for x402 server middleware."""

import json

from switchboard.x402.server import (
    PAYMENT_HEADER,
    PAYMENT_PROOF_HEADER,
    WWW_AUTHENTICATE_X402,
    PaymentRequirements,
    PaymentVerifier,
    X402Server,
    inject_payment,
    map_payment_envelope,
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


# ─── x402 HTTP envelope propagation (#89, mirrors hanzoai/mcp#9) ──────────────


def test_inject_payment_merges_json_header_under_payment_key():
    params = {"prompt": "hello"}
    header = json.dumps({"txHash": "0xtx", "payer": "0xp", "amount": "1.00"})
    out = inject_payment(params, header)
    assert out["prompt"] == "hello"
    assert out["payment"] == {"txHash": "0xtx", "payer": "0xp", "amount": "1.00"}


def test_inject_payment_keeps_non_json_header_as_string():
    out = inject_payment({}, "opaque-proof-token")
    assert out["payment"] == "opaque-proof-token"


def test_inject_payment_no_header_is_noop():
    params = {"prompt": "hi"}
    out = inject_payment(params, "")
    assert "payment" not in out
    assert out == {"prompt": "hi"}


def test_inject_payment_does_not_overwrite_caller_structured_payment():
    caller = {"payment": {"txHash": "0xCALLER"}}
    header = json.dumps({"txHash": "0xHEADER"})
    out = inject_payment(caller, header)
    # An explicit, structured caller payment always wins over the transport header.
    assert out["payment"] == {"txHash": "0xCALLER"}


def test_map_payment_envelope_maps_402_with_www_authenticate():
    result = {
        "status": 402,
        "payment_required": {
            "scheme": "exact",
            "network": "base",
            "asset": "USDC",
            "amount": "2.50",
            "payTo": "0xpay",
        },
    }
    mapped = map_payment_envelope(result)
    assert mapped is not None
    status, headers, body = mapped
    assert status == 402
    assert headers["WWW-Authenticate"] == WWW_AUTHENTICATE_X402
    reqs = PaymentRequirements.from_header(headers["X-Payment-Required"])
    assert reqs.amount == "2.50"
    assert reqs.pay_to == "0xpay"
    assert json.loads(body)["payment_requirements"]["amount"] == "2.50"


def test_map_payment_envelope_ignores_ok_result():
    assert map_payment_envelope({"status": "ok", "data": 1}) is None


def test_map_payment_envelope_status_402_without_payment_required_is_none():
    # A bare 402 (e.g. an opaque upstream 402) is not an x402 envelope.
    assert map_payment_envelope({"status": 402}) is None
    assert map_payment_envelope({"status": 402, "payment_required": "nope"}) is None


def test_map_payment_envelope_ignores_non_dict():
    assert map_payment_envelope("not-a-dict") is None
    assert map_payment_envelope(None) is None


def test_read_payment_header_prefers_x_payment_then_falls_back():
    assert X402Server.read_payment_header({PAYMENT_HEADER: "new", PAYMENT_PROOF_HEADER: "old"}) == "new"
    assert X402Server.read_payment_header({PAYMENT_PROOF_HEADER: "old"}) == "old"
    assert X402Server.read_payment_header({}) == ""


def test_build_402_response_advertises_x402_scheme():
    server = X402Server(pay_to_address="0xpay", amount_usdc="1.00")
    _status, headers, _body = server.build_402_response()
    assert headers["WWW-Authenticate"] == WWW_AUTHENTICATE_X402
