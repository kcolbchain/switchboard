# ��11 �� Post-Quantum Signatures

**Status:** Spec draft v0.1
**Spec section:** ��11 (new)
**Parent document:** docs/post-quantum.md
**Tracks issue:** [#34](https://github.com/kcolbchain/switchboard/issues/34)

---

## 11.1 Overview

This section defines the post-quantum signature scheme for agent-to-agent payment envelopes. It covers:

- Algorithm registry and wire encoding (��11.2)
- Signing transcript format (��11.3)
- Verification rules (��11.4)
- Key management (��11.5)
- Security levels (��11.6)

The design follows the NIST FIPS 204 (ML-DSA) and FIPS 205 (SLH-DSA) standards. Hybrid mode pairs a classical ECDSA signature with an ML-DSA signature for migration.

## 11.2 Algorithm registry

### 11.2.1 Tag assignment

Each algorithm is identified by a uint8 tag on the ZAP wire and a string identifier in JSON payloads.

| Tag   | String identifier              | Signature size | Public key size | Standard     | Security level |
|-------|-------------------------------|:--------------:|:---------------:|--------------|:--------------:|
| 0x00  | none                           | 0              | 0               | ��       | ��        |
| 0x01  | ecdsa-secp256k1                | 65 (r,s,v)     | 33 (compressed) | SEC-1        | Classical 128  |
| 0x10  | ml-dsa-44                      | 2,420          | 1,312           | FIPS 204     | NIST-I (128)   |
| 0x11  | ml-dsa-65                      | 3,309          | 1,952           | FIPS 204     | NIST-III (192) |
| 0x12  | ml-dsa-87                      | 4,627          | 2,592           | FIPS 204     | NIST-V (256)   |
| 0x20  | slh-dsa-128s                   | 7,856          | 32              | FIPS 205     | NIST-I (128)   |
| 0x21  | slh-dsa-128f                   | 17,088         | 32              | FIPS 205     | NIST-I (128)   |
| 0x22  | slh-dsa-192s                   | 17,592         | 48              | FIPS 205     | NIST-III (192) |
| 0x23  | slh-dsa-192f                   | 35,664         | 48              | FIPS 205     | NIST-III (192) |
| 0x24  | slh-dsa-256s                   | 49,856         | 64              | FIPS 205     | NIST-V (256)   |
| 0x80  | hybrid-ecdsa-ml-dsa-65         | 65+3,309       | 33+1,952        | Concat       | Hybrid 192     |
| 0x81  | hybrid-ecdsa-ml-dsa-87         | 65+4,627       | 33+2,592        | Concat       | Hybrid 256     |

### 11.2.2 Encoding rules

**JSON wire:**

```json
{
  "signature_alg": "ml-dsa-65",
  "signature": "base64-encoded-raw-sig-bytes"
}
```

The signature field is unpadded base64url (RFC 4648 ��5). The signature_alg field is the string identifier from the registry above.

**ZAP binary wire:**

```
uint8   signature_alg    // tag from registry
bytes   signature        // variable-length, raw bytes
```

The signature field length is determined by the algorithm (see table above). For hybrid modes, the bytes are concatenated in a fixed order (ECDSA first, then ML-DSA / SLH-DSA).

### 11.2.3 Hybrid mode encoding

For hybrid-ecdsa-ml-dsa-65 (tag 0x80):

```
signature_bytes = ecdsa_sig_65_bytes || ml_dsa_65_sig_3309_bytes
pk_bytes = ecdsa_pk_33_bytes || ml_dsa_65_pk_1952_bytes
```

A verifier MUST check both halves. If either half fails verification, the entire signature is REJECTED.

## 11.3 Signing transcript

### 11.3.1 Domain separation

The bytes signed are a domain-separated hash of the canonical payload. This prevents cross-protocol replay (e.g., a signature from the A2A payment protocol cannot be reused for a different protocol).

```
transcript = "switchboard/pq/v1\0" || <payload-type-tag> || <canonical-bytes>
digest     = SHAKE-256(transcript, 64)   // 64 bytes = 512 bits output
signature  = Sign(sk, digest)
```

### 11.3.2 Payload type tags

| Payload type             | Tag (uint8)  | Description                      |
|--------------------------|:------------:|----------------------------------|
| PaymentOffer           | 0x01      | Offer from payee to payer        |
| PaymentProof          | 0x02      | Proof of delivery from payee     |
| PaymentRequest         | 0x03      | Request from payer to payee      |
| Receipt               | 0x04      | Payment receipt (aggregation)    |
| BatchRoot             | 0x05      | Batch Merkle root (issue #43)    |
| CrossChainEnvelope    | 0x06      | Cross-chain settlement envelope  |

### 11.3.3 Canonical bytes

For JSON payloads, canonical-bytes is the output of:

```python
def get_canonical_bytes(payload: dict) -> bytes:
    """Return canonical bytes for signing. Excludes signature fields."""
    excluded = {"signature", "signature_alg"}
    filtered = {k: v for k, v in payload.items() if k not in excluded}
    return json.dumps(filtered, sort_keys=True, separators=(",", ":")).encode("utf-8")
```

For ZAP wire payloads, canonical-bytes is the raw struct bytes with the signature and signature_alg fields zeroed.

### 11.3.4 Rationale

- Domain prefix ("switchboard/pq/v1\0"): Binds the signature to switchboard's PQ protocol, version 1. The null byte prevents ambiguity with string-terminated protocols.
- Payload type tag: Ensures a signature on a PaymentOffer cannot be replayed as a PaymentProof.
- SHAKE-256 (512 bits): Provides a security margin above the highest PQ security level (NIST-V = 256-bit classical).
- Signature fields excluded from canonical bytes: The signature cannot be part of what is signed (circular dependency).

## 11.4 Verification rules

### 11.4.1 Standard verification flow

```python
def verify_envelope_signature(
    payload: dict,
    pk: bytes,
    alg: str,
    sig: bytes,
    payload_type: int,
) -> bool:
    if alg == "none":
        return True

    transcript = build_transcript(payload, payload_type)
    digest = shake_256(transcript, 64)

    if alg.startswith("hybrid-"):
        ecdsa_sig, pq_sig = split_hybrid_sig(sig, alg)
        ecdsa_pk, pq_pk = split_hybrid_pk(pk, alg)
        return (
            ecdsa_verify(ecdsa_pk, digest, ecdsa_sig) and
            pq_verify(pq_pk, digest, pq_sig, alg)
        )
    else:
        return pq_verify(pk, digest, sig, alg)
```

### 11.4.2 Policy enforcement

The receiving agent has a signature policy that controls which signatures are accepted:

```python
@dataclass
class SignaturePolicy:
    accept_unsigned: bool = False
    accept_classical: bool = False
    accept_pq: bool = True
    accept_hybrid: bool = True
    min_security_level: int = 192
    allowed_algs: Set[str] | None = None
```

Default policy (switchboard v1.1+):
- accept_unsigned = False
- accept_classical = False
- accept_pq = True
- accept_hybrid = True
- min_security_level = 192

## 11.5 Key management

### 11.5.1 Key pair generation

```python
@dataclass
class PQKeyPair:
    alg: str
    sk: bytes
    pk: bytes
    key_id: str         # sha256(pk)[:16] hex
    created_at: int

    @classmethod
    def generate(cls, alg: str = "ml-dsa-65") -> "PQKeyPair": ...
    def sign(self, transcript: bytes) -> bytes: ...
    def verify(self, payload: dict, sig: bytes, payload_type: int) -> bool:
        digest = shake_256(build_transcript(payload, payload_type), 64)
        return pq_verify(self.pk, digest, sig, self.alg)
```

### 11.5.2 Storage format

Keys are stored on disk in a custom encrypted format:

```
+----------------+------------------+------------------+------------------+
| magic bytes     | KDF params       | nonce (12 bytes) | ciphertext       |
| "SWITCHBOARD\n" | scrypt N,r,p     |                   | ChaCha20-Poly1305|
| 12 bytes        | 4+4+4 bytes      |                   | variable length  |
+----------------+------------------+------------------+------------------+
```

- KDF: scrypt (N=2^20, r=8, p=1) wrapping the passphrase into a 256-bit key.
- Encryption: ChaCha20-Poly1305 (AEAD).
- Plaintext: JSON {alg, sk_hex, pk_hex, key_id, created_at}.

### 11.5.3 Key publication

Agents publish their PQ public keys at a .well-known/agent-payment.json endpoint:

```json
{
  "pq_algs": ["ml-dsa-65", "hybrid-ecdsa-ml-dsa-87"],
  "keys": {
    "ml-dsa-65": {
      "key_id": "a1b2c3d4e5f6g7h8",
      "pk_hex": "0x..."
    }
  }
}
```

## 11.6 Security levels

### 11.6.1 NIST security level mapping

| Level | Classical security | Quantum security | Recommended algorithms |
|:-----:|:------------------:|:----------------:|------------------------|
| I     | 128-bit            | 64-bit           | ML-DSA-44, SLH-DSA-128s/f |
| III   | 192-bit            | 96-bit           | ML-DSA-65, SLH-DSA-192s/f |
| V     | 256-bit            | 128-bit          | ML-DSA-87, SLH-DSA-256s |

### 11.6.2 Switchboard recommendations

| Deployment        | Recommended algorithm | Rationale                    |
|-------------------|-----------------------|------------------------------|
| Testnet / dev     | ML-DSA-44             | Smallest signature, fast      |
| Mainnet default   | ML-DSA-65             | NIST-III, balanced            |
| High security     | ML-DSA-87             | NIST-V, max margin            |
| Migration         | Hybrid ECDSA+ML-DSA-65 | Both verifies must pass      |
| Conservative      | SLH-DSA-128s          | Hash-based, minimal assumptions |

### 11.6.3 Signature size budget

For ZAP wire, the total envelope size including signature:

| Algorithm      | Envelope overhead | Suitable for      |
|----------------|:-----------------:|-------------------|
| ML-DSA-44      | ~2.5 KB           | All chains        |
| ML-DSA-65      | ~3.3 KB           | All chains        |
| ML-DSA-87      | ~4.6 KB           | L2 / LUX          |
| SLH-DSA-128s   | ~7.9 KB           | LUX (high mem)    |
| Hybrid ECDSA+65| ~3.4 KB           | Migration         |

## 11.7 Test vectors

### 11.7.1 Canonical test vector format

```json
{
  "vectors": [
    {
      "id": "pq-sig-01",
      "description": "ML-DSA-65 signing of minimal PaymentOffer",
      "alg": "ml-dsa-65",
      "payload_type": 1,
      "payload": { "version": "1.0", "amount_wei": 1000 },
      "pk_hex": "0x...",
      "canonical_bytes_b64": "...",
      "digest_hex": "0x...",
      "signature_hex": "0x..."
    }
  ]
}
```

### 11.7.2 Negative test cases

Each positive vector has a negative counterpart that flips one byte in the canonical input, asserts verify == false.
