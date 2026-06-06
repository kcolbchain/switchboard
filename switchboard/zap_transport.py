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

import struct
from dataclasses import dataclass, field, replace
from enum import IntEnum, IntFlag

from .x402_middleware import PaymentOffer, PaymentProof, PaymentScheme

try:
    from zap_py import Builder, HASH_SIZE, StructBuilder, address_from_hex, parse

    HAS_ZAP_PY = True
except ImportError:  # pragma: no cover — exercised by environment, not tests
    HAS_ZAP_PY = False


__all__ = [
    "HAS_ZAP_PY",
    "ZapNotAvailable",
    "OFFER_SCHEMA",
    "PROOF_SCHEMA",
    "ZAP_WIRE_MAGIC",
    "ZAP_WIRE_VERSION",
    "ZapRetryPolicy",
    "ZapWireCapability",
    "ZapWireFrame",
    "ZapWireFrameType",
    "ZapWireReceiveResult",
    "ZapWireSession",
    "ZapWireSessionError",
    "encode_offer",
    "decode_offer",
    "encode_proof",
    "decode_proof",
    "signing_transcript",
]


class ZapNotAvailable(RuntimeError):
    """zap_py is not installed; ZAP transport is unavailable."""


class ZapWireSessionError(ValueError):
    """Raised when a ZAP v1.0 session frame violates the wire contract."""


# ─── Wire constants ──────────────────────────────────────────────────────────
#
# Both schemas use uint256 amount-as-bytes (32 big-endian bytes) so we don't truncate
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

ZAP_WIRE_MAGIC = b"ZAP!"
ZAP_WIRE_VERSION = 1
_ZAP_WIRE_HEADER = struct.Struct(">4sBBHQI")
_ACK_ECHO = struct.Struct(">I")
_MAX_PAYLOAD_LEN = 16 * 1024


class ZapWireFrameType(IntEnum):
    """Connection-level frame types for the ZAP v1.0 session layer."""

    HELLO = 0x01
    HELLO_ACK = 0x02
    DATA = 0x03
    ACK = 0x04
    FIN = 0x05
    ERROR = 0x06


class ZapWireCapability(IntFlag):
    """Capability bits negotiated during the ZAP v1.0 handshake."""

    NONE = 0
    PQ_ENVELOPE = 1 << 0
    NESTED_X402 = 1 << 1
    MPP_SESSION = 1 << 2


_KNOWN_CAPABILITIES = (
    int(ZapWireCapability.PQ_ENVELOPE)
    | int(ZapWireCapability.NESTED_X402)
    | int(ZapWireCapability.MPP_SESSION)
)


def _check_u64(name: str, value: int) -> None:
    if value < 0 or value > 0xFFFFFFFFFFFFFFFF:
        raise ZapWireSessionError(f"{name} must fit uint64")


def _check_u16(name: str, value: int) -> None:
    if value < 0 or value > 0xFFFF:
        raise ZapWireSessionError(f"{name} must fit uint16")


def _check_payload(payload: bytes) -> None:
    if len(payload) > _MAX_PAYLOAD_LEN:
        raise ZapWireSessionError("ZAP v1.0 frame payload exceeds 16 KiB")


@dataclass(frozen=True)
class ZapWireFrame:
    """A ZAP v1.0 connection-level frame.

    The frame wraps existing ZAP payload bytes and adds session semantics:
    magic/version negotiation, explicit sequence numbers, ACK/FIN control
    frames, and a negotiated capability bitmask.
    """

    frame_type: ZapWireFrameType
    seq: int = 0
    payload: bytes = b""
    capabilities: ZapWireCapability = ZapWireCapability.NONE
    version: int = ZAP_WIRE_VERSION

    def encode(self) -> bytes:
        _check_u64("seq", self.seq)
        _check_u16("capabilities", int(self.capabilities))
        _check_payload(self.payload)
        header = _ZAP_WIRE_HEADER.pack(
            ZAP_WIRE_MAGIC,
            self.version,
            int(self.frame_type),
            int(self.capabilities),
            self.seq,
            len(self.payload),
        )
        return header + self.payload

    @classmethod
    def decode(cls, wire: bytes) -> "ZapWireFrame":
        if len(wire) < _ZAP_WIRE_HEADER.size:
            raise ZapWireSessionError("ZAP v1.0 frame is shorter than the header")
        magic, version, frame_type, capabilities, seq, payload_len = _ZAP_WIRE_HEADER.unpack(
            wire[: _ZAP_WIRE_HEADER.size]
        )
        if magic != ZAP_WIRE_MAGIC:
            raise ZapWireSessionError("invalid ZAP v1.0 magic")
        if version != ZAP_WIRE_VERSION:
            raise ZapWireSessionError(f"unsupported ZAP v1.0 version {version}")
        if payload_len > _MAX_PAYLOAD_LEN:
            raise ZapWireSessionError("ZAP v1.0 frame payload exceeds 16 KiB")
        expected_len = _ZAP_WIRE_HEADER.size + payload_len
        if len(wire) != expected_len:
            raise ZapWireSessionError(
                f"ZAP v1.0 frame length mismatch: expected {expected_len}, got {len(wire)}"
            )
        if capabilities & ~_KNOWN_CAPABILITIES:
            raise ZapWireSessionError("unknown ZAP v1.0 capability bit")
        try:
            parsed_type = ZapWireFrameType(frame_type)
            parsed_capabilities = ZapWireCapability(capabilities)
        except ValueError as exc:
            raise ZapWireSessionError("unknown ZAP v1.0 frame type or capability") from exc
        return cls(
            frame_type=parsed_type,
            seq=seq,
            payload=wire[_ZAP_WIRE_HEADER.size :],
            capabilities=parsed_capabilities,
            version=version,
        )

    @classmethod
    def hello(cls, capabilities: ZapWireCapability, seq: int = 0) -> "ZapWireFrame":
        return cls(ZapWireFrameType.HELLO, seq=seq, capabilities=capabilities)

    @classmethod
    def hello_ack(cls, capabilities: ZapWireCapability, seq: int = 0) -> "ZapWireFrame":
        return cls(ZapWireFrameType.HELLO_ACK, seq=seq, capabilities=capabilities)

    @classmethod
    def ack(cls, acked_seq: int, seq: int = 0) -> "ZapWireFrame":
        if acked_seq < 0 or acked_seq > 0xFFFFFFFF:
            raise ZapWireSessionError("ACK echo must fit uint32")
        return cls(ZapWireFrameType.ACK, seq=seq, payload=_ACK_ECHO.pack(acked_seq))

    def acked_seq(self) -> int:
        if self.frame_type != ZapWireFrameType.ACK or len(self.payload) != _ACK_ECHO.size:
            raise ZapWireSessionError("frame is not a valid ZAP v1.0 ACK")
        return _ACK_ECHO.unpack(self.payload)[0]


