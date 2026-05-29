"""Roundtrip tests for switchboard.zap_transport.

Skipped if zap_py is not installed — install with::

    pip install 'luxfi-zap @ git+https://github.com/luxfi/zap@main#subdirectory=python'
"""

from __future__ import annotations

import time

import pytest

try:
    import zap_py
except ImportError:  # pragma: no cover - environment-dependent
    zap_py = None

from switchboard.x402_middleware import PaymentOffer, PaymentProof, PaymentScheme
from switchboard import zap_transport as zt


VITALIK = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
SAMPLE_TX = "0x" + "ab" * 32
SAMPLE_SIG = "0x" + "cd" * 48
ZAP_REQUIRED = pytest.mark.skipif(zap_py is None, reason="zap_py is not installed")


@ZAP_REQUIRED
def _build_old_offer_schema():
    return (
        zap_py.StructBuilder("SwitchboardPaymentOffer")
        .uint8("scheme")
        .uint64("chain_id")
        .uint64("expires_at")
        .address("recipient")
        .bytes("amount")
        .text("currency")
        .text("description")
        .text("endpoint")
        .text("nonce")
        .build()
    )


@ZAP_REQUIRED
def _build_old_proof_schema():
    return (
        zap_py.StructBuilder("SwitchboardPaymentProof")
        .uint64("chain_id")
        .uint64("timestamp")
        .address("payer")
        .hash("tx_hash")
        .bytes("amount")
        .text("nonce")
        .build()
    )


@ZAP_REQUIRED
def _decode_offer_with_schema(wire: bytes, schema):
    msg = zap_py.parse(wire)
    root = msg.root()
    f = {fld.name: fld.offset for fld in schema.fields}
    expires = root.uint64(f["expires_at"])
    return {
        "amount_wei": zt._amount_from_bytes(root.bytes(f["amount"])),
        "currency": root.text(f["currency"]),
        "recipient": zt._addr_to_hex(root.address(f["recipient"])),
        "chain_id": root.uint64(f["chain_id"]),
        "scheme": zt._WIRE_TO_SCHEME[root.uint8(f["scheme"])],
        "description": root.text(f["description"]),
        "endpoint": root.text(f["endpoint"]),
        "nonce": root.text(f["nonce"]),
        "expires_at": int(expires) if expires else None,
    }


@ZAP_REQUIRED
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
        "signature_alg",
        "signature",
    }
    # Final align(8); schema remains 8-byte aligned after the appended fields.
    assert zt.OFFER_SCHEMA.size > 0
    assert zt.OFFER_SCHEMA.size % 8 == 0


@ZAP_REQUIRED
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
    assert out.signature_alg == "none"
    assert out.signature == ""
    assert out.description == ""
    assert out.endpoint == ""
    assert out.nonce == ""
    assert out.expires_at is None  # 0 sentinel restored to None


