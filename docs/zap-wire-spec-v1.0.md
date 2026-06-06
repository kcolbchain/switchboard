# ZAP Wire v1.0

Status: v1.0 reference contract for switchboard peer-to-peer ZAP sessions.

This document defines the connection-level semantics that sit around the
existing ZAP `PaymentOffer` and `PaymentProof` payload codecs in
`switchboard/zap_transport.py`. The payload codec still owns the payment schema.
ZAP Wire v1.0 owns session open, sequencing, ACK, retry, idempotency, and close.

## Goals

- Make session startup deterministic across Python and Go implementations.
- Add explicit sequence numbers so dropped frames and duplicates are observable.
- Keep the wire envelope small and independent from `zap_py`, HTTP, JSON, or a
  specific transport such as TCP, QUIC, or a message queue.
- Preserve the existing ZAP payload bytes unchanged inside DATA frames.

## Frame Layout

All multi-byte integers are big-endian.

| Offset | Size | Field | Notes |
| --- | ---: | --- | --- |
| 0 | 4 | magic | ASCII `ZAP!` |
| 4 | 1 | version | `0x01` for this spec |
| 5 | 1 | frame_type | See frame type table |
| 6 | 2 | capabilities | Negotiated bitmask on HELLO/HELLO_ACK; zero otherwise |
| 8 | 8 | seq | Unsigned sequence number |
| 16 | 4 | payload_len | Length in bytes, maximum 16 KiB |
| 20 | N | payload | Empty for HELLO/HELLO_ACK/FIN unless noted |

Frame type values:

| Value | Name | Payload |
| ---: | --- | --- |
| `0x01` | `HELLO` | Empty |
| `0x02` | `HELLO_ACK` | Empty |
| `0x03` | `DATA` | Existing ZAP payload bytes |
| `0x04` | `ACK` | Four-byte big-endian sequence echo |
| `0x05` | `FIN` | Empty |
| `0x06` | `ERROR` | UTF-8 diagnostic string |

Capability bits:

| Bit | Name | Meaning |
| ---: | --- | --- |
| `0x0001` | `PQ_ENVELOPE` | Payment payloads may carry post-quantum signature fields |
| `0x0002` | `NESTED_X402` | DATA payloads may carry nested x402 envelopes |
| `0x0004` | `MPP_SESSION` | Peers may use multi-party payment session metadata |

## Handshake

The initiator sends a `HELLO` frame with `seq = 0` and its supported capability
bitmask. The responder intersects those bits with its local supported bitmask
and returns `HELLO_ACK` with `seq = 0`.

If either peer receives an unsupported version, invalid magic, unknown frame
type, unknown capability, or oversized payload, it must reject the session before
processing the payload.

## Sequence Semantics

DATA frames use monotonically increasing unsigned 64-bit sequence numbers,
starting at `1` for the first application payload. Receivers track the highest
accepted sequence number for the session:

- `seq > highest_seen_seq`: accept the frame, update `highest_seen_seq`, and ACK.
- `seq <= highest_seen_seq`: treat as duplicate or stale, do not reprocess the
  payload, and ACK the received sequence.

The ACK frame payload is a four-byte big-endian echo of the accepted or duplicate
sequence. v1.0 therefore requires live ACKed sequences to fit `uint32`; higher
sequence ranges are reserved for a future v1.1 ACK extension.

## Retry Policy

Senders retain each unacknowledged DATA frame until an ACK arrives or retry
attempts are exhausted. The reference policy is:

- first retry timeout: 200 ms;
- timeout for attempt `n`: `min(5000 ms, 200 ms * 2^n)`;
- maximum retry attempts: 5;
- after the final failed retry, drop the session and surface the unacknowledged
  sequence to the application for resubmit.

The timer source is intentionally transport-specific. The reference Python state
machine returns the next frame and delay but does not sleep.

## Idempotency

Every `PaymentOffer` should carry an application request id. In the current
Python model this is the `PaymentOffer.nonce` field. Receivers maintain a
per-session set of request ids:

- first occurrence: process normally and ACK;
- duplicate request id: do not process the payment twice, but ACK the frame so
  the sender can stop retrying.

This rule is independent from the sequence check. A retry can arrive with the
same sequence and same request id; a transport replay can arrive with a new
sequence but an already-seen request id.

## Close

Either side may send `FIN` to close the session. On receiving `FIN`, the peer
marks the session closed and surfaces the sorted list of still-unacknowledged
outbound sequence numbers. The application can resubmit those payloads on a new
session if they are still valid and idempotent.

## Reference Vectors

The reference conformance vectors live in
`tests/conformance/zap_wire_v1_vectors.json`. Implementations in Go or any other
language should decode those exact bytes, verify the header fields, and re-encode
to the same lowercase hexadecimal strings before attempting live interop.

The repository also includes a standalone Go vector consumer in
`tests/conformance/zap_wire_v1_vectors_test.go`; run it from the repository root
with `cd tests/conformance && go test .`.

The Python reference tests cover:

- HELLO / HELLO_ACK capability intersection;
- DATA sequence acceptance and duplicate ACK;
- request-id idempotency;
- exponential retry calculation and attempt cap;
- FIN orphaned-sequence surfacing;
- static byte-for-byte frame vectors.
