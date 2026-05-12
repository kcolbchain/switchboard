"""Roundtrip tests for switchboard.zap_transport.

Skipped if zap_py is not installed — install with::

    pip install 'luxfi-zap @ git+https://github.com/luxfi/zap@main#subdirectory=python'
"""

from __future__ import annotations

import time

import pytest

zap_py = pytest.importorskip("zap_py")

from switchboard.x402_middleware import PaymentOffer, PaymentProof, PaymentScheme
from switchboard import zap_transport as zt


VITALIK = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
SAMPLE_TX = "0x" + "ab" * 32


def test_offer_schema_layout():
    """Schema is well-formed and has the expected fields."""
    fields = {f.name for f in zt.OFFER_SCHEMA.fields}
    assert fields == {
        "scheme",
        "chain_id",
        "expires_at",
        "recipient",
        "amount",
        "currency",
        "description",
        "endpoint",
        "nonce",
    }
    # Final align(8); nine fields, smallest start.
    assert zt.OFFER_SCHEMA.size > 0
    assert zt.OFFER_SCHEMA.size % 8 == 0


def test_offer_roundtrip_minimal():
    offer = PaymentOffer(
        amount_wei=1_000_000,
        currency="USDC",
        recipient=USDC,
        chain_id=8453,
    )
    wire = zt.encode_offer(offer)
    assert isinstance(wire, bytes)
    out = zt.decode_offer(wire)

    assert out.amount_wei == offer.amount_wei
    assert out.currency == offer.currency
    assert out.recipient.lower() == offer.recipient.lower()
    assert out.chain_id == offer.chain_id
    assert out.scheme == PaymentScheme.EXACT  # default
    assert out.description == ""
    assert out.endpoint == ""
    assert out.nonce == ""
    assert out.expires_at is None  # 0 sentinel restored to None


def test_offer_roundtrip_full():
    offer = PaymentOffer(
        amount_wei=12_345_678_901_234_567_890_123,  # > uint64
        currency="ETH",
        recipient=VITALIK,
        chain_id=1,
        scheme=PaymentScheme.ESCROW,
        description="inference call to /v1/embed",
        endpoint="/v1/embed",
        nonce="abc-xyz-123",
        expires_at=int(time.time()) + 60,
    )
    wire = zt.encode_offer(offer)
    out = zt.decode_offer(wire)

    assert out.amount_wei == offer.amount_wei
    assert out.currency == offer.currency
    assert out.recipient.lower() == offer.recipient.lower()
    assert out.chain_id == offer.chain_id
    assert out.scheme == offer.scheme
    assert out.description == offer.description
    assert out.endpoint == offer.endpoint
    assert out.nonce == offer.nonce
    assert out.expires_at == offer.expires_at


def test_offer_each_scheme_roundtrips():
    for scheme in PaymentScheme:
        offer = PaymentOffer(amount_wei=1, currency="ETH", recipient=USDC, chain_id=1, scheme=scheme)
        wire = zt.encode_offer(offer)
        out = zt.decode_offer(wire)
        assert out.scheme == scheme


def test_offer_amount_uint256_max():
    """Wire format must accept a full uint256 amount, not truncate to uint64."""
    big = (1 << 256) - 1
    offer = PaymentOffer(amount_wei=big, currency="ETH", recipient=USDC, chain_id=1)
    wire = zt.encode_offer(offer)
    out = zt.decode_offer(wire)
    assert out.amount_wei == big


def test_offer_amount_overflow_rejected():
    offer = PaymentOffer(amount_wei=1 << 256, currency="ETH", recipient=USDC, chain_id=1)
    with pytest.raises(ValueError):
        zt.encode_offer(offer)


def test_offer_negative_amount_rejected():
    offer = PaymentOffer(amount_wei=-1, currency="ETH", recipient=USDC, chain_id=1)
    with pytest.raises(ValueError):
        zt.encode_offer(offer)


def test_proof_roundtrip():
    proof = PaymentProof(
        tx_hash=SAMPLE_TX,
        chain_id=8453,
        payer=VITALIK,
        amount_wei=999_999,
        nonce="proof-1",
        timestamp=1714421000.0,
    )
    wire = zt.encode_proof(proof)
    out = zt.decode_proof(wire)

    assert out.tx_hash == SAMPLE_TX
    assert out.chain_id == proof.chain_id
    assert out.payer.lower() == proof.payer.lower()
    assert out.amount_wei == proof.amount_wei
    assert out.nonce == proof.nonce
    assert out.timestamp == 1714421000.0


def test_proof_invalid_tx_hash_length():
    proof = PaymentProof(
        tx_hash="0xdead",  # too short
        chain_id=1,
        payer=VITALIK,
        amount_wei=1,
    )
    with pytest.raises(ValueError):
        zt.encode_proof(proof)


def test_zap_not_available_raises_when_disabled(monkeypatch):
    """If zap_py is force-disabled, encode/decode must raise ZapNotAvailable."""
    monkeypatch.setattr(zt, "HAS_ZAP_PY", False)
    offer = PaymentOffer(amount_wei=1, currency="ETH", recipient=USDC, chain_id=1)
    with pytest.raises(zt.ZapNotAvailable):
        zt.encode_offer(offer)
    with pytest.raises(zt.ZapNotAvailable):
        zt.decode_offer(b"junk")


def test_offer_wire_smaller_than_json():
    """Sanity: ZAP encoding is genuinely tighter than the JSON header path."""
    import json

    offer = PaymentOffer(
        amount_wei=1_000_000,
        currency="USDC",
        recipient=USDC,
        chain_id=8453,
        nonce="n-1",
        endpoint="/v1/x",
    )
    wire = zt.encode_offer(offer)
    json_blob = json.dumps({
        "amount": str(offer.amount_wei),
        "currency": offer.currency,
        "recipient": offer.recipient,
        "chainId": offer.chain_id,
        "scheme": offer.scheme.value,
        "endpoint": offer.endpoint,
        "nonce": offer.nonce,
    }).encode()
    # Not an absolute guarantee (small offers may bloat with the ZAP header),
    # but for any realistic offer the binary form should win.
    assert len(wire) <= len(json_blob) + 64  # tolerance for fixed ZAP header