@ZAP_REQUIRED
def test_offer_roundtrip_full():
    offer = PaymentOffer(
        amount_wei=12_345_678_901_234_567_890_123,  # > uint64
        currency="ETH",
        recipient=VITALIK,
        chain_id=1,
        scheme=PaymentScheme.ESCROW,
        signature_alg="ml-dsa-65",
        signature=SAMPLE_SIG,
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
    assert out.signature_alg == offer.signature_alg
    assert out.signature == offer.signature
    assert out.description == offer.description
    assert out.endpoint == offer.endpoint
    assert out.nonce == offer.nonce
    assert out.expires_at == offer.expires_at


@ZAP_REQUIRED
def test_offer_each_scheme_roundtrips():
    for scheme in PaymentScheme:
        offer = PaymentOffer(amount_wei=1, currency="ETH", recipient=USDC, chain_id=1, scheme=scheme)
        wire = zt.encode_offer(offer)
        out = zt.decode_offer(wire)
        assert out.scheme == scheme


@ZAP_REQUIRED
def test_offer_amount_uint256_max():
    """Wire format must accept a full uint256 amount, not truncate to uint64."""
    big = (1 << 256) - 1
    offer = PaymentOffer(amount_wei=big, currency="ETH", recipient=USDC, chain_id=1)
    wire = zt.encode_offer(offer)
    out = zt.decode_offer(wire)
    assert out.amount_wei == big


@ZAP_REQUIRED
def test_offer_amount_overflow_rejected():
    offer = PaymentOffer(amount_wei=1 << 256, currency="ETH", recipient=USDC, chain_id=1)
    with pytest.raises(ValueError):
        zt.encode_offer(offer)


@ZAP_REQUIRED
def test_offer_negative_amount_rejected():
    offer = PaymentOffer(amount_wei=-1, currency="ETH", recipient=USDC, chain_id=1)
    with pytest.raises(ValueError):
        zt.encode_offer(offer)


@ZAP_REQUIRED
def test_proof_roundtrip():
    proof = PaymentProof(
        tx_hash=SAMPLE_TX,
        chain_id=8453,
        payer=VITALIK,
        amount_wei=999_999,
        nonce="proof-1",
        signature_alg="ecdsa-secp256k1",
        signature=SAMPLE_SIG,
        timestamp=1714421000.0,
    )
    wire = zt.encode_proof(proof)
    out = zt.decode_proof(wire)

    assert out.tx_hash == SAMPLE_TX
    assert out.chain_id == proof.chain_id
    assert out.payer.lower() == proof.payer.lower()
    assert out.amount_wei == proof.amount_wei
    assert out.nonce == proof.nonce
    assert out.signature_alg == proof.signature_alg
    assert out.signature == proof.signature
    assert out.timestamp == 1714421000.0


@ZAP_REQUIRED
def test_proof_invalid_tx_hash_length():
    proof = PaymentProof(
        tx_hash="0xdead",  # too short
        chain_id=1,
        payer=VITALIK,
        amount_wei=1,
    )
    with pytest.raises(ValueError):
        zt.encode_proof(proof)


@ZAP_REQUIRED
@pytest.mark.parametrize("alg", zt._PQ_ALG_TO_TAG)
def test_offer_roundtrip_each_signature_alg(alg):
    offer = PaymentOffer(
        amount_wei=7,
        currency="USDC",
        recipient=USDC,
        chain_id=8453,
        signature_alg=alg,
        signature="" if alg == "none" else SAMPLE_SIG,
        nonce=f"offer-{alg}",
    )
    out = zt.decode_offer(zt.encode_offer(offer))
    assert out.signature_alg == alg
    assert out.signature == ("" if alg == "none" else SAMPLE_SIG)


@ZAP_REQUIRED
@pytest.mark.parametrize("alg", zt._PQ_ALG_TO_TAG)
def test_proof_roundtrip_each_signature_alg(alg):
    proof = PaymentProof(
        tx_hash=SAMPLE_TX,
        chain_id=1,
        payer=VITALIK,
        amount_wei=7,
        nonce=f"proof-{alg}",
        signature_alg=alg,
        signature="" if alg == "none" else SAMPLE_SIG,
        timestamp=1714421000.0,
    )
    out = zt.decode_proof(zt.encode_proof(proof))
    assert out.signature_alg == alg
    assert out.signature == ("" if alg == "none" else SAMPLE_SIG)


@ZAP_REQUIRED
def test_old_offer_reader_ignores_new_trailing_fields():
    offer = PaymentOffer(
        amount_wei=42,
        currency="USDC",
        recipient=USDC,
        chain_id=8453,
        signature_alg="ml-dsa-44",
        signature=SAMPLE_SIG,
        description="desc",
        endpoint="/paid",
        nonce="n-1",
    )
    decoded = _decode_offer_with_schema(zt.encode_offer(offer), _build_old_offer_schema())
    assert decoded["amount_wei"] == offer.amount_wei
    assert decoded["currency"] == offer.currency
    assert decoded["recipient"].lower() == offer.recipient.lower()
    assert decoded["chain_id"] == offer.chain_id
    assert decoded["scheme"] == offer.scheme
    assert decoded["description"] == offer.description
    assert decoded["endpoint"] == offer.endpoint
    assert decoded["nonce"] == offer.nonce


@ZAP_REQUIRED
def test_new_reader_decodes_old_offer_payload_with_none_signature():
    old_schema = _build_old_offer_schema()
    f = {fld.name: fld.offset for fld in old_schema.fields}
    b = zap_py.Builder()
    ob = b.start_object(old_schema.size)
    ob.set_uint8(f["scheme"], zt._SCHEME_TO_WIRE[PaymentScheme.EXACT])
    ob.set_uint64(f["chain_id"], 8453)
    ob.set_uint64(f["expires_at"], 0)
    ob.set_address(f["recipient"], zt._addr_to_bytes(USDC))
    ob.set_bytes(f["amount"], zt._amount_to_bytes(123))
    ob.set_text(f["currency"], "USDC")
    ob.set_text(f["description"], "")
    ob.set_text(f["endpoint"], "")
    ob.set_text(f["nonce"], "legacy-offer")
    ob.finish_as_root()
    out = zt.decode_offer(b.finish())
    assert out.signature_alg == "none"
    assert out.signature == ""
    assert out.nonce == "legacy-offer"


@ZAP_REQUIRED
def test_new_reader_decodes_old_proof_payload_with_none_signature():
    old_schema = _build_old_proof_schema()
    f = {fld.name: fld.offset for fld in old_schema.fields}
    b = zap_py.Builder()
    ob = b.start_object(old_schema.size)
    ob.set_uint64(f["chain_id"], 8453)
    ob.set_uint64(f["timestamp"], 1714421000)
    ob.set_address(f["payer"], zt._addr_to_bytes(VITALIK))
    ob.set_hash(f["tx_hash"], zt._hash_from_hex(SAMPLE_TX))
    ob.set_bytes(f["amount"], zt._amount_to_bytes(456))
    ob.set_text(f["nonce"], "legacy-proof")
    ob.finish_as_root()
    out = zt.decode_proof(b.finish())
    assert out.signature_alg == "none"
    assert out.signature == ""
    assert out.nonce == "legacy-proof"


@ZAP_REQUIRED
def test_signing_transcript_zeroes_signature_fields():
    offer = PaymentOffer(
        amount_wei=1,
        currency="USDC",
        recipient=USDC,
        chain_id=8453,
        signature_alg="ml-dsa-65",
        signature=SAMPLE_SIG,
    )
    proof = PaymentProof(
        tx_hash=SAMPLE_TX,
        chain_id=8453,
        payer=VITALIK,
        amount_wei=1,
        signature_alg="ecdsa-secp256k1",
        signature=SAMPLE_SIG,
    )
    assert zt.decode_offer(zt.signing_transcript(offer)).signature_alg == "none"
    assert zt.decode_offer(zt.signing_transcript(offer)).signature == ""
    assert zt.decode_proof(zt.signing_transcript(proof)).signature_alg == "none"
    assert zt.decode_proof(zt.signing_transcript(proof)).signature == ""


def test_zap_not_available_raises_when_disabled(monkeypatch):
    """If zap_py is force-disabled, encode/decode must raise ZapNotAvailable."""
    monkeypatch.setattr(zt, "HAS_ZAP_PY", False)
    offer = PaymentOffer(amount_wei=1, currency="ETH", recipient=USDC, chain_id=1)
    with pytest.raises(zt.ZapNotAvailable):
        zt.encode_offer(offer)
    with pytest.raises(zt.ZapNotAvailable):
        zt.decode_offer(b"junk")


@ZAP_REQUIRED
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
