# Post-Quantum Signed Agent-to-Agent Payments

**Status:** Design draft v0.1
**Owner:** @Pattermesh (drafting) / @abhicris (review)
**Tracks meta-issue:** TBD

---

## 1. Why now

Switchboard's A2A payment protocol v1.0 ships unsigned at the application
layer. Authenticity rides on two assumptions:

1. **HTTPS / TLS** for the HTTP path (`X-Payment-Required`, `X-Payment-Proof`
   in `x402_middleware.py`).
2. **secp256k1 ECDSA on the Ethereum tx** for on-chain settlement — the
   "proof" a receiver checks is `tx_hash` resolution against the chain.

Neither holds in a post-quantum future. Shor breaks secp256k1 on day one;
TLS without PQ-hybrid suites is recoverable from "harvest-now-decrypt-later"
captures. Switchboard is the substrate; if we want it to outlive the
classical-crypto era we add an **application-layer PQ signature on the
payment envelopes themselves**, independent of the rail.

This is also the cleanest place to validate Lux's PQ stack (`threshold`,
`fhe`, `mpc`, `proofs`) against a real consumer.

## 2. Threat model

| Adversary capability        | Today's defense          | After this work                                    |
| --------------------------- | ------------------------ | -------------------------------------------------- |
| Passive TLS capture (now)   | TLS 1.3                  | PQ sig on envelope — capture reveals nothing usable |
| TLS break (future quantum)  | none                     | PQ sig on envelope still verifies                  |
| Fake `PaymentOffer` injection in-band | tx-hash check only useful *after* paying | PQ sig on offer — verify before paying |
| ECDSA break (future quantum) | none on-chain            | hybrid mode pairs PQ with ECDSA; off-chain proof still verifies; full on-chain PQ tracked separately under Lux escrow variant |

Out of scope for this design: defending the EVM tx signature itself.
That requires a PQ-aware chain (Lux). We cover the off-chain envelope
and provide hooks for a Lux-native escrow.

## 3. Wire format

### 3.1 Algorithm registry

Algorithm tag is a single `uint8` on the ZAP wire and a string on JSON.

| tag  | name              | sig size  | pk size  | source                |
| ---- | ----------------- | --------- | -------- | --------------------- |
| 0x00 | `none`            | 0         | 0        | sentinel, unsigned    |
| 0x01 | `ecdsa-secp256k1` | 65 (r,s,v)| 33 (cmp) | classical, existing   |
| 0x10 | `ml-dsa-44`       | 2,420     | 1,312    | FIPS 204              |
| 0x11 | `ml-dsa-65`       | 3,309     | 1,952    | FIPS 204              |
| 0x12 | `ml-dsa-87`       | 4,627     | 2,592    | FIPS 204              |
| 0x20 | `slh-dsa-128s`    | 7,856     | 32       | FIPS 205              |
| 0x21 | `slh-dsa-128f`    | 17,088    | 32       | FIPS 205              |
| 0x80 | `hybrid-ecdsa-ml-dsa-65` | 65+3,309 | 33+1,952 | concat, both verify |

Default for v1: `ml-dsa-65`. Hybrid mode `0x80` is recommended during
migration so a verifier rejecting one of the two halves rejects the
message.

### 3.2 Signing transcript

The bytes signed are a domain-separated hash of the canonical payload:

```
transcript = "switchboard/pq/v1\0" || <payload-type-tag> || <canonical-bytes>
digest     = SHAKE-256(transcript, 64)
signature  = Sign(sk, digest)
```

`<payload-type-tag>` is `0x01` for `PaymentOffer`, `0x02` for
`PaymentProof`, `0x03` for `PaymentRequest`. Domain separation
guarantees a signature on an offer can't be replayed as a proof.

