"""Roundtrip tests for switchboard.zap_transport.

Skipped if zap_py is not installed — install with::

    pip install 'luxfi-zap @ git+https://github.com/luxfi/zap@main#subdirectory=python'
"""

from __future__ import annotations

import json
import time
from pathlib import Path

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


def test_nested_tag_policy_rejects_reserved_tags():
    with pytest.raises(zt.ReservedNestedTag, match="RESERVED_NESTED_TAG"):
        zt._validate_nested_tag(0x03)
    with pytest.raises(zt.ReservedNestedTag, match="RESERVED_NESTED_TAG"):
        zt._validate_nested_tag(0xFF)


def test_zap_nested_conformance_vectors_cover_required_tags():
    vectors = json.loads(
        Path("tests/protocol_vectors/zap_nested.v1.json").read_text(encoding="utf-8")
    )
    assert vectors["schema"] == "switchboard/zap-nested/v1"
    cases = {case["name"]: case for case in vectors["cases"]}
    assert set(cases) == {
        "no-nesting",
        "x402-nested",
        "warp-reserved-for-consumers",
        "reserved-tag-rejected",
    }
    assert cases["no-nesting"]["nested_tag"] == zt.NESTED_NONE
    assert cases["x402-nested"]["nested_tag"] == zt.NESTED_X402
    assert cases["warp-reserved-for-consumers"]["nested_tag"] == zt.NESTED_WARP
    assert cases["reserved-tag-rejected"]["expected"] == "RESERVED_NESTED_TAG"


@ZAP_REQUIRED
def test_generic_frame_no_nesting_roundtrip():
    wire = zt.encode(
        zt.ZAP_FRAME_VERSION,
        PaymentScheme.EXACT,
        b"\x00" * 32,
        b"outer",
        nested_tag=zt.NESTED_NONE,
    )

    assert zt.decode(wire) == (
        zt.ZAP_FRAME_VERSION,
        PaymentScheme.EXACT,
        "0x" + "00" * 32,
        b"outer",
        zt.NESTED_NONE,
        b"",
    )


@ZAP_REQUIRED
def test_generic_frame_x402_nested_payload_roundtrips_against_parser():
    x402_payload = json.dumps({
        "amount": "1000",
        "currency": "USDC",
        "recipient": USDC,
        "chainId": 8453,
        "scheme": "exact",
        "nonce": "nested-1",
    }, separators=(",", ":")).encode()

    wire = zt.encode(
        zt.ZAP_FRAME_VERSION,
        PaymentScheme.EXACT,
        "11" * 32,
        b"warp",
        nested_tag=zt.NESTED_X402,
        nested_payload=x402_payload,
    )
    version, scheme, header_digest, payload, nested_tag, nested_payload = zt.decode(wire)
    nested_offer = PaymentOffer.from_header(nested_payload.decode(), endpoint="/zap")

    assert version == zt.ZAP_FRAME_VERSION
    assert scheme == PaymentScheme.EXACT
    assert header_digest == "0x" + "11" * 32
    assert payload == b"warp"
    assert nested_tag == zt.NESTED_X402
    assert nested_offer.amount_wei == 1000
    assert nested_offer.recipient.lower() == USDC.lower()
    assert nested_offer.endpoint == "/zap"


@ZAP_REQUIRED
def test_generic_frame_accepts_reserved_warp_tag_two_without_recursive_decode():
    wire = zt.encode(
        zt.ZAP_FRAME_VERSION,
        PaymentScheme.ESCROW,
        b"\x22" * 32,
        b"outer",
        nested_tag=zt.NESTED_WARP,
        nested_payload=b"\x01\x02\x03",
    )

    assert zt.decode(wire) == (
        zt.ZAP_FRAME_VERSION,
        PaymentScheme.ESCROW,
        "0x" + "22" * 32,
        b"outer",
        zt.NESTED_WARP,
        b"\x01\x02\x03",
    )


@ZAP_REQUIRED
def test_generic_frame_rejects_reserved_nested_tag_on_encode():
    with pytest.raises(zt.ReservedNestedTag, match="RESERVED_NESTED_TAG"):
        zt.encode(
            zt.ZAP_FRAME_VERSION,
            PaymentScheme.STREAMING,
            b"\x33" * 32,
            b"bad",
            nested_tag=0x03,
            nested_payload=b"\x00",
        )


