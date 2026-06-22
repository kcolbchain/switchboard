# ZAP Wire Protocol v1.0 — Session Layer

Status: Draft for magicians / LP review
Issue: kcolbchain/switchboard#85
Scope: connection-level **session** semantics that sit *under* the existing
`PaymentOffer` / `PaymentProof` payload codecs in `switchboard/zap_transport.py`.

This document specifies the framing, handshake, sequencing, acknowledgement,
retry, idempotency, and teardown rules for a ZAP wire session. It is
deliberately payload-agnostic: a session carries opaque application payloads
(typically an encoded `PaymentOffer`/`PaymentProof`, or a generic Switchboard
frame from #76). The session layer never inspects payload bytes.

The session framing here is **pure Python with no external dependency**
(`zap_py` is NOT required). It is a self-describing big-endian binary format so
a Go or Rust peer can implement the same bytes from this document alone.

---

## 1. Design goals

1. **Implementable from bytes.** Every field has a fixed offset or an explicit
   length prefix. No reliance on a schema-compiler at the session layer.
2. **Cross-language.** All multi-byte integers are unsigned big-endian
   ("network byte order"). No floats on the wire.
3. **Deterministic.** Encoding a frame is a pure function of its fields, so
   conformance vectors can pin exact hex.
4. **Forward-compatible.** A version + capability handshake lets peers refuse
   or downgrade gracefully instead of misparsing.
5. **At-least-once with dedup.** Retries are safe because every request carries
   an idempotency key the receiver deduplicates on.

---

## 2. Byte conventions

- All integers are unsigned, big-endian, fixed width as stated.
- `u8`, `u16`, `u32`, `u64` denote 1/2/4/8-byte unsigned integers.
- A frame's `PAYLOAD` is explicitly length-prefixed by the `PAYLOAD_LEN` header
  field, so frames are self-delimiting on a byte stream.
- Maximum payload length is `2^32 - 1`; v1.0 receivers MAY reject payloads
  larger than `MAX_PAYLOAD` (default 16 MiB) with `ERR_TOO_LARGE`.
- The `RESERVED` byte and reserved bits MUST be written as zero and MUST be
  ignored on read (so a later minor version can assign them).

---

## 3. Frame format

Every byte exchanged in a session is a sequence of self-delimiting **frames**.
A frame is:

```
offset  size  field
------  ----  -----------------------------------------------------------
0       2     MAGIC          = 0x5A50  ("ZP", ZAP)
2       1     WIRE_VERSION   = 0x01    (session-layer version; see §4)
3       1     FRAME_TYPE     (see §3.1)
4       1     FLAGS          (bitfield; see §3.2)
5       1     RESERVED       (MUST be 0)
6       4     SEQ            u32 — per-direction sequence number (§5)
10      4     ACK            u32 — cumulative ack of peer's SEQ (§6)
14      8     REQUEST_ID     u64 — idempotency key (§8); 0 = "not a request"
22      4     PAYLOAD_LEN    u32 — length of PAYLOAD that follows
26      N     PAYLOAD        N = PAYLOAD_LEN bytes (opaque to session layer)
```

Fixed header size is **26 bytes**. Total frame size is `26 + PAYLOAD_LEN`.

A frame with a bad `MAGIC` or an unknown `WIRE_VERSION` MUST be rejected before
any other field is interpreted (see §4.3).

### 3.1 Frame types

| Value | Name      | Direction        | Carries payload | Purpose                              |
|-------|-----------|------------------|-----------------|--------------------------------------|
| 0x01  | `HELLO`   | initiator → peer | yes (caps)      | Open: offered version + capabilities |
| 0x02  | `WELCOME` | peer → initiator | yes (caps)      | Accept: negotiated version + caps    |
| 0x03  | `DATA`    | either           | yes             | Application payload                  |
| 0x04  | `ACK`     | either           | no              | Standalone cumulative ack            |
| 0x05  | `FIN`     | either           | no              | Graceful half-close (§9)             |
| 0x06  | `RST`     | either           | optional (err)  | Abort session with error code (§10)  |

Unknown frame types received on an established session MUST be answered with
`RST(ERR_PROTOCOL)` and the session torn down.

### 3.2 FLAGS bitfield

```
bit 0 (0x01)  ACK_PRESENT   — the ACK field is meaningful (cumulative ack)
bit 1 (0x02)  REQ           — this frame is an idempotent request (REQUEST_ID set)
bit 2 (0x04)  RETRANSMIT    — this is a retransmission of a previously sent SEQ
bits 3..7     RESERVED      — MUST be 0
```

`ACK` is only read when `ACK_PRESENT` is set. `HELLO`/`WELCOME`/`DATA`/`FIN`/
`RST` MAY set `ACK_PRESENT` to piggyback an ack. A standalone `ACK` frame MUST
set `ACK_PRESENT`.

---

## 4. Handshake and capability negotiation

### 4.1 Capability bitmask (DECISION)

The capability field is a **`u32` bitmask split as 8 bits version + 24 bits
feature flags**:

```
 31                    24 23                                              0
+------------------------+-------------------------------------------------+
|   VERSION (8 bits)     |          FEATURE FLAGS (24 bits)                |
+------------------------+-------------------------------------------------+
```

- **VERSION** (`bits 24..31`, 8 bits): highest session-protocol version the
  sender supports. v1.0 sends `0x01`. Value `0` is invalid.
- **FEATURE FLAGS** (`bits 0..23`, 24 bits): independently negotiable features.

Defined v1.0 feature flags:

| Bit  | Mask       | Name              | Meaning                                          |
|------|------------|-------------------|--------------------------------------------------|
| 0    | 0x000001   | `CAP_ACK`         | Sender honours cumulative ACK + retransmit (§6)  |
| 1    | 0x000002   | `CAP_RETRY`       | Sender will retransmit unacked frames (§7)       |
| 2    | 0x000004   | `CAP_IDEMPOTENT`  | Sender dedups on REQUEST_ID (§8)                 |
| 3    | 0x000008   | `CAP_FIN`         | Sender performs graceful FIN teardown (§9)       |
| 4    | 0x000010   | `CAP_NESTED`      | Sender understands nested payload tags (#76)     |
| 5..23| —          | RESERVED          | MUST be 0 in v1.0; ignored on read               |

The full v1.0 baseline mask is therefore
`0x0100001F` (version 1, all five flags set).

Rationale for 8+24: the protocol version is a single small monotonic integer,
so 8 bits (255 versions) is ample and keeps it byte-aligned in the high octet
for easy human inspection of the hex. 24 flag bits leaves generous headroom for
post-quantum / streaming / multiplexing features without a v2 wire bump.

### 4.2 HELLO / WELCOME payload

The `HELLO` and `WELCOME` payloads are exactly:

```
offset size field
0      4    CAPABILITIES  u32  (§4.1)
4      8    SESSION_ID    u64  (initiator-chosen random; echoed in WELCOME)
```

Payload length is 12. The handshake completes when the initiator has sent
`HELLO` and received a `WELCOME` echoing the same `SESSION_ID`.

### 4.3 Negotiation algorithm

1. Initiator sends `HELLO` with `SEQ = 0`, its full capability mask, and a
   random non-zero `SESSION_ID`.
2. Responder:
   - If `MAGIC`/`WIRE_VERSION` invalid → drop / `RST(ERR_PROTOCOL)`.
   - Compute `negotiated_version = min(my_version, peer_version)`.
     If `negotiated_version == 0` (no common version) → `RST(ERR_VERSION)`.
   - Compute `negotiated_flags = my_flags & peer_flags` (intersection: a
     feature is active only if **both** peers advertise it).
   - Reply `WELCOME` with `SEQ = 0`, `CAPABILITIES =
     (negotiated_version << 24) | negotiated_flags`, echoing `SESSION_ID`.
3. Initiator validates the echoed `SESSION_ID`; on mismatch → `RST(ERR_PROTOCOL)`.
   The negotiated capability mask in the `WELCOME` is authoritative for **both**
   sides for the life of the session.

A session is **ESTABLISHED** once the initiator processes a valid `WELCOME` and
the responder has sent it. `DATA`/`ACK`/`FIN` before establishment → `RST(ERR_PROTOCOL)`.

---

## 5. Sequence numbers

- Each direction has its own `u32` SEQ space starting at **0** (the handshake
  frame in that direction).
- Every frame that occupies sequence space increments the sender's SEQ by 1:
  `HELLO`, `WELCOME`, `DATA`, and `FIN` consume one SEQ each.
- `ACK` and `RST` do **not** consume sequence space (they carry the *current*
  SEQ as a non-advancing marker; receivers ignore the SEQ of an `ACK`/`RST`
  for ordering purposes).
- SEQ is monotonic and wraps modulo `2^32`. v1.0 sessions are not expected to
  exceed `2^31` frames; comparisons use unsigned-mod-2^32 "is-after" semantics
  (`a` is after `b` iff `0 < (a - b) mod 2^32 < 2^31`).

### 5.1 In-order delivery and reordering

The receiver tracks `expected_seq` (next SEQ it will accept in order),
initialized to `0` and advanced past the handshake frame.

- **In-order** (`SEQ == expected_seq`): deliver payload to the application,
  advance `expected_seq`, then drain any buffered contiguous successors.
- **Future / out-of-order** (`SEQ` is after `expected_seq`): buffer the frame
  in a reorder map keyed by SEQ (bounded by `MAX_REORDER`, default 256 frames;
  overflow → `RST(ERR_FLOW)`), and re-ack the last in-order SEQ to prompt fast
  retransmit of the gap.
- **Duplicate / past** (`SEQ` is before `expected_seq`): the frame has already
  been delivered. Drop the payload but still send an `ACK` (the peer's prior
  ack was likely lost).

---

## 6. ACK semantics

ACK is **cumulative**: `ACK = N` means "I have received, in order, every frame
with SEQ ≤ N in your stream." The first legal ack value acknowledges SEQ 0
(the handshake), so a peer that has only processed the handshake acks `0`.

- A receiver SHOULD ack the highest *contiguous* SEQ it has delivered, never a
  SEQ beyond a gap.
- ACK MAY be piggybacked on any outbound frame (set `ACK_PRESENT`) or sent
  standalone as an `ACK` frame.
- Receiving `ACK = N` retires every unacked sent frame with SEQ ≤ N from the
  retransmit queue (§7) and is idempotent (a repeated or stale ack is harmless;
  an ack for a SEQ already retired is ignored).
- A peer without `CAP_ACK` negotiated MUST NOT be sent retransmits; the session
  degrades to fire-and-forget ordered delivery.

---

## 7. Retry / timeout policy

When `CAP_RETRY` is negotiated, every SEQ-consuming frame is placed on a
**retransmit queue** with a send timestamp until it is cumulatively acked.

Configurable parameters (defaults chosen for LAN port-9999 agent traffic):

| Param            | Default | Meaning                                                  |
|------------------|---------|----------------------------------------------------------|
| `rtt`            | 200 ms  | Estimated round-trip time; base retransmit timeout (RTO) |
| `rto_multiplier` | 2.0     | Exponential backoff factor per attempt                   |
| `max_retries`    | 5       | Attempts before the frame is declared lost               |
| `max_rto`        | 30 s    | Ceiling on the backed-off timeout                        |

- The RTO for attempt `k` (0-indexed) is
  `min(rtt * rto_multiplier^k, max_rto)`.
- A frame whose oldest unacked send is older than its current RTO is
  **retransmitted** with the `RETRANSMIT` flag set and `attempt` incremented.
  Its SEQ and REQUEST_ID are unchanged (so the receiver dedups, §8).
- After `max_retries` retransmits without an ack, the frame is declared lost;
  the session raises `ERR_TIMEOUT` to the application and SHOULD `RST`.
- Time is injected (a `now()` callable) so retransmit logic is deterministic and
  testable without real clocks.

Implementations MAY refine `rtt` with an RTT estimator (e.g. EWMA over observed
ack latencies); v1.0 mandates only the static-RTO behaviour above.

---

## 8. REQUEST_ID idempotency and dedup

- A frame with the `REQ` flag carries a non-zero `u64 REQUEST_ID` chosen by the
  sender to uniquely identify a logical request within the session.
- The receiver maintains a **seen-request set**. On a `REQ` frame:
  - If `REQUEST_ID` is already in the set → this is a duplicate (e.g. a retried
    request whose original was processed but whose response/ack was lost). The
    receiver MUST NOT re-execute the application side effect; it re-acks and, if
    it cached a response, replays the cached response.
  - Otherwise → record the id, deliver to the application exactly once.
- `REQUEST_ID = 0` means "not an idempotent request"; such frames are delivered
  by SEQ ordering only and are not deduplicated by id.
- The seen-request set is per-session and discarded on session close. Bounded by
  `MAX_REQUEST_IDS` (default 4096, FIFO eviction); eviction only affects
  long-lived sessions and never re-executes within the window.

This makes the at-least-once retransmit of §7 safe: a retransmitted request
arrives with the same REQUEST_ID and is deduplicated.

---

## 9. Session close (FIN) and orphaned sequences

Close is a **graceful half-close** like TCP:

1. A side that is done sending sends `FIN` (consuming one SEQ). It MAY piggyback
   a final ACK. After sending FIN it MUST NOT originate new `DATA`/`REQ` frames,
   but it MUST keep acking inbound frames and MAY still retransmit its own
   in-flight (unacked) frames until they are acked or declared lost.
2. The peer, on receiving `FIN`, delivers any buffered in-order payloads up to
   the FIN's SEQ, then enters `CLOSING`. It sends its own `FIN` when it too has
   no more to send.
3. The session is `CLOSED` once **both** FINs have been sent and all SEQ ≤ each
   FIN are cumulatively acked.

### 9.1 Orphaned-sequence handling

An **orphaned sequence** is a SEQ that was sent before FIN but is still unacked,
or a buffered out-of-order frame whose predecessors never arrived, at the moment
of close:

- **Unacked-but-sent (in-flight) on the FIN-sender side:** continue to be
  retransmitted per §7 until acked or `max_retries` is hit. FIN does not cancel
  the retransmit queue. Only when every SEQ ≤ FIN is acked is the half-stream
  truly drained.
- **Buffered out-of-order on the receiver side:** if a gap below the FIN's SEQ
  is never filled (predecessor declared lost), the receiver MUST discard the
  orphaned buffered frames at close and surface `ERR_INCOMPLETE` for that
  session rather than deliver out of order. A clean FIN therefore requires the
  full contiguous SEQ range `[0, FIN_SEQ]` to have been delivered.
- A `RST` (§10) is the hard alternative: it abandons all queues immediately
  (orphans are dropped, no further retransmit, application sees `ERR_RESET`).

---

## 10. Error codes (RST payload)

`RST` MAY carry a 4-byte payload: `u32 ERROR_CODE`.

| Code | Name             | Cause                                              |
|------|------------------|----------------------------------------------------|
| 0x01 | `ERR_PROTOCOL`   | Malformed frame, illegal state transition          |
| 0x02 | `ERR_VERSION`    | No common protocol version in handshake            |
| 0x03 | `ERR_TIMEOUT`    | A frame exceeded `max_retries` without ack         |
| 0x04 | `ERR_FLOW`       | Reorder/flow bound exceeded (`MAX_REORDER`)        |
| 0x05 | `ERR_TOO_LARGE`  | Payload exceeded `MAX_PAYLOAD`                      |
| 0x06 | `ERR_INCOMPLETE` | FIN reached with an unfillable SEQ gap (§9.1)      |
| 0x07 | `ERR_RESET`      | Peer reset; surfaced to application                |

---

## 11. State machine

```
            send HELLO                 recv WELCOME (id ok)
  CLOSED ───────────────▶ HELLO_SENT ──────────────────────▶ ESTABLISHED
    │                                                            │
    │ recv HELLO                                                 │ send/recv FIN
    └──────────▶ WELCOME_SENT ───── recv DATA/ack ──▶ ESTABLISHED│
                                                                 ▼
                                                            CLOSING ──(both FIN
                                                                 │     + fully
                                                                 │     acked)──▶ CLOSED
                       any error / RST at any state ────────────────────────────▶ CLOSED
```

- `CLOSED → HELLO_SENT`: initiator sends HELLO.
- `CLOSED → WELCOME_SENT`: responder receives HELLO, sends WELCOME.
- `HELLO_SENT → ESTABLISHED`: initiator receives matching WELCOME.
- `WELCOME_SENT → ESTABLISHED`: responder treats WELCOME-sent as established for
  sending DATA / receiving the first post-handshake frame.
- `ESTABLISHED → CLOSING`: either side sends or receives FIN.
- `CLOSING → CLOSED`: both FINs exchanged and all SEQ ≤ FIN acked.
- `* → CLOSED`: any `RST` or fatal error.

---

## 12. Relationship to existing codecs

- The session `PAYLOAD` of a `DATA` frame is normally an encoded
  `PaymentOffer` / `PaymentProof` (see `encode_offer`/`encode_proof`) or a
  generic Switchboard frame with nested tags (#76). The session layer treats it
  as opaque bytes.
- `CAP_NESTED` advertises that the sender understands the #76 nested-tag frame
  inside `DATA` payloads; it does not change session framing.
- The session layer does not sign payloads; payload signing remains the
  responsibility of the `signing_transcript` path.

---

## 13. Open questions for magicians / LP review

1. **Cumulative vs. selective ack.** v1.0 is cumulative-only. Do we want
   SACK ranges for lossy WAN links, or is LAN-cumulative enough for v1.0?
2. **SEQ width.** `u32` per direction. Is wrap (2^32 frames) ever a concern for
   long-lived streaming sessions, or should streaming use a fresh session?
3. **REQUEST_ID allocation.** Sender-chosen `u64`. Should we mandate a structure
   (e.g. high 32 bits = sender id, low 32 = counter) to avoid cross-peer
   collisions when sessions are pooled?
4. **Response caching on dedup.** §8 says a deduped request "MAY replay a cached
   response." Should v1.0 mandate response caching, or is re-ack-only
   sufficient (application owns its own idempotency beyond the ack)?
5. **FIN vs. orphan policy.** §9.1 surfaces `ERR_INCOMPLETE` on an unfillable
   gap at FIN. Alternative: allow the application to opt into "deliver what
   arrived, gaps reported" — do LP flows ever want partial delivery?
6. **Capability bit budget.** 24 feature bits. Are PQ-signing / streaming /
   multiplexing each a single bit, or do some need sub-fields (pushing us toward
   a TLV capability list instead of a flat bitmask)?
7. **MAGIC collision.** `0x5A50` ("ZP"). Confirm no clash with the existing
   raw ZAP struct stream on port 9999 when both share a socket.