`<canonical-bytes>` for JSON payloads = the existing `content_hash`
input (sorted keys, tight separators, `created_at` and `status`
excluded, plus the new `signature` and `signature_alg` fields also
excluded since they're the things being computed).

For ZAP wire payloads, `<canonical-bytes>` = the bytes of the
`StructBuilder.build()` output with `signature` and `signature_alg`
fields zeroed. This keeps offset arithmetic stable.

### 3.3 Schema additions

`PaymentOffer` and `PaymentProof` gain two optional fields:

```python
@dataclass
class PaymentOffer:
    # … existing fields …
    signature_alg: str = "none"     # registry name, JSON
    signature: bytes = b""           # raw bytes; base64 in JSON, raw on wire
```

ZAP `StructBuilder` additions, appended after existing fields so old
readers ignore them:

```python
.uint8("signature_alg")
.bytes("signature")          # variable-length, see _AMOUNT_BYTES pattern
```

`PaymentRequest` (in `src/payment_protocol.py`) gains the same two
fields. Spec §2 table extended.

## 4. Key management

New module `switchboard/pq_keys.py`:

```python
class PQKeyPair:
    alg: str                  # registry name
    sk: bytes                 # secret key, owner-only
    pk: bytes                 # public key
    key_id: str               # sha256(pk)[:16] hex, the agent's PQ identity

    @classmethod
    def generate(cls, alg: str = "ml-dsa-65") -> "PQKeyPair": ...
    @classmethod
    def load(cls, path: str, passphrase: bytes | None = None) -> "PQKeyPair": ...
    def save(self, path: str, passphrase: bytes | None = None) -> None: ...
    def sign(self, transcript: bytes) -> bytes: ...

def verify(alg: str, pk: bytes, transcript: bytes, sig: bytes) -> bool: ...
```

Storage on disk: PEM-like file with KDF-wrapped secret key
(scrypt, then chacha20-poly1305). No on-chain key publication in v1;
agents exchange `pk` out-of-band or via a future `.well-known/agent-payment.json`.

## 5. Verification flow

```
                  PaymentOffer received
                            │
                            ▼
            ┌─────────────────────────┐
            │ offer.signature_alg     │
            └───────────┬─────────────┘
                        │
       ┌────────────────┼────────────────┐
       │                │                │
       ▼                ▼                ▼
    "none"        "ecdsa-…"        "ml-dsa-…" / "hybrid-…"
    accept iff    classical        PQ verify (+ ECDSA half
    not in       ECDSA verify       if hybrid). Reject if any
    strict mode                     half fails.
```

`X402Middleware` adds a `require_signed: bool = False` (default off
during migration) and `accepted_algs: set[str]`. When `True`, an
unsigned offer is treated as a parse error.

The provider side (server middleware) signs its own outgoing offers
with `pq_signer` if configured.

## 6. On-chain story

Two paths, separately tracked.

**Path A — Off-chain envelope only (in scope).** The PQ sig
authenticates the offer/proof to the *counterparty*. On-chain
settlement still goes through the existing `AgentEscrow.sol` with
ECDSA tx signatures. Trust model improvement: an offer's authenticity
is verifiable before the payer commits funds.

**Path B — Lux-native PQ escrow (out of scope; tracked).**
`contracts/AgentEscrowPQ.sol` (or a Lux-side equivalent) accepts a
`bytes signature, uint8 alg` pair on `createPayment` and verifies it
via a Lux precompile. Requires `luxfi/threshold` or a precompile in
the EVM extensions on Lux. Defer to a follow-up; capture the
interface here for forward-compat.

## 7. Migration plan

1. **v1.1 — non-breaking.** Add fields, default `signature_alg="none"`,
   `require_signed=False`. Existing payloads unchanged.
2. **v1.2 — hybrid recommended.** Docs + samples promote
   `hybrid-ecdsa-ml-dsa-65`. Web explorer shows sig presence.
3. **v2.0 — PQ default.** `require_signed=True` becomes the
   middleware default. Unsigned offers logged + rejected.
4. **v2.x — Lux escrow.** On-chain PQ verification ships
   independently.

## 8. Dependencies

- `liboqs-python` (Open Quantum Safe) — covers ML-DSA, SLH-DSA.
  Optional dep, gated like `zap_py`. Native build; pin via wheel
  where available.
- Existing: `eth-account` for the ECDSA half of hybrid.
- New optional extra: `pip install 'switchboard-agents[pq]'`.

Pure-Python fallback (e.g. `dilithium-py`) for environments
without native deps, with a loud "slow path" warning. Useful for
test rigs only.

## 9. Test vectors

Add three fixtures to `tests/protocol_vectors/payment_request.v1.json`
(or a new `payment_request.v1.pq.json` keyed off the same payloads):

| Fixture                   | Alg                       | Exercises                                     |
| ------------------------- | ------------------------- | --------------------------------------------- |
| `fixture-04-mldsa65-minimal` | `ml-dsa-65`            | required-only payload + ML-DSA-65 signature   |
| `fixture-05-slhdsa128s`   | `slh-dsa-128s`            | hash-based fallback, conservative assumption  |
| `fixture-06-hybrid`       | `hybrid-ecdsa-ml-dsa-65`  | both halves must verify; deterministic ECDSA  |

Each fixture pins:
- input payload
- public key (hex)
- signing transcript bytes (post domain-separation)
- signature bytes (hex)
- `expected_verify: true`

A negative twin per fixture flips one transcript byte and asserts
`verify == false`. Cross-impl interop fixtures live next to
`luxfi/zap` Go test files.

## 10. Breakdown — issues to file

The first six are ready to ship; the rest depend on these landing.

| # | Title | DoD | Depends on |
|---|---|---|---|
| PQ-0 | **Meta: post-quantum signed A2A payments** | parent tracker; checklist below ticks | — |
| PQ-1 | Spec §11: PQ signatures — wire format + transcript + algorithm registry | `docs/post-quantum.md` merged; `docs/agent-payment-protocol.md` §11 added | — |
| PQ-2 | `switchboard/pq.py`: thin `liboqs` wrapper + `[pq]` extra | `sign()` / `verify()` for ML-DSA-44/65/87, SLH-DSA-128s/f; CI wheel install | PQ-1 |
| PQ-3 | `switchboard/pq_keys.py`: `PQKeyPair.generate/load/save/sign`, key-id derivation | passphrase-protected file format; round-trip test | PQ-2 |
| PQ-4 | Extend `PaymentOffer` / `PaymentProof` dataclasses with `signature_alg`, `signature` | non-breaking default `"none"`; `sign(keypair)` / `verify(pk)` helpers | PQ-3 |
| PQ-5 | Extend ZAP wire schema with `signature_alg` (`uint8`) + `signature` (`bytes`) | offset constants pinned; back-compat for old readers via append-only | PQ-4 |
| PQ-6 | Hybrid mode `0x80 hybrid-ecdsa-ml-dsa-65`: concat + both-must-verify | dedicated test, partial-fail cases | PQ-4 |
| PQ-7 | `X402Middleware`: `require_signed` + `accepted_algs`; reject when policy fails | docs + 4 test cases (signed-ok, signed-wrong-alg, unsigned-strict, hybrid-half-fail) | PQ-4 |
| PQ-8 | A2A x402 adapter: `extra.signature` + `extra.signatureAlg` (base64) | round-trip tests vs. fixture-04..06 | PQ-4 |
| PQ-9 | `PaymentRequest.content_hash` v2: domain-separated SHAKE-256 transcript | versioned; v1 hashes still computable; spec §2.2 updated | PQ-1 |
| PQ-10 | Test vectors fixture-04, fixture-05, fixture-06 + negative twins | `pytest tests/test_protocol_vectors.py` green with PQ deps | PQ-2 |
| PQ-11 | Conformance: cross-impl interop fixtures with `luxfi/zap` Go side | Go test reads the same JSON file and asserts | PQ-5, PQ-10 |
| PQ-12 | Web explorer: render `signature_alg`, sig present/absent badge, hybrid status | screenshot in PR | PQ-4 |
| PQ-13 | **Deferred** — Lux-native PQ escrow contract (`AgentEscrowPQ.sol`) interface sketch | spec stub only; no implementation here | PQ-1 |

## 11. Open questions

- **Algorithm default**: `ml-dsa-65` is the safe NIST-1+ pick; do we
  want `ml-dsa-87` for higher security margin at +40% sig size?
- **Hybrid concat order**: ECDSA-then-PQ vs PQ-then-ECDSA. Pin one;
  any choice is fine as long as it's pinned.
- **Key rotation cadence**: tied to agent identity. Need an
  `agent-identity.md` doc separately (cross-cuts with `.well-known`
  A2A discovery from spec §10).
- **Storage**: where does `key_id` get published so receivers know
  which `pk` to use? Out of scope here; rolls into A2A discovery.
- **Pure-Python fallback**: worth maintaining `dilithium-py` path or
  is `liboqs-python` enough? Native dep is the only real cost.

## 12. Non-goals

- Replacing ECDSA on the EVM tx itself.
- Encrypting payment metadata (separate problem: confidentiality,
  not authenticity; would use PQ-KEM, not PQ-sig).
- TLS hardening — handled at the transport layer.
- Threshold / multi-party PQ signatures — interesting but not v1.
