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

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum

from .x402_middleware import PaymentOffer, PaymentProof, PaymentScheme

try:
    from zap_py import Builder, HASH_SIZE, StructBuilder, address_from_hex, parse

    HAS_ZAP_PY = True
except ImportError:  # pragma: no cover — exercised by environment, not tests
    HAS_ZAP_PY = False


__all__ = [
    "HAS_ZAP_PY",
    "ZapNotAvailable",
    "ReservedNestedTag",
    "ZAP_FRAME_VERSION",
    "NESTED_NONE",
    "NESTED_X402",
    "NESTED_WARP",
    "FRAME_SCHEMA",
    "OFFER_SCHEMA",
    "PROOF_SCHEMA",
    "encode",
    "decode",
    "encode_frame",
    "decode_frame",
    "encode_offer",
    "decode_offer",
    "encode_proof",
    "decode_proof",
    "signing_transcript",
    # ── ZAP wire v1.0 session layer (issue #85) ──
    "WIRE_MAGIC",
    "WIRE_VERSION",
    "SESSION_HEADER_SIZE",
    "FRAME_HELLO",
    "FRAME_WELCOME",
    "FRAME_DATA",
    "FRAME_ACK",
    "FRAME_FIN",
    "FRAME_RST",
    "FLAG_ACK_PRESENT",
    "FLAG_REQ",
    "FLAG_RETRANSMIT",
    "CAP_VERSION_SHIFT",
    "CAP_ACK",
    "CAP_RETRY",
    "CAP_IDEMPOTENT",
    "CAP_FIN",
    "CAP_NESTED",
    "CAP_BASELINE",
    "ERR_PROTOCOL",
    "ERR_VERSION",
    "ERR_TIMEOUT",
    "ERR_FLOW",
    "ERR_TOO_LARGE",
    "ERR_INCOMPLETE",
    "ERR_RESET",
    "SessionFrame",
    "SessionState",
    "ReceiveResult",
    "SessionError",
    "SessionProtocolError",
    "SessionVersionError",
    "SessionTimeout",
    "SessionIncomplete",
    "SessionReset",
    "encode_session_frame",
    "decode_session_frame",
    "encode_capabilities_payload",
    "decode_capabilities_payload",
    "make_capabilities",
    "capability_version",
    "capability_flags",
    "negotiate_capabilities",
    "ZapSession",
]


class ZapNotAvailable(RuntimeError):
    """zap_py is not installed; ZAP transport is unavailable."""


# ─── Wire constants ──────────────────────────────────────────────────────────
#
# Both schemas use uint256 amount-as-bytes (32 big-endian bytes) so we don't truncate
# realistic on-chain values into a uint64 — the JSON path already accepts
# arbitrarily large `int`s and we want byte-for-byte interop with that.
class ReservedNestedTag(ValueError):
    """A ZAP frame used a reserved nested payload tag."""


_AMOUNT_BYTES = 32
ZAP_FRAME_VERSION = 1
NESTED_NONE = 0x00
NESTED_X402 = 0x01
NESTED_WARP = 0x02
_SUPPORTED_NESTED_TAGS = {NESTED_NONE, NESTED_X402, NESTED_WARP}

# ``scheme`` is encoded as uint8. Order matches the wire intent, not the Python
# enum's iteration order — pin it here so a Go implementation can mirror it.
_SCHEME_TO_WIRE = {
    PaymentScheme.EXACT: 0,
    PaymentScheme.ESCROW: 1,
    PaymentScheme.STREAMING: 2,
}
_WIRE_TO_SCHEME = {v: k for k, v in _SCHEME_TO_WIRE.items()}

_PQ_ALG_TO_TAG = {
    "none": 0x00,
    "ecdsa-secp256k1": 0x01,
    "ml-dsa-44": 0x10,
    "ml-dsa-65": 0x11,
    "ml-dsa-87": 0x12,
    "slh-dsa-128s": 0x20,
    "slh-dsa-128f": 0x21,
    "hybrid-ecdsa-ml-dsa-65": 0x80,
}
_TAG_TO_PQ_ALG = {v: k for k, v in _PQ_ALG_TO_TAG.items()}


