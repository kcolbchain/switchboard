"""ZAP wire transport for switchboard payment flows.

Switchboard's existing transport encodes ``PaymentOffer`` / ``PaymentProof``
as JSON in HTTP headers. That is fine for HTTP/REST agents but expensive
for high-volume agent-to-agent traffic — every offer is parsed, allocated,
and copied. This module adds a binary alternative: encode an offer or a
proof as a `ZAP <https://github.com/luxfi/zap>`_ message so two agents
sitting on the same Lux network (port 9999) can exchange them without
HTTP, JSON, or per-call allocation.

The wire layout is a fixed ZAP struct schema declared up front, so any
ZAP-speaking language (Go via ``luxfi/zap`` upstream, Python via
``zap_py``) reads and writes the same bytes. Field offsets and total
struct size are pinned by tests against the canonical
``StructBuilder.build()`` output.

zap_py is an *optional* dependency. If it isn't installed,
``encode_offer`` / ``decode_offer`` raise ``ZapNotAvailable`` and callers
should fall back to the existing JSON path. Tests are skipped via
``pytest.importorskip`` so the suite stays green either way.

Install (until ``luxfi-zap`` is on PyPI)::

    pip install 'luxfi-zap @ git+https://github.com/luxfi/zap@main#subdirectory=python'

References
----------
- ``switchboard.x402_middleware.PaymentOffer`` / ``PaymentProof``
- luxfi/zap (Go reference) + ``python/zap_py`` (parity-tested Python port)
"""

from __future__ import annotations

from typing import Optional, Tuple

from .x402_middleware import PaymentOffer, PaymentProof, PaymentScheme

try:
    from zap_py import (
        ADDRESS_SIZE,
        Address,
        Builder,
        HASH_SIZE,
        Hash,
        StructBuilder,
        Type,
        address_from_hex,
        parse,
    )

    HAS_ZAP_PY = True
except ImportError:  # pragma: no cover — exercised by environment, not tests
    HAS_ZAP_PY = False


__all__ = [
    "HAS_ZAP_PY",
    "ZapNotAvailable",
    "OFFER_SCHEMA",
    "PROOF_SCHEMA",
    "encode_offer",
    "decode_offer",
    "encode_proof",
    "decode_proof",
]


class ZapNotAvailable(RuntimeError):
    """zap_py is not installed; ZAP transport is unavailable."""


# ─── Wire constants ──────────────────────────────────────────────────────────
#
# Both schemas use uint256 amount-as-bytes (32 LE bytes) so we don't truncate
# realistic on-chain values into a uint64 — the JSON path already accepts
# arbitrarily large `int`s and we want byte-for-byte interop with that.
_AMOUNT_BYTES = 32

# ``scheme`` is encoded as uint8. Order matches the wire intent, not the Python
# enum's iteration order — pin it here so a Go implementation can mirror it.
_SCHEME_TO_WIRE = {
    PaymentScheme.EXACT: 0,
    PaymentScheme.ESCROW: 1,
    PaymentScheme.STREAMING: 2,
}
_WIRE_TO_SCHEME = {v: k for k, v in _SCHEME_TO_WIRE.items()}


def _build_offer_schema():
    if not HAS_ZAP_PY:
        return None
    return (
        StructBuilder("SwitchboardPaymentOffer")
        .uint8("scheme")
        .uint64("chain_id")
        .uint64("expires_at")  # 0 sentinel = "no expiry"
        .address("recipient")
        .bytes("amount")  # 32-byte big-endian uint256
        .text("currency")
        .text("description")
        .text("endpoint")
        .text("nonce")
        .build()
    )


def _build_proof_schema():
    if not HAS_ZAP_PY:
        return None
    return (
        StructBuilder("SwitchboardPaymentProof")
        .uint64("chain_id")
        .uint64("timestamp")
        .address("payer")
        .hash("tx_hash")
        .bytes("amount")  # 32-byte big-endian uint256
        .text("nonce")
        .build()
    )


OFFER_SCHEMA = _build_offer_schema()
PROOF_SCHEMA = _build_proof_schema()


def _require_zap() -> None:
    if not HAS_ZAP_PY:
        raise ZapNotAvailable(
            "zap_py is not installed; install luxfi-zap (see switchboard/zap_transport.py)"
        )


def _amount_to_bytes(amount: int) -> bytes:
    if amount < 0:
        raise ValueError("amount must be non-negative")
    if amount.bit_length() > _AMOUNT_BYTES * 8:
        raise ValueError(f"amount exceeds uint{_AMOUNT_BYTES * 8}")
    return amount.to_bytes(_AMOUNT_BYTES, "big")