@ZAP_REQUIRED
def test_generic_frame_rejects_reserved_nested_tag_on_decode():
    f = {fld.name: fld.offset for fld in zt.FRAME_SCHEMA.fields}
    b = zap_py.Builder()
    ob = b.start_object(zt.FRAME_SCHEMA.size)
    ob.set_uint8(f["version"], zt.ZAP_FRAME_VERSION)
    ob.set_uint8(f["scheme"], zt._SCHEME_TO_WIRE[PaymentScheme.STREAMING])
    ob.set_uint8(f["nested_tag"], 0x03)
    ob.set_hash(f["header_digest"], b"\x33" * 32)
    ob.set_bytes(f["payload"], b"bad")
    ob.set_bytes(f["nested_payload"], b"\x00")
    ob.finish_as_root()

    with pytest.raises(zt.ReservedNestedTag, match="RESERVED_NESTED_TAG"):
        zt.decode(b.finish())


@ZAP_REQUIRED
def test_generic_frame_rejects_nested_payload_without_tag():
    with pytest.raises(ValueError, match="nested_payload"):
        zt.encode(
            zt.ZAP_FRAME_VERSION,
            PaymentScheme.EXACT,
            b"\x44" * 32,
            b"outer",
            nested_tag=zt.NESTED_NONE,
            nested_payload=b"unexpected",
        )


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


# ───────────────────────────────────────────────────────────────────────────
# ZAP wire v1.0 session layer (issue #85)
#
# Pure-Python, no zap_py dependency. These tests are NOT gated by ZAP_REQUIRED:
# the session framing and state machine must work with the standard library.
# See docs/zap-wire-spec-v1.0.md.
# ───────────────────────────────────────────────────────────────────────────