def _build_frame_schema():
    if not HAS_ZAP_PY:
        return None
    return (
        StructBuilder("SwitchboardZapFrame")
        .uint8("version")
        .uint8("scheme")
        .uint8("nested_tag")
        .hash("header_digest")
        .bytes("payload")
        .bytes("nested_payload")
        .build()
    )


def _build_offer_schema():
    if not HAS_ZAP_PY:
        return None
    return (
        StructBuilder("SwitchboardPaymentOffer")
        # Fixed/header offsets (pinned by tests against StructBuilder output):
        # | field         | type    | offset |
        # |---------------|---------|--------|
        # | scheme        | uint8   | 0      |
        # | chain_id      | uint64  | 8      |
        # | expires_at    | uint64  | 16     |
        # | recipient     | address | 24     |
        # | amount        | bytes   | 48     |
        # | currency      | text    | 56     |
        # | description   | text    | 64     |
        # | endpoint      | text    | 72     |
        # | nonce         | text    | 80     |
        # | signature_alg | uint8   | 88     |
        # | signature     | bytes   | 96     |
        .uint8("scheme")
        .uint64("chain_id")
        .uint64("expires_at")  # 0 sentinel = "no expiry"
        .address("recipient")
        .bytes("amount")  # 32-byte big-endian uint256
        .text("currency")
        .text("description")
        .text("endpoint")
        .text("nonce")
        .uint8("signature_alg")
        .bytes("signature")
        .build()
    )


def _build_proof_schema():
    if not HAS_ZAP_PY:
        return None
    return (
        StructBuilder("SwitchboardPaymentProof")
        # Fixed/header offsets (pinned by tests against StructBuilder output):
        # | field         | type    | offset |
        # |---------------|---------|--------|
        # | chain_id      | uint64  | 0      |
        # | timestamp     | uint64  | 8      |
        # | payer         | address | 16     |
        # | tx_hash       | hash    | 40     |
        # | amount        | bytes   | 72     |
        # | nonce         | text    | 80     |
        # | signature_alg | uint8   | 88     |
        # | signature     | bytes   | 96     |
        .uint64("chain_id")
        .uint64("timestamp")
        .address("payer")
        .hash("tx_hash")
        .bytes("amount")  # 32-byte big-endian uint256
        .text("nonce")
        .uint8("signature_alg")
        .bytes("signature")
        .build()
    )


FRAME_SCHEMA = _build_frame_schema()
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


def _sig_to_bytes(signature_alg: str, signature: str) -> bytes:
    if signature_alg == "none":
        return b""
    if not signature:
        raise ValueError("signature must be present when signature_alg != 'none'")
    if signature.startswith(("0x", "0X")):
        return bytes.fromhex(signature[2:])
    return signature.encode()


def _sig_from_bytes(data: bytes) -> str:
    return "0x" + data.hex() if data else ""


def _get_offset(schema, name: str) -> int:
    for field in schema.fields:
        if field.name == name:
            return field.offset
    raise KeyError(name)


def _read_optional_uint8(root, schema, name: str, default: int) -> int:
    try:
        return root.uint8(_get_offset(schema, name))
    except Exception:
        return default


def _read_optional_bytes(root, schema, name: str, default: bytes = b"") -> bytes:
    try:
        return root.bytes(_get_offset(schema, name))
    except Exception:
        return default


def signing_transcript(payload: PaymentOffer | PaymentProof) -> bytes:
    """Return the canonical ZAP transcript bytes with signature fields zeroed.

    Spec §11 requires the wire `signature_alg` and `signature` fields to be
    zeroed before hashing. We produce the canonical wire bytes here and let the
    caller choose the hash function.
    """
    if isinstance(payload, PaymentOffer):
        return encode_offer(replace(payload, signature_alg="none", signature=""))
    return encode_proof(replace(payload, signature_alg="none", signature=""))


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
    ob.set_uint8(f["signature_alg"], _PQ_ALG_TO_TAG[offer.signature_alg])
    ob.set_bytes(f["signature"], _sig_to_bytes(offer.signature_alg, offer.signature))
    ob.finish_as_root()
    return b.finish()


