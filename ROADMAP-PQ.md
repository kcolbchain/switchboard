# Post-Quantum Roadmap: Agent-to-Agent Payments

**Status:** Meta roadmap v0.1
**Owner:** switchboard maintainers
**Tracks meta-issue:** [#33](https://github.com/kcolbchain/switchboard/issues/33)

---

## Overview

This roadmap charts the transition of switchboard's agent-to-agent payment protocol from classical ECDSA signatures to post-quantum (PQ) signatures. The migration spans four phases across 2026-2027, from specification to mandatory enforcement.

## Phase Plan

### Phase 0: Specification (Q2 2026) \u2190 CURRENT

| # | Item | Owner | DoD |
|---|------|-------|-----|
| PQ-0 | Meta: roadmap + tracker | @Gaotax2006 | This document merged |
| PQ-1 | Spec ��11: PQ signatures algorithm registry, transcript, wire format | @Gaotax2006 | spec/pq-signatures.md merged |
| PQ-2 | switchboard/pq.py: liboqs wrapper + [pq] extra | TBD | sign() / verify() for ML-DSA-44/65/87, SLH-DSA-128s/f |
| PQ-3 | switchboard/pq_keys.py: PQKeyPair + encrypted storage | TBD | Passphrase-protected file format; round-trip test |

**Target:** All spec documents merged and filed as issues by end of Q2 2026.

### Phase 1: Implementation (Q3 2026)

| # | Item | Owner | DoD |
|---|------|-------|-----|
| PQ-4 | Extend PaymentOffer / PaymentProof with signature_alg, signature | TBD | Non-breaking default "none"; sign()/verify() helpers |
| PQ-5 | Extend ZAP wire schema with PQ signature fields | TBD | Offset constants pinned; back-compat via append-only |
| PQ-6 | Hybrid mode: ECDSA + ML-DSA-65 | TBD | Both-must-verify; dedicated test |
| PQ-7 | X402Middleware: require_signed + accepted_algs | TBD | 4 test cases |
| PQ-8 | A2A x402 adapter: extra signature fields | TBD | Round-trip tests |

**Target:** PQ signatures functional in testnet by end of Q3 2026.

### Phase 2: Migration (Q4 2026)

| # | Item | Owner | DoD |
|---|------|-------|-----|
| PQ-9 | PaymentRequest content_hash v2: SHAKE-256 transcript | TBD | Versioned; v1 hashes still computable |
| PQ-10 | Test vectors + negative twins | TBD | pytest green with PQ deps |
| PQ-11 | Cross-impl interop with luxfi/zap Go | TBD | Go test reads same JSON vectors |
| PQ-12 | Web explorer: signature badge | TBD | Screenshot in PR |
| PQ-13 | Documentation: migration guide for agents | TBD | upgrade-pq.md |

**Target:** Hybrid mode recommended by default. All existing agents can upgrade without breaking changes.

### Phase 3: Hardening (Q1 2027)

| # | Item | Owner | DoD |
|---|------|-------|-----|
| PQ-14 | PQ-only default: require_signed=True becomes middleware default | TBD | Unsigned offers logged + rejected |
| PQ-15 | Key rotation: automatic re-key for long-running agents | TBD | agent-identity.md |
| PQ-16 | Lux-native PQ escrow (deferred) | TBD | Interface sketch in AgentEscrowPQ.sol |
| PQ-17 | Audit: third-party PQ implementation audit | TBD | Audit report |

**Target:** PQ mandatory. All unsigned offers rejected. Lux PQ escrow in testnet.

## Algorithm Selection Criteria

### Selection matrix

| Criteria              | ML-DSA-44 | ML-DSA-65 | ML-DSA-87 | SLH-DSA-128s | SLH-DSA-128f | Hybrid ECDSA+65 |
|-----------------------|:---------:|:---------:|:---------:|:------------:|:------------:|:---------------:|
| NIST security level   | I (128)   | III (192) | V (256)   | I (128)      | I (128)      | III (192)       |
| Signature size        | 2,420 B   | 3,309 B   | 4,627 B   | 7,856 B      | 17,088 B     | 3,374 B         |
| Public key size       | 1,312 B   | 1,952 B   | 2,592 B   | 32 B         | 32 B         | 1,985 B         |
| Signing speed         | Fast      | Moderate  | Slow      | Slow         | Fast         | Moderate        |
| Verification speed    | Fast      | Moderate  | Slow      | Slow         | Fast         | Moderate        |
| PQ assumption         | Lattice   | Lattice   | Lattice   | Hash-based   | Hash-based   | Lattice + ECDSA |
| Conservative          | No        | No        | No        | Yes          | Yes          | Partial         |
| Migration compatible  | Yes       | Yes       | Yes       | Yes          | Yes          | Yes             |

### Decision

1. Default: ML-DSA-65 (NIST-III). Balances security margin with size/speed. Lattice assumption is well-studied; NIST selected ML-DSA as the primary PQ standard.
2. Conservative fallback: SLH-DSA-128s. Hash-based signatures make minimal cryptographic assumptions. Use for high-value agents or those with regulatory requirements.
3. Migration: Hybrid ECDSA + ML-DSA-65. Both signatures must verify. Protects against the case where one of the two schemes is broken before the migration completes.
4. Future: FN-DSA (FIPS 206). Once standardized, evaluate for inclusion as a second lattice option with different security assumptions.

### Algorithm lifecycle

```
v1.0 (2026 Q2): ECDSA only (status quo)
v1.1 (2026 Q3): PQ optional, hybrid recommended
v2.0 (2027 Q1): PQ mandatory, ECDSA phased out
v2.x (2027+):   FN-DSA, Lux native PQ escrow
```

## Timeline

```
2026 Q2    2026 Q3    2026 Q4    2027 Q1    2027 Q2
  |          |          |          |          |
  [Spec]---->          |          |          |
  [Impl]---->[Impl]--->|          |          |
             [Phase1] [Phase2]-->|          |
                        [Migration]        |
                                 [Hardening]
                                   [Phase3]
```

### Milestones

| Date       | Milestone | Deliverable |
|------------|-----------|-------------|
| 2026-06-30 | PQ spec complete | ��11 merged, roadmap accepted |
| 2026-08-31 | PQ functional on testnet | PQ keys, signatures, verification working |
| 2026-10-31 | Hybrid default | All new agents default to hybrid mode |
| 2026-12-31 | Migration guide published | Documentation + examples for all agent frameworks |
| 2027-03-31 | PQ mandatory | Unsigned offers rejected at middleware level |
| 2027-06-30 | Lux PQ escrow | On-chain PQ verification on Lux testnet |

## Risk Assessment

### Risk matrix

| Risk | Probability | Impact | Mitigation |
|------|:-----------:|:------:|------------|
| ML-DSA broken before migration | Very low | Critical | Hybrid mode + SLH-DSA fallback |
| liboqs unmaintained | Low | High | Pure-Python fallback, multi-library support |
| Agent operators slow to upgrade | Medium | Medium | Graceful degradation: unsigned offers accepted but flagged |
| Gas cost of on-chain PQ verification | Medium | Medium (Lux only) | Off-chain verification first, on-chain deferred to PQ-16 |
| Cross-impl incompatibility | Medium | High | Test vectors pinned, cross-impl CI (PQ-11) |
| Key management complexity | Medium | Medium | pq_keys.py abstraction, documented best practices |

### Contingency plan

If ML-DSA is broken before Phase 2 completes:
1. Immediately switch default to Hybrid ECDSA + SLH-DSA-128s.
2. Fast-track FN-DSA evaluation for Phase 3.
3. Issue security advisory for all agents using PQ-only mode.

## Dependencies

### External

| Dependency | Version | Role | Risk |
|------------|---------|------|------|
| liboqs-python | >=0.10 | PQ signature operations | Must keep updated with NIST releases |
| eth-account | >=0.11 | ECDSA half of hybrid | Stable |
| py-ecc | >=6.0 | secp256k1 operations | Stable |

### Internal

| Item | Owner | Deadline |
|------|-------|----------|
| switchboard v1.1 release | @kcolbchain | 2026 Q3 |
| ZAP wire format extension | @Pattermesh | 2026 Q3 |
| x402 middleware release | @abhicris | 2026 Q3 |

## Glossary

| Term | Definition |
|------|------------|
| ML-DSA | Module-Lattice-Based Digital Signature Algorithm (FIPS 204) |
| SLH-DSA | Stateless Hash-Based Digital Signature Algorithm (FIPS 205) |
| FN-DSA | Fiat-Shamir with Aborts Digital Signature Algorithm (FIPS 206, draft) |
| NIST level | Security category defined by NIST PQC standardization process |
| Hybrid mode | Using both classical (ECDSA) and PQ (ML-DSA) signatures on the same payload |
| XOF | Extendable-Output Function (e.g., SHAKE-256) |