class FakeClock:
    """Deterministic injectable clock for retransmit-timeout tests."""

    def __init__(self, start: float = 0.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ─── Frame encode/decode ─────────────────────────────────────────────────────


def test_session_frame_header_is_26_bytes():
    wire = zt.encode_session_frame(zt.SessionFrame(frame_type=zt.FRAME_DATA, seq=0))
    assert len(wire) == zt.SESSION_HEADER_SIZE == 26


def test_session_frame_roundtrip_all_fields():
    frame = zt.SessionFrame(
        frame_type=zt.FRAME_DATA,
        flags=zt.FLAG_ACK_PRESENT | zt.FLAG_REQ,
        seq=7,
        ack=3,
        request_id=0xDEADBEEFCAFE,
        payload=b"hello-zap",
    )
    wire = zt.encode_session_frame(frame)
    out = zt.decode_session_frame(wire)
    assert out.frame_type == zt.FRAME_DATA
    assert out.flags == zt.FLAG_ACK_PRESENT | zt.FLAG_REQ
    assert out.seq == 7
    assert out.ack == 3
    assert out.request_id == 0xDEADBEEFCAFE
    assert out.payload == b"hello-zap"


def test_session_frame_starts_with_magic_and_version():
    wire = zt.encode_session_frame(zt.SessionFrame(frame_type=zt.FRAME_HELLO, seq=0))
    assert wire[0:2] == zt.WIRE_MAGIC.to_bytes(2, "big")
    assert wire[2] == zt.WIRE_VERSION


def test_session_frame_decode_rejects_bad_magic():
    wire = bytearray(zt.encode_session_frame(zt.SessionFrame(frame_type=zt.FRAME_DATA, seq=0)))
    wire[0] ^= 0xFF
    with pytest.raises(zt.SessionProtocolError):
        zt.decode_session_frame(bytes(wire))


def test_session_frame_decode_rejects_unknown_version():
    wire = bytearray(zt.encode_session_frame(zt.SessionFrame(frame_type=zt.FRAME_DATA, seq=0)))
    wire[2] = 0x99
    with pytest.raises(zt.SessionProtocolError):
        zt.decode_session_frame(bytes(wire))


# ─── Capability bitmask + negotiation ───────────────────────────────────────


def test_capability_bitmask_layout_is_8_version_24_flags():
    caps = zt.make_capabilities(zt.WIRE_VERSION, zt.CAP_ACK | zt.CAP_FIN)
    assert zt.capability_version(caps) == zt.WIRE_VERSION
    assert zt.capability_flags(caps) == (zt.CAP_ACK | zt.CAP_FIN)
    # version lives in the high 8 bits
    assert caps >> 24 == zt.WIRE_VERSION
    # flags live in the low 24 bits
    assert caps & 0x00FFFFFF == (zt.CAP_ACK | zt.CAP_FIN)


def test_capability_baseline_mask_is_0x0100001f():
    assert zt.CAP_BASELINE == 0x0100001F


def test_negotiate_intersects_flags_and_min_version():
    local = zt.make_capabilities(2, zt.CAP_ACK | zt.CAP_RETRY | zt.CAP_FIN)
    remote = zt.make_capabilities(1, zt.CAP_ACK | zt.CAP_IDEMPOTENT | zt.CAP_FIN)
    negotiated = zt.negotiate_capabilities(local, remote)
    assert zt.capability_version(negotiated) == 1  # min(2, 1)
    assert zt.capability_flags(negotiated) == (zt.CAP_ACK | zt.CAP_FIN)  # intersection


def test_negotiate_no_common_version_raises():
    local = zt.make_capabilities(0, zt.CAP_ACK)
    remote = zt.make_capabilities(1, zt.CAP_ACK)
    with pytest.raises(zt.SessionVersionError):
        zt.negotiate_capabilities(local, remote)


# ─── Handshake roundtrip + capability negotiation ───────────────────────────


def test_handshake_roundtrip_and_capability_negotiation():
    initiator = zt.ZapSession(capabilities=zt.make_capabilities(zt.WIRE_VERSION, zt.CAP_ACK | zt.CAP_FIN))
    responder = zt.ZapSession(capabilities=zt.make_capabilities(zt.WIRE_VERSION, zt.CAP_ACK | zt.CAP_RETRY))

    hello = initiator.connect(session_id=0x1234)
    decoded_hello = zt.decode_session_frame(hello)
    assert decoded_hello.frame_type == zt.FRAME_HELLO
    assert decoded_hello.seq == 0

    welcome = responder.accept(hello)
    decoded_welcome = zt.decode_session_frame(welcome)
    assert decoded_welcome.frame_type == zt.FRAME_WELCOME

    initiator.process(welcome)

    assert initiator.state == zt.SessionState.ESTABLISHED
    assert responder.state == zt.SessionState.ESTABLISHED
    # negotiated = intersection of the two flag sets
    assert zt.capability_flags(initiator.negotiated) == zt.CAP_ACK
    assert zt.capability_flags(responder.negotiated) == zt.CAP_ACK


def test_accept_rejects_data_before_handshake():
    responder = zt.ZapSession()
    data = zt.encode_session_frame(zt.SessionFrame(frame_type=zt.FRAME_DATA, seq=0, payload=b"x"))
    with pytest.raises(zt.SessionProtocolError):
        responder.process(data)


def test_welcome_session_id_mismatch_raises():
    initiator = zt.ZapSession()
    initiator.connect(session_id=0xAAAA)
    # A WELCOME echoing the wrong session id must be rejected (spec §4.3).
    bad = zt.encode_session_frame(
        zt.SessionFrame(
            frame_type=zt.FRAME_WELCOME,
            seq=0,
            flags=zt.FLAG_ACK_PRESENT,
            payload=zt.encode_capabilities_payload(zt.CAP_BASELINE, session_id=0x9999),
        )
    )
    with pytest.raises(zt.SessionProtocolError):
        initiator.process(bad)


# ─── In-order / out-of-order sequence handling + ACK ─────────────────────────


def _establish_pair():
    initiator = zt.ZapSession(capabilities=zt.CAP_BASELINE)
    responder = zt.ZapSession(capabilities=zt.CAP_BASELINE)
    hello = initiator.connect(session_id=0x55)
    welcome = responder.accept(hello)
    initiator.process(welcome)
    return initiator, responder


def test_in_order_data_delivered_and_acked():
    initiator, responder = _establish_pair()
    f1 = initiator.send_data(b"one")
    f2 = initiator.send_data(b"two")

    r1 = responder.process(f1)
    r2 = responder.process(f2)

    assert r1.delivered == [b"one"]
    assert r2.delivered == [b"two"]
    # cumulative ack: responder has delivered up to seq 2 (hello=0, one=1, two=2)
    assert responder.ack_seq == 2


def test_out_of_order_data_buffered_then_drained():
    initiator, responder = _establish_pair()
    f1 = initiator.send_data(b"one")  # seq 1
    f2 = initiator.send_data(b"two")  # seq 2

    # deliver f2 before f1
    r_future = responder.process(f2)
    assert r_future.delivered == []  # buffered, nothing delivered yet
    assert responder.ack_seq == 0  # still only handshake is contiguous

    r_gap = responder.process(f1)
    # now both drain in order
    assert r_gap.delivered == [b"one", b"two"]
    assert responder.ack_seq == 2


def test_duplicate_past_frame_dropped_but_reacked():
    initiator, responder = _establish_pair()
    f1 = initiator.send_data(b"one")
    responder.process(f1)
    r_dup = responder.process(f1)  # same seq again
    assert r_dup.delivered == []  # not redelivered
    assert r_dup.should_ack is True  # but we re-ack
    assert responder.ack_seq == 1


def test_ack_retires_retransmit_queue():
    initiator, responder = _establish_pair()
    f1 = initiator.send_data(b"one")  # seq 1
    f2 = initiator.send_data(b"two")  # seq 2
    assert initiator.unacked_seqs() == [1, 2]

    # Deliver both in order so the responder can cumulatively ack up to seq 2.
    responder.process(f1)
    ack_frame = responder.process(f2)
    assert ack_frame.should_ack
    assert responder.ack_seq == 2

    initiator.process(responder.make_ack())  # ACK = 2 retires seq 1 and 2
    assert initiator.unacked_seqs() == []


# ─── Retry after timeout ─────────────────────────────────────────────────────


def test_no_retransmit_before_rto():
    clock = FakeClock()
    initiator = zt.ZapSession(capabilities=zt.CAP_BASELINE, rtt=0.2, now=clock)
    responder = zt.ZapSession(capabilities=zt.CAP_BASELINE, now=clock)
    initiator.process(responder.accept(initiator.connect(session_id=1)))

    initiator.send_data(b"lossy")  # seq 1, sent at t=0
    clock.advance(0.1)  # < rtt
    assert initiator.due_retransmissions() == []


def test_retransmit_after_rto_sets_retransmit_flag():
    clock = FakeClock()
    initiator = zt.ZapSession(capabilities=zt.CAP_BASELINE, rtt=0.2, now=clock)
    responder = zt.ZapSession(capabilities=zt.CAP_BASELINE, now=clock)
    initiator.process(responder.accept(initiator.connect(session_id=1)))

    initiator.send_data(b"lossy")  # seq 1
    clock.advance(0.25)  # > rtt
    due = initiator.due_retransmissions()
    assert len(due) == 1
    rt = zt.decode_session_frame(due[0])
    assert rt.seq == 1
    assert rt.flags & zt.FLAG_RETRANSMIT
    assert rt.payload == b"lossy"


def test_retransmit_backoff_then_declared_lost():
    clock = FakeClock()
    initiator = zt.ZapSession(
        capabilities=zt.CAP_BASELINE, rtt=0.2, rto_multiplier=2.0, max_retries=2, now=clock
    )
    responder = zt.ZapSession(capabilities=zt.CAP_BASELINE, now=clock)
    initiator.process(responder.accept(initiator.connect(session_id=1)))

    initiator.send_data(b"lossy")  # seq 1
    # attempt 0 RTO = 0.2; attempt 1 RTO = 0.4
    clock.advance(0.25)
    assert len(initiator.due_retransmissions()) == 1  # retransmit #1
    clock.advance(0.45)
    assert len(initiator.due_retransmissions()) == 1  # retransmit #2 (== max_retries)
    clock.advance(1.0)
    # max_retries exhausted -> frame declared lost
    with pytest.raises(zt.SessionTimeout):
        initiator.due_retransmissions()


# ─── Idempotent request_id dedup ─────────────────────────────────────────────


def test_idempotent_request_deduped_on_retransmit():
    initiator, responder = _establish_pair()
    req = initiator.send_data(b"charge", request_id=0xABCD)
    r1 = responder.process(req)
    assert r1.delivered == [b"charge"]

    # retransmit of the same request (same seq + request_id)
    r2 = responder.process(req)
    assert r2.delivered == []  # NOT re-executed
    assert r2.duplicate is True
    assert r2.should_ack is True


def test_distinct_request_ids_both_delivered():
    initiator, responder = _establish_pair()
    r1 = responder.process(initiator.send_data(b"a", request_id=1))
    r2 = responder.process(initiator.send_data(b"b", request_id=2))
    assert r1.delivered == [b"a"]
    assert r2.delivered == [b"b"]


# ─── Clean FIN with in-flight frames ─────────────────────────────────────────


def test_clean_fin_with_in_flight_frames():
    initiator, responder = _establish_pair()
    data = initiator.send_data(b"last")  # seq 1, in-flight (unacked)
    fin = initiator.close()  # seq 2 FIN
    assert zt.decode_session_frame(fin).frame_type == zt.FRAME_FIN
    assert initiator.state == zt.SessionState.CLOSING
    # in-flight data is NOT cancelled by FIN
    assert 1 in initiator.unacked_seqs()

    # responder receives the in-flight data, then the FIN, in order
    responder.process(data)
    fin_result = responder.process(fin)
    assert fin_result.fin is True
    assert responder.state == zt.SessionState.CLOSING

    # responder half-closes back; its FIN piggybacks an ack covering the
    # initiator's in-flight DATA + FIN, so the initiator drains and CLOSES.
    responder_fin = responder.close()
    initiator.process(responder_fin)
    assert initiator.unacked_seqs() == []  # in-flight DATA+FIN now acked
    assert initiator.state == zt.SessionState.CLOSED


def test_fin_with_unfillable_gap_is_incomplete():
    initiator, responder = _establish_pair()
    initiator.send_data(b"one")          # seq 1 — will be "lost" (never delivered)
    initiator.send_data(b"two")          # seq 2
    fin = initiator.close()              # seq 3 FIN

    # responder only ever sees seq 2 and the FIN, never seq 1
    responder.process(zt.encode_session_frame(
        zt.SessionFrame(frame_type=zt.FRAME_DATA, seq=2, payload=b"two")
    ))
    with pytest.raises(zt.SessionIncomplete):
        responder.process(fin)


# ─── Conformance vectors ─────────────────────────────────────────────────────


def test_session_conformance_vectors_decode_to_expected_fields():
    vectors = json.loads(
        Path("tests/protocol_vectors/zap_session.v1.json").read_text(encoding="utf-8")
    )
    assert vectors["schema"] == "switchboard/zap-session/v1"
    cases = {c["name"]: c for c in vectors["cases"]}
    assert {"hello", "welcome", "data-request", "ack", "fin", "rst"} <= set(cases)

    for case in vectors["cases"]:
        wire = bytes.fromhex(case["wire_hex"])
        frame = zt.decode_session_frame(wire)
        assert frame.frame_type == case["frame_type"]
        assert frame.seq == case["seq"]
        assert frame.flags == case["flags"]
        if "request_id" in case:
            assert frame.request_id == case["request_id"]
        if "payload_hex" in case:
            assert frame.payload == bytes.fromhex(case["payload_hex"])
        # re-encoding must reproduce the exact bytes (deterministic codec)
        assert zt.encode_session_frame(frame) == wire


def test_session_conformance_capability_vector_matches_negotiation():
    vectors = json.loads(
        Path("tests/protocol_vectors/zap_session.v1.json").read_text(encoding="utf-8")
    )
    cap = vectors["capability_negotiation"]
    local = int(cap["local"], 16)
    remote = int(cap["remote"], 16)
    expected = int(cap["negotiated"], 16)
    assert zt.negotiate_capabilities(local, remote) == expected