def _amount_from_bytes(data: bytes) -> int:
    if len(data) != _AMOUNT_BYTES:
        raise ValueError(f"amount field must be {_AMOUNT_BYTES} bytes, got {len(data)}")
    return int.from_bytes(data, "big")


def _addr_to_bytes(s: str) -> bytes:
    """Accept a 0x-prefixed hex address; return raw 20 bytes."""
    return address_from_hex(s).bytes


def _addr_to_hex(addr) -> str:
    return addr.hex()


# ─── PaymentOffer ────────────────────────────────────────────────────────────


def encode_offer(offer: PaymentOffer) -> bytes:
    """Serialize a PaymentOffer to a ZAP wire message (zero allocations on read)."""
    _require_zap()
    f = {fld.name: fld.offset for fld in OFFER_SCHEMA.fields}

    b = Builder()
    ob = b.start_object(OFFER_SCHEMA.size)
    ob.set_uint8(f["scheme"], _SCHEME_TO_WIRE[offer.scheme])
    ob.set_uint64(f["chain_id"], offer.chain_id)
    ob.set_uint64(f["expires_at"], offer.expires_at or 0)
    ob.set_address(f["recipient"], _addr_to_bytes(offer.recipient))
    ob.set_bytes(f["amount"], _amount_to_bytes(offer.amount_wei))
    ob.set_text(f["currency"], offer.currency)
    ob.set_text(f["description"], offer.description)
    ob.set_text(f["endpoint"], offer.endpoint)
    ob.set_text(f["nonce"], offer.nonce)
    ob.finish_as_root()
    return b.finish()


def decode_offer(wire: bytes) -> PaymentOffer:
    """Parse a ZAP wire message into a PaymentOffer."""
    _require_zap()
    f = {fld.name: fld.offset for fld in OFFER_SCHEMA.fields}

    msg = parse(wire)
    root = msg.root()

    expires = root.uint64(f["expires_at"])
    return PaymentOffer(
        amount_wei=_amount_from_bytes(root.bytes(f["amount"])),
        currency=root.text(f["currency"]),
        recipient=_addr_to_hex(root.address(f["recipient"])),
        chain_id=root.uint64(f["chain_id"]),
        scheme=_WIRE_TO_SCHEME[root.uint8(f["scheme"])],
        description=root.text(f["description"]),
        endpoint=root.text(f["endpoint"]),
        nonce=root.text(f["nonce"]),
        expires_at=int(expires) if expires else None,
    )


# ─── PaymentProof ────────────────────────────────────────────────────────────


def _hash_from_hex(s: str) -> bytes:
    if s.startswith("0x") or s.startswith("0X"):
        s = s[2:]
    raw = bytes.fromhex(s)
    if len(raw) != HASH_SIZE:
        raise ValueError(f"tx_hash must be {HASH_SIZE} bytes, got {len(raw)}")
    return raw


def encode_proof(proof: PaymentProof) -> bytes:
    """Serialize a PaymentProof to a ZAP wire message."""
    _require_zap()
    f = {fld.name: fld.offset for fld in PROOF_SCHEMA.fields}

    b = Builder()
    ob = b.start_object(PROOF_SCHEMA.size)
    ob.set_uint64(f["chain_id"], proof.chain_id)
    ob.set_uint64(f["timestamp"], int(proof.timestamp))
    ob.set_address(f["payer"], _addr_to_bytes(proof.payer))
    ob.set_hash(f["tx_hash"], _hash_from_hex(proof.tx_hash))
    ob.set_bytes(f["amount"], _amount_to_bytes(proof.amount_wei))
    ob.set_text(f["nonce"], proof.nonce)
    ob.finish_as_root()
    return b.finish()


def decode_proof(wire: bytes) -> PaymentProof:
    """Parse a ZAP wire message into a PaymentProof."""
    _require_zap()
    f = {fld.name: fld.offset for fld in PROOF_SCHEMA.fields}

    msg = parse(wire)
    root = msg.root()

    return PaymentProof(
        tx_hash=root.hash(f["tx_hash"]).hex(),
        chain_id=root.uint64(f["chain_id"]),
        payer=_addr_to_hex(root.address(f["payer"])),
        amount_wei=_amount_from_bytes(root.bytes(f["amount"])),
        nonce=root.text(f["nonce"]),
        timestamp=float(root.uint64(f["timestamp"])),
    )
