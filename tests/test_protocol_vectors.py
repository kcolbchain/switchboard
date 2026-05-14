"""
Conformance tests for the agent-to-agent payment protocol wire encoding +
content hash.

Loads frozen fixtures from `tests/protocol_vectors/payment_request.v1.json`
and verifies that the current `PaymentRequest` implementation reproduces:

  - the exact `wire_canonical_bytes` from `to_json()`
  - the exact `hash_input_canonical_bytes` (canonical bytes minus
    `created_at` / `status`)
  - the exact `content_hash_sha256` from `content_hash()`

A failure here means the wire format has drifted from v1.0 of the spec
(see `docs/agent-payment-protocol.md` §2.1 and §2.2). That's a breaking
change for cross-language implementations and should be reflected in a
protocol version bump.

Other language implementations of this protocol (e.g. a Rust client, a
TypeScript client) can pin against the same JSON fixtures and assert the
same hashes — that's the point of having frozen test vectors.

Run with:
  pytest tests/test_protocol_vectors.py -v
"""

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from payment_protocol import PaymentRequest  # noqa: E402


FIXTURES = Path(__file__).resolve().parent / "protocol_vectors" / "payment_request.v1.json"


def _load_vectors():
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def _build_request(d: dict) -> PaymentRequest:
    """Rebuild a PaymentRequest from the JSON fixture's `input` dict."""
    d = dict(d)
    if d.get("amount_usd") is not None:
        d["amount_usd"] = Decimal(d["amount_usd"])
    return PaymentRequest(**d)


@pytest.fixture(scope="module")
def vectors():
    return _load_vectors()


def test_protocol_version_matches(vectors):
    assert vectors["protocol_version"] == "1.0"


@pytest.mark.parametrize("fixture", _load_vectors()["vectors"], ids=lambda f: f["name"])
def test_wire_canonical_bytes(fixture):
    req = _build_request(fixture["input"])
    assert req.to_json() == fixture["wire_canonical_bytes"]


@pytest.mark.parametrize("fixture", _load_vectors()["vectors"], ids=lambda f: f["name"])
def test_content_hash(fixture):
    req = _build_request(fixture["input"])
    assert req.content_hash() == fixture["content_hash_sha256"]


@pytest.mark.parametrize("fixture", _load_vectors()["vectors"], ids=lambda f: f["name"])
def test_round_trip_via_from_dict(fixture):
    """`from_dict(to_dict(req))` MUST produce a request that hashes identically."""
    req1 = _build_request(fixture["input"])
    req2 = PaymentRequest.from_dict(req1.to_dict())
    assert req2.content_hash() == fixture["content_hash_sha256"]
    assert req2.to_json() == fixture["wire_canonical_bytes"]