def decode_offer(wire: bytes) -> PaymentOffer:
    """Parse a ZAP wire message into a PaymentOffer."""
    _require_zap()
    f = {fld.name: fld.offset for fld in OFFER_SCHEMA.fields}

    msg = parse(wire)
    root = msg.root()

    expires = root.uint64(f["expires_at"])
    signature_alg_tag = _read_optional_uint8(root, OFFER_SCHEMA, "signature_alg", 0x00)
    signature_bytes = _read_optional_bytes(root, OFFER_SCHEMA, "signature", b"")
    signature_alg = _TAG_TO_PQ_ALG[signature_alg_tag]
    return PaymentOffer(
        amount_wei=_amount_from_bytes(root.bytes(f["amount"])),
        currency=root.text(f["currency"]),
        recipient=_addr_to_hex(root.address(f["recipient"])),
        chain_id=root.uint64(f["chain_id"]),
        scheme=_WIRE_TO_SCHEME[root.uint8(f["scheme"])],
        signature_alg=signature_alg,
        signature=_sig_from_bytes(signature_bytes),
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


def _coerce_digest(header_digest: bytes | str) -> bytes:
    if isinstance(header_digest, str):
        if header_digest.startswith(("0x", "0X")):
            header_digest = header_digest[2:]
        raw = bytes.fromhex(header_digest)
    else:
        raw = bytes(header_digest)
    if len(raw) != HASH_SIZE:
        raise ValueError(f"header_digest must be {HASH_SIZE} bytes, got {len(raw)}")
    return raw


def _validate_nested_tag(nested_tag: int) -> None:
    if nested_tag not in _SUPPORTED_NESTED_TAGS:
        raise ReservedNestedTag(f"RESERVED_NESTED_TAG: 0x{nested_tag:02x}")


def encode(
    version: int,
    scheme: PaymentScheme | int,
    header_digest: bytes | str,
    payload: bytes,
    *,
    nested_tag: int = NESTED_NONE,
    nested_payload: bytes = b"",
) -> bytes:
    """Serialize a generic Switchboard ZAP frame with optional nested payload."""
    _require_zap()
    _validate_nested_tag(nested_tag)
    if nested_tag == NESTED_NONE and nested_payload:
        raise ValueError("nested_payload must be empty when nested_tag is 0x00")

    f = {fld.name: fld.offset for fld in FRAME_SCHEMA.fields}
    scheme_tag = _SCHEME_TO_WIRE[scheme] if isinstance(scheme, PaymentScheme) else int(scheme)

    b = Builder()
    ob = b.start_object(FRAME_SCHEMA.size)
    ob.set_uint8(f["version"], version)
    ob.set_uint8(f["scheme"], scheme_tag)
    ob.set_uint8(f["nested_tag"], nested_tag)
    ob.set_hash(f["header_digest"], _coerce_digest(header_digest))
    ob.set_bytes(f["payload"], bytes(payload))
    ob.set_bytes(f["nested_payload"], bytes(nested_payload))
    ob.finish_as_root()
    return b.finish()


def decode(wire: bytes) -> tuple[int, PaymentScheme, str, bytes, int, bytes]:
    """Decode `(version, scheme, header_digest, payload, nested_tag, nested_payload)`."""
    _require_zap()
    f = {fld.name: fld.offset for fld in FRAME_SCHEMA.fields}

    msg = parse(wire)
    root = msg.root()
    nested_tag = root.uint8(f["nested_tag"])
    _validate_nested_tag(nested_tag)

    return (
        root.uint8(f["version"]),
        _WIRE_TO_SCHEME[root.uint8(f["scheme"])],
        root.hash(f["header_digest"]).hex(),
        root.bytes(f["payload"]),
        nested_tag,
        root.bytes(f["nested_payload"]),
    )


encode_frame = encode
decode_frame = decode


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
    ob.set_uint8(f["signature_alg"], _PQ_ALG_TO_TAG[proof.signature_alg])
    ob.set_bytes(f["signature"], _sig_to_bytes(proof.signature_alg, proof.signature))
    ob.finish_as_root()
    return b.finish()


def decode_proof(wire: bytes) -> PaymentProof:
    """Parse a ZAP wire message into a PaymentProof."""
    _require_zap()
    f = {fld.name: fld.offset for fld in PROOF_SCHEMA.fields}

    msg = parse(wire)
    root = msg.root()

    signature_alg_tag = _read_optional_uint8(root, PROOF_SCHEMA, "signature_alg", 0x00)
    signature_bytes = _read_optional_bytes(root, PROOF_SCHEMA, "signature", b"")
    return PaymentProof(
        tx_hash=root.hash(f["tx_hash"]).hex(),
        chain_id=root.uint64(f["chain_id"]),
        payer=_addr_to_hex(root.address(f["payer"])),
        amount_wei=_amount_from_bytes(root.bytes(f["amount"])),
        nonce=root.text(f["nonce"]),
        signature_alg=_TAG_TO_PQ_ALG[signature_alg_tag],
        signature=_sig_from_bytes(signature_bytes),
        timestamp=float(root.uint64(f["timestamp"])),
    )


# ═══════════════════════════════════════════════════════════════════════════
# ZAP wire v1.0 session layer (issue #85)
#
# A pure-Python, dependency-free session/framing layer that sits UNDER the
# zap_py payload codecs above. A session carries opaque application payloads
# (typically an encoded PaymentOffer/PaymentProof). The session layer never
# inspects payload bytes. See docs/zap-wire-spec-v1.0.md for the byte format,
# handshake, sequencing, ACK, retry, idempotency, and FIN semantics.
#
# Everything below uses only the standard library so the state machine works
# whether or not zap_py is installed.
# ═══════════════════════════════════════════════════════════════════════════

# ─── Wire constants (spec §3) ────────────────────────────────────────────────

WIRE_MAGIC = 0x5A50  # "ZP"
WIRE_VERSION = 0x01
SESSION_HEADER_SIZE = 26

# Frame types (spec §3.1)
FRAME_HELLO = 0x01
FRAME_WELCOME = 0x02
FRAME_DATA = 0x03
FRAME_ACK = 0x04
FRAME_FIN = 0x05
FRAME_RST = 0x06
_KNOWN_FRAME_TYPES = {
    FRAME_HELLO,
    FRAME_WELCOME,
    FRAME_DATA,
    FRAME_ACK,
    FRAME_FIN,
    FRAME_RST,
}
# Frame types that consume sequence space (spec §5).
_SEQ_CONSUMING = {FRAME_HELLO, FRAME_WELCOME, FRAME_DATA, FRAME_FIN}

# FLAGS bitfield (spec §3.2)
FLAG_ACK_PRESENT = 0x01
FLAG_REQ = 0x02
FLAG_RETRANSMIT = 0x04

# Capability bitmask: 8 bits version (high octet) + 24 bits feature flags (spec §4.1)
CAP_VERSION_SHIFT = 24
_CAP_FLAGS_MASK = 0x00FFFFFF
CAP_ACK = 0x000001
CAP_RETRY = 0x000002
CAP_IDEMPOTENT = 0x000004
CAP_FIN = 0x000008
CAP_NESTED = 0x000010
# v1.0 baseline: version 1 + all five defined flags = 0x0100001F.
CAP_BASELINE = (WIRE_VERSION << CAP_VERSION_SHIFT) | (
    CAP_ACK | CAP_RETRY | CAP_IDEMPOTENT | CAP_FIN | CAP_NESTED
)

# RST error codes (spec §10)
ERR_PROTOCOL = 0x01
ERR_VERSION = 0x02
ERR_TIMEOUT = 0x03
ERR_FLOW = 0x04
ERR_TOO_LARGE = 0x05
ERR_INCOMPLETE = 0x06
ERR_RESET = 0x07

# Tunable bounds (spec §2/§5.1/§8)
MAX_PAYLOAD = 16 * 1024 * 1024
MAX_REORDER = 256
MAX_REQUEST_IDS = 4096


class SessionError(RuntimeError):
    """Base class for ZAP session-layer errors."""


class SessionProtocolError(SessionError):
    """Malformed frame or illegal state transition (ERR_PROTOCOL)."""


class SessionVersionError(SessionError):
    """No common protocol version during handshake (ERR_VERSION)."""


class SessionTimeout(SessionError):
    """A frame exceeded max_retries without an ack (ERR_TIMEOUT)."""


class SessionIncomplete(SessionError):
    """FIN reached with an unfillable sequence gap (ERR_INCOMPLETE)."""


class SessionReset(SessionError):
    """Peer reset the session (ERR_RESET)."""


class SessionState(Enum):
    CLOSED = "closed"
    HELLO_SENT = "hello_sent"
    WELCOME_SENT = "welcome_sent"
    ESTABLISHED = "established"
    CLOSING = "closing"


# ─── Frame encode/decode (spec §3) ──────────────────────────────────────────


@dataclass
class SessionFrame:
    """A single ZAP session frame (26-byte header + opaque payload)."""

    frame_type: int
    seq: int = 0
    ack: int = 0
    flags: int = 0
    request_id: int = 0
    payload: bytes = b""


def encode_session_frame(frame: SessionFrame) -> bytes:
    """Serialize a SessionFrame to its canonical big-endian wire bytes."""
    payload = bytes(frame.payload)
    if len(payload) > MAX_PAYLOAD:
        raise SessionProtocolError(f"payload exceeds MAX_PAYLOAD ({len(payload)} bytes)")
    if frame.frame_type not in _KNOWN_FRAME_TYPES:
        raise SessionProtocolError(f"unknown frame_type 0x{frame.frame_type:02x}")
    header = b"".join(
        (
            WIRE_MAGIC.to_bytes(2, "big"),
            bytes((WIRE_VERSION, frame.frame_type, frame.flags & 0xFF, 0x00)),
            (frame.seq & 0xFFFFFFFF).to_bytes(4, "big"),
            (frame.ack & 0xFFFFFFFF).to_bytes(4, "big"),
            (frame.request_id & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "big"),
            len(payload).to_bytes(4, "big"),
        )
    )
    return header + payload


def decode_session_frame(wire: bytes) -> SessionFrame:
    """Parse canonical wire bytes into a SessionFrame.

    Rejects bad magic / unknown wire version before any other field is
    interpreted (spec §4.3).
    """
    if len(wire) < SESSION_HEADER_SIZE:
        raise SessionProtocolError("frame shorter than 26-byte header")
    if int.from_bytes(wire[0:2], "big") != WIRE_MAGIC:
        raise SessionProtocolError("bad MAGIC")
    if wire[2] != WIRE_VERSION:
        raise SessionProtocolError(f"unknown WIRE_VERSION 0x{wire[2]:02x}")
    frame_type = wire[3]
    if frame_type not in _KNOWN_FRAME_TYPES:
        raise SessionProtocolError(f"unknown frame_type 0x{frame_type:02x}")
    flags = wire[4]
    seq = int.from_bytes(wire[6:10], "big")
    ack = int.from_bytes(wire[10:14], "big")
    request_id = int.from_bytes(wire[14:22], "big")
    payload_len = int.from_bytes(wire[22:26], "big")
    payload = wire[SESSION_HEADER_SIZE : SESSION_HEADER_SIZE + payload_len]
    if len(payload) != payload_len:
        raise SessionProtocolError("truncated payload")
    return SessionFrame(
        frame_type=frame_type,
        seq=seq,
        ack=ack,
        flags=flags,
        request_id=request_id,
        payload=payload,
    )


# ─── Capability bitmask (spec §4.1/§4.2) ─────────────────────────────────────


def make_capabilities(version: int, flags: int) -> int:
    """Pack (version, flags) into the u32 capability bitmask."""
    if not 0 <= version <= 0xFF:
        raise ValueError("version must fit in 8 bits")
    if flags & ~_CAP_FLAGS_MASK:
        raise ValueError("flags must fit in 24 bits")
    return (version << CAP_VERSION_SHIFT) | (flags & _CAP_FLAGS_MASK)


def capability_version(caps: int) -> int:
    return (caps >> CAP_VERSION_SHIFT) & 0xFF


def capability_flags(caps: int) -> int:
    return caps & _CAP_FLAGS_MASK


def negotiate_capabilities(local: int, remote: int) -> int:
    """Negotiate min(version) and the intersection of feature flags (spec §4.3)."""
    version = min(capability_version(local), capability_version(remote))
    if version == 0:
        raise SessionVersionError("no common protocol version")
    flags = capability_flags(local) & capability_flags(remote)
    return make_capabilities(version, flags)


def encode_capabilities_payload(caps: int, session_id: int) -> bytes:
    """Encode the 12-byte HELLO/WELCOME payload (capabilities + session id)."""
    return (caps & 0xFFFFFFFF).to_bytes(4, "big") + (
        session_id & 0xFFFFFFFFFFFFFFFF
    ).to_bytes(8, "big")


def decode_capabilities_payload(payload: bytes) -> tuple[int, int]:
    """Decode a HELLO/WELCOME payload into (capabilities, session_id)."""
    if len(payload) != 12:
        raise SessionProtocolError("handshake payload must be 12 bytes")
    return int.from_bytes(payload[0:4], "big"), int.from_bytes(payload[4:12], "big")


def _seq_is_after(a: int, b: int) -> bool:
    """Unsigned mod-2^32 'is-after' comparison (spec §5)."""
    return 0 < ((a - b) & 0xFFFFFFFF) < 0x80000000


# ─── Receive result ──────────────────────────────────────────────────────────


@dataclass
class ReceiveResult:
    """Outcome of feeding one inbound frame to a ZapSession.process()."""

    delivered: list[bytes] = field(default_factory=list)
    should_ack: bool = False
    duplicate: bool = False
    fin: bool = False


@dataclass
class _Outstanding:
    """An unacked sent frame tracked on the retransmit queue (spec §7)."""

    seq: int
    wire: bytes
    sent_at: float
    attempts: int = 0


# ─── Session state machine ───────────────────────────────────────────────────


class ZapSession:
    """A pure-Python ZAP wire v1.0 session endpoint.

    Drive it by feeding inbound wire bytes to ``process()`` and transmitting
    the wire bytes returned from ``connect()`` / ``accept()`` / ``send_data()``
    / ``make_ack()`` / ``close()`` / ``due_retransmissions()``. The session is
    transport-agnostic: it never touches a socket.
    """

    def __init__(
        self,
        capabilities: int = CAP_BASELINE,
        *,
        rtt: float = 0.2,
        rto_multiplier: float = 2.0,
        max_retries: int = 5,
        max_rto: float = 30.0,
        now: Callable[[], float] | None = None,
    ):
        self.capabilities = capabilities
        self.negotiated = capabilities
        self.rtt = rtt
        self.rto_multiplier = rto_multiplier
        self.max_retries = max_retries
        self.max_rto = max_rto
        self._now = now or time.monotonic
        self.state = SessionState.CLOSED
        self.session_id = 0

        # Send side.
        self._send_seq = 0
        self._outstanding: dict[int, _Outstanding] = {}

        # Receive side.
        self.expected_seq = 0  # next in-order SEQ we will accept
        self.ack_seq = 0  # highest contiguous SEQ delivered (cumulative ack)
        self._reorder: dict[int, SessionFrame] = {}
        self._seen_requests: dict[int, None] = {}
        self._peer_fin_seq: int | None = None
        self._sent_fin = False

    # ── helpers ──

    def _next_seq(self) -> int:
        seq = self._send_seq
        self._send_seq = (self._send_seq + 1) & 0xFFFFFFFF
        return seq

    @property
    def _retry_enabled(self) -> bool:
        return bool(capability_flags(self.negotiated) & CAP_RETRY)

    def _emit(self, frame: SessionFrame, *, track: bool) -> bytes:
        """Encode a frame, piggyback the current ack, and (optionally) track it."""
        frame.flags |= FLAG_ACK_PRESENT
        frame.ack = self.ack_seq
        wire = encode_session_frame(frame)
        if track and self._retry_enabled:
            self._outstanding[frame.seq] = _Outstanding(
                seq=frame.seq, wire=wire, sent_at=self._now()
            )
        return wire

    # ── handshake (spec §4) ──

    def connect(self, session_id: int = 1) -> bytes:
        """Initiator: build the HELLO frame (SEQ 0) and enter HELLO_SENT."""
        self.session_id = session_id & 0xFFFFFFFFFFFFFFFF
        self.state = SessionState.HELLO_SENT
        self._send_seq = 0
        frame = SessionFrame(
            frame_type=FRAME_HELLO,
            seq=self._next_seq(),
            payload=encode_capabilities_payload(self.capabilities, self.session_id),
        )
        return self._emit(frame, track=True)

    def accept(self, hello_wire: bytes) -> bytes:
        """Responder: consume a HELLO, negotiate, build WELCOME, enter ESTABLISHED."""
        hello = decode_session_frame(hello_wire)
        if hello.frame_type != FRAME_HELLO:
            raise SessionProtocolError("expected HELLO")
        peer_caps, session_id = decode_capabilities_payload(hello.payload)
        self.negotiated = negotiate_capabilities(self.capabilities, peer_caps)
        self.session_id = session_id
        # Receive side: the HELLO occupied SEQ 0; we have it in order.
        self.expected_seq = (hello.seq + 1) & 0xFFFFFFFF
        self.ack_seq = hello.seq
        self._send_seq = 0
        welcome = SessionFrame(
            frame_type=FRAME_WELCOME,
            seq=self._next_seq(),
            payload=encode_capabilities_payload(self.negotiated, self.session_id),
        )
        self.state = SessionState.ESTABLISHED
        return self._emit(welcome, track=True)

    # ── sending (spec §3/§5/§8) ──

    def send_data(self, payload: bytes, *, request_id: int = 0) -> bytes:
        """Send a DATA frame; set request_id != 0 for an idempotent request."""
        if self.state not in (SessionState.ESTABLISHED, SessionState.WELCOME_SENT):
            raise SessionProtocolError(f"cannot send DATA in state {self.state}")
        flags = FLAG_REQ if request_id else 0
        frame = SessionFrame(
            frame_type=FRAME_DATA,
            seq=self._next_seq(),
            flags=flags,
            request_id=request_id,
            payload=bytes(payload),
        )
        return self._emit(frame, track=True)

    def make_ack(self) -> bytes:
        """Build a standalone cumulative ACK frame (does not consume SEQ)."""
        frame = SessionFrame(frame_type=FRAME_ACK, seq=self._send_seq)
        return self._emit(frame, track=False)

    def close(self) -> bytes:
        """Send a FIN (consumes one SEQ) and enter CLOSING (spec §9)."""
        if self.state == SessionState.CLOSED:
            raise SessionProtocolError("session already closed")
        frame = SessionFrame(frame_type=FRAME_FIN, seq=self._next_seq())
        wire = self._emit(frame, track=True)
        self._sent_fin = True
        self._maybe_close()
        if self.state != SessionState.CLOSED:
            self.state = SessionState.CLOSING
        return wire

    # ── receiving (spec §5.1/§6/§8/§9) ──

    def process(self, wire: bytes) -> ReceiveResult:
        """Feed one inbound frame; returns delivered payloads + ack signal."""
        frame = decode_session_frame(wire)

        if frame.flags & FLAG_ACK_PRESENT:
            self._apply_ack(frame.ack)

        if frame.frame_type == FRAME_WELCOME:
            return self._on_welcome(frame)
        if frame.frame_type == FRAME_ACK:
            return ReceiveResult()
        if frame.frame_type == FRAME_RST:
            self.state = SessionState.CLOSED
            raise SessionReset("peer reset the session")
        if frame.frame_type in (FRAME_DATA, FRAME_FIN):
            return self._on_sequenced(frame)
        if frame.frame_type == FRAME_HELLO:
            raise SessionProtocolError("unexpected HELLO on established session")
        raise SessionProtocolError(f"unexpected frame_type 0x{frame.frame_type:02x}")

    def _on_welcome(self, frame: SessionFrame) -> ReceiveResult:
        if self.state != SessionState.HELLO_SENT:
            raise SessionProtocolError("WELCOME outside handshake")
        caps, session_id = decode_capabilities_payload(frame.payload)
        if session_id != self.session_id:
            raise SessionProtocolError("WELCOME session_id mismatch")
        self.negotiated = caps
        self.expected_seq = (frame.seq + 1) & 0xFFFFFFFF
        self.ack_seq = frame.seq
        self.state = SessionState.ESTABLISHED
        return ReceiveResult(should_ack=True)

    def _on_sequenced(self, frame: SessionFrame) -> ReceiveResult:
        if self.state not in (SessionState.ESTABLISHED, SessionState.CLOSING):
            raise SessionProtocolError(f"data/fin in state {self.state}")

        # Duplicate / already-delivered past frame: drop payload, still re-ack.
        if frame.seq == self.expected_seq:
            pass  # in order, handled below
        elif _seq_is_after(self.expected_seq, frame.seq):
            return ReceiveResult(duplicate=True, should_ack=True)
        elif _seq_is_after(frame.seq, self.expected_seq):
            # A FIN signals the peer will originate no new frames, so a gap
            # below it can never be filled — the session is incomplete (§9.1).
            if frame.frame_type == FRAME_FIN:
                self._peer_fin_seq = frame.seq
                raise SessionIncomplete(
                    f"FIN at seq {frame.seq} with unfilled gap at {self.expected_seq}"
                )
            # Future / out-of-order DATA: buffer and re-ack the last in-order SEQ.
            if len(self._reorder) >= MAX_REORDER:
                raise SessionProtocolError("reorder buffer overflow")
            self._reorder[frame.seq] = frame
            return ReceiveResult(should_ack=True)

        result = ReceiveResult(should_ack=True)
        self._accept_in_order(frame, result)
        # Drain any buffered contiguous successors.
        while self.expected_seq in self._reorder:
            nxt = self._reorder.pop(self.expected_seq)
            self._accept_in_order(nxt, result)
        return result

    def _accept_in_order(self, frame: SessionFrame, result: ReceiveResult) -> None:
        if frame.frame_type == FRAME_FIN:
            self._peer_fin_seq = frame.seq
            self.ack_seq = frame.seq
            self.expected_seq = (frame.seq + 1) & 0xFFFFFFFF
            result.fin = True
            if self.state == SessionState.ESTABLISHED:
                self.state = SessionState.CLOSING
            self._maybe_close()
            return

        # DATA frame: idempotent dedup on request_id (spec §8).
        if frame.flags & FLAG_REQ and frame.request_id:
            if frame.request_id in self._seen_requests:
                result.duplicate = True
                self.ack_seq = frame.seq
                self.expected_seq = (frame.seq + 1) & 0xFFFFFFFF
                return
            self._seen_requests[frame.request_id] = None
            if len(self._seen_requests) > MAX_REQUEST_IDS:
                # FIFO eviction (dict preserves insertion order).
                oldest = next(iter(self._seen_requests))
                del self._seen_requests[oldest]

        result.delivered.append(frame.payload)
        self.ack_seq = frame.seq
        self.expected_seq = (frame.seq + 1) & 0xFFFFFFFF

    def _apply_ack(self, ack: int) -> None:
        """Retire every outstanding frame with SEQ <= ack (cumulative)."""
        for seq in list(self._outstanding):
            if seq == ack or _seq_is_after(ack, seq):
                del self._outstanding[seq]
        self._maybe_close()

    def _maybe_close(self) -> None:
        """CLOSED once both FINs are exchanged and all our frames are acked."""
        if (
            self._sent_fin
            and self._peer_fin_seq is not None
            and not self._outstanding
        ):
            self.state = SessionState.CLOSED

    # ── retransmit (spec §7) ──

    def unacked_seqs(self) -> list[int]:
        """SEQs still awaiting a cumulative ack, in order."""
        return sorted(self._outstanding)

    def due_retransmissions(self) -> list[bytes]:
        """Retransmit frames whose RTO has elapsed; raise on exhausted retries."""
        if not self._retry_enabled:
            return []
        now = self._now()
        out: list[bytes] = []
        for seq in sorted(self._outstanding):
            item = self._outstanding[seq]
            rto = min(self.rtt * (self.rto_multiplier**item.attempts), self.max_rto)
            if now - item.sent_at < rto:
                continue
            if item.attempts >= self.max_retries:
                raise SessionTimeout(f"seq {seq} exceeded max_retries")
            # Re-mark the wire bytes with the RETRANSMIT flag.
            frame = decode_session_frame(item.wire)
            frame.flags |= FLAG_RETRANSMIT
            frame.ack = self.ack_seq
            retx = encode_session_frame(frame)
            item.wire = retx
            item.attempts += 1
            item.sent_at = now
            out.append(retx)
        return out