@dataclass(frozen=True)
class ZapRetryPolicy:
    """Retry timing for unacknowledged ZAP v1.0 DATA frames."""

    initial_timeout_ms: int = 200
    max_timeout_ms: int = 5_000
    max_attempts: int = 5

    def timeout_ms_for_attempt(self, attempt: int) -> int:
        if attempt < 0:
            raise ValueError("attempt must be non-negative")
        return min(self.max_timeout_ms, self.initial_timeout_ms * (2**attempt))


@dataclass
class _PendingZapFrame:
    frame: ZapWireFrame
    request_id: str | None = None
    attempts: int = 0


@dataclass(frozen=True)
class ZapWireReceiveResult:
    """Result of accepting or rejecting an inbound DATA frame."""

    status: str
    ack: ZapWireFrame | None = None
    payload: bytes = b""
    request_id: str | None = None


@dataclass
class ZapWireSession:
    """Minimal state machine for ZAP v1.0 peer-to-peer session semantics."""

    capabilities: ZapWireCapability = (
        ZapWireCapability.PQ_ENVELOPE
        | ZapWireCapability.NESTED_X402
        | ZapWireCapability.MPP_SESSION
    )
    retry_policy: ZapRetryPolicy = field(default_factory=ZapRetryPolicy)
    next_seq: int = 1
    highest_seen_seq: int = 0
    peer_capabilities: ZapWireCapability = ZapWireCapability.NONE
    closed: bool = False
    _pending: dict[int, _PendingZapFrame] = field(default_factory=dict)
    _seen_request_ids: set[str] = field(default_factory=set)

    def make_hello(self) -> ZapWireFrame:
        return ZapWireFrame.hello(self.capabilities)

    def accept_hello(self, frame: ZapWireFrame) -> ZapWireFrame:
        if frame.frame_type not in {ZapWireFrameType.HELLO, ZapWireFrameType.HELLO_ACK}:
            raise ZapWireSessionError("expected HELLO or HELLO_ACK frame")
        self.peer_capabilities = self.capabilities & frame.capabilities
        if frame.frame_type == ZapWireFrameType.HELLO:
            return ZapWireFrame.hello_ack(self.peer_capabilities)
        return ZapWireFrame.ack(frame.seq)

    def make_data(self, payload: bytes, request_id: str | None = None) -> ZapWireFrame:
        if self.closed:
            raise ZapWireSessionError("cannot send DATA on a closed ZAP session")
        seq = self.next_seq
        self.next_seq += 1
        frame = ZapWireFrame(ZapWireFrameType.DATA, seq=seq, payload=payload)
        self._pending[seq] = _PendingZapFrame(frame=frame, request_id=request_id)
        return frame

    def receive_data(
        self, frame: ZapWireFrame, request_id: str | None = None
    ) -> ZapWireReceiveResult:
        if frame.frame_type != ZapWireFrameType.DATA:
            raise ZapWireSessionError("expected DATA frame")
        if frame.seq <= self.highest_seen_seq or (
            request_id is not None and request_id in self._seen_request_ids
        ):
            return ZapWireReceiveResult(
                status="duplicate",
                ack=ZapWireFrame.ack(frame.seq),
                payload=frame.payload,
                request_id=request_id,
            )
        self.highest_seen_seq = frame.seq
        if request_id is not None:
            self._seen_request_ids.add(request_id)
        return ZapWireReceiveResult(
            status="accepted",
            ack=ZapWireFrame.ack(frame.seq),
            payload=frame.payload,
            request_id=request_id,
        )

    def receive_ack(self, frame: ZapWireFrame) -> int:
        acked_seq = frame.acked_seq()
        self._pending.pop(acked_seq, None)
        return acked_seq

    def next_retry(self, seq: int) -> tuple[ZapWireFrame, int] | None:
        pending = self._pending.get(seq)
        if pending is None:
            return None
        if pending.attempts >= self.retry_policy.max_attempts:
            self._pending.pop(seq, None)
            raise ZapWireSessionError(f"ZAP frame {seq} exceeded retry attempts")
        timeout_ms = self.retry_policy.timeout_ms_for_attempt(pending.attempts)
        pending.attempts += 1
        return pending.frame, timeout_ms

    def make_fin(self) -> ZapWireFrame:
        self.closed = True
        seq = self.next_seq
        self.next_seq += 1
        return ZapWireFrame(ZapWireFrameType.FIN, seq=seq)

    def receive_fin(self, frame: ZapWireFrame) -> list[int]:
        if frame.frame_type != ZapWireFrameType.FIN:
            raise ZapWireSessionError("expected FIN frame")
        self.closed = True
        return sorted(self._pending)


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
