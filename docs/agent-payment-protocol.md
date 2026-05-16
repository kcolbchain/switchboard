# Agent-to-Agent Payment Protocol — switchboard

**Status:** Draft v1.0
**Reference impl:** [`src/payment_protocol.py`](../src/payment_protocol.py)
**On-chain side:** [`contracts/AgentEscrow.sol`](../contracts/AgentEscrow.sol)
**Tracks issue:** [#2 — Add agent-to-agent payment protocol](https://github.com/kcolbchain/switchboard/issues/2)

---

## 1. Goal

Let two autonomous agents settle a payment for work without a human in the loop, with three properties:

1. **Payee gets paid only if the work is accepted** (or after a timeout the payer can't grief past).
2. **Payer can recover funds** if the payee disappears or fails to deliver.
3. **The protocol is portable** across EVM-compatible chains.

The wire format is JSON; the settlement is an on-chain escrow.

## 2. Message format — `PaymentRequest`

Canonical structure (all fields lowercase snake_case):

| field                       | type    | required | notes                                                      |
| --------------------------- | ------- | -------- | ---------------------------------------------------------- |
| `version`                   | string  | yes      | Protocol version. Current = `"1.0"`.                       |
| `request_id`                | string  | yes      | UUIDv4 chosen by payer. Used as on-chain key.              |
| `payer`                     | string  | yes      | Checksummed EVM address.                                   |
| `payee`                     | string  | yes      | Checksummed EVM address.                                   |
| `amount_wei`                | int     | yes      | Amount in smallest denomination of `currency`.             |
| `amount_usd`                | string  | no       | Optional USD equivalent at request time, decimal as string.|
| `currency`                  | string  | yes      | `"ETH"`, `"USDC"`, `"USDT"`, etc.                          |
| `chain_id`                  | int     | yes      | EIP-155 chain ID. `1` = mainnet, `8453` = Base, etc.       |
| `timeout_blocks`            | int     | yes      | Blocks after `created_at` before payee can no longer claim.|
| `challenge_period_blocks`   | int     | yes      | Blocks after `timeout_blocks` before payer can refund.     |
| `description`               | string  | no       | Free-form human-readable.                                  |
| `metadata`                  | object  | no       | Arbitrary JSON object — protocol-opaque.                   |
| `created_at`                | float   | yes      | Unix epoch seconds, set by payer at request time.          |
| `status`                    | string  | yes      | Local mirror of on-chain state. See §4.                    |

### 2.1 Wire encoding

```python
json.dumps(d, sort_keys=True, separators=(',', ':'))
```

`sort_keys=True` and the tight separators give a byte-for-byte canonical encoding. Any payload that round-trips through `from_dict()` → `to_json()` produces identical bytes.

### 2.2 Content hash

```
content_hash = "0x" + sha256(canonical_json).hexdigest()
```

`content_hash` is computed over **all fields except `created_at` and `status`**. Rationale: those two are instance-time / mutable. Two `PaymentRequest` objects representing the same payment intent (same `request_id`, payer, payee, amount, terms, metadata) MUST produce the same `content_hash` regardless of when they were instantiated or what their current local status is.

For replay protection, agents SHOULD use `request_id` (UUID), not `content_hash`.

## 3. Escrow contract — `AgentEscrow.sol`

The `payer` calls `createPayment(request_id, payee, timeout_blocks, challenge_period)` with `msg.value = amount_wei`. Funds are held by the contract until one of:

- `confirmPayment(request_id)` — released to `payee`. Callable only by `payer`.
- `requestRefund(request_id)` — returned to `payer`. Callable only by `payer` AND only after `created_at + timeout_blocks + challenge_period`.
- `cancelPayment(request_id)` — returned to `payer`. Callable only by `payer` AND only while still in `LOCKED`.

Events emitted: `PaymentCreated`, `PaymentReleased`, `PaymentRefunded`. ABI mirrored in [`src/payment_protocol.py`](../src/payment_protocol.py) → `ESCROW_ABI`.

## 4. State machine

```
                       createPayment()
                  ┌────────────────────┐
                  ▼                    │
         ┌────────────────┐            │
   ┌────▶│    PENDING     │            │
   │     │  (request only)│            │
   │     └────────┬───────┘            │
   │              │ funds locked       │
   │              ▼                    │
   │     ┌────────────────┐            │
   │     │     LOCKED     │            │
   │     └────────┬───────┘            │
   │              │                    │
   │              ├──── confirmPayment()──▶ ┌───────────┐
   │              │                         │  RELEASED  │
   │              │                         └───────────┘
   │              │
   │              ├──── cancelPayment() ──▶ ┌────────────┐
   │              │                         │  CANCELLED │
   │              │                         └────────────┘
   │              │
   │              │ timeout_blocks +
   │              │ challenge_period
   │              │ elapsed
   │              ▼
   │     ┌────────────────┐  requestRefund()  ┌────────────┐
   └─────│    EXPIRED     │ ─────────────────▶│  REFUNDED  │
         └────────────────┘                   └────────────┘
```

| state      | transitions to                                    | trigger                                               |
| ---------- | ------------------------------------------------- | ----------------------------------------------------- |
| PENDING    | LOCKED                                            | `createPayment()` succeeds on-chain                   |
| LOCKED     | RELEASED, CANCELLED, EXPIRED                      | `confirmPayment()`, `cancelPayment()`, time elapses   |
| EXPIRED    | REFUNDED                                          | `requestRefund()` after challenge period              |
| RELEASED, REFUNDED, CANCELLED | (terminal)                            | —                                                     |

`PaymentState` enum mirrors this in `src/payment_protocol.py`.

## 5. Lifecycle (happy path)

```
Agent A (payer)                Escrow                  Agent B (payee)
     │                            │                            │
     │   PaymentRequest (JSON) ───┼──────────────────────────▶ │
     │                            │                            │
     │   createPayment{value}     │                            │
     │ ───────────────────────────▶                            │
     │                            │ funds locked               │
     │                            │   PaymentCreated ────────▶ │
     │                            │                            │
     │                            │       (B does the work)    │
     │                            │ ◀───────────────────────── │
     │                            │      delivery / proof      │
     │                            │                            │
     │   confirmPayment           │                            │
     │ ───────────────────────────▶                            │
     │                            │ funds released             │
     │                            │   PaymentReleased ───────▶ │
     │                            │                            │
```

## 6. Lifecycle (refund path)

```
Agent A (payer)                Escrow                  Agent B (payee)
     │                            │                            │
     │   createPayment{value}     │                            │
     │ ───────────────────────────▶                            │
     │                            │ funds locked               │
     │                            │                            │
     │                            │  (B disappears,            │
     │                            │   never confirms)          │
     │                            │                            │
     │                            │  timeout_blocks elapse     │
     │                            │  state → EXPIRED            │
     │                            │                            │
     │                            │  challenge_period elapses  │
     │                            │                            │
     │   requestRefund            │                            │
     │ ───────────────────────────▶                            │
     │                            │ funds returned             │
     │                            │   PaymentRefunded          │
     │ ◀────────────────────────── │                            │
```

## 7. Multi-chain considerations

`chain_id` (EIP-155) MUST be set in every `PaymentRequest`. Receivers MUST reject any request whose `chain_id` doesn't match the chain their escrow is deployed on.

`currency` is independent of `chain_id` — e.g. USDC on Base (`chain_id=8453`) vs USDC on Ethereum mainnet (`chain_id=1`) are different settlement instruments. Agents MUST explicitly set both.

For non-EVM chains (Solana, Stellar, etc.) the wire format is reusable but the escrow contract is chain-specific and outside this protocol's scope. Future versions will extend `chain_id` to a CAIP-2 string (e.g. `"eip155:8453"`, `"solana:mainnet"`).

## 8. Backwards-compat notes (vs. earlier drafts)

- `content_hash` previously included `created_at`, which made the hash time-sensitive and broke determinism tests. v1.0 excludes `created_at` and `status` from the hash. Agents written against pre-v1.0 hashes MUST recompute on upgrade.
- `parse_wei("N wei")` previously returned `N × 10^18` due to a case-mismatch in the unit dictionary. v1.0 fixes this to return `N` exactly. Audit any internal call sites that relied on the broken behavior.

## 9. Test vectors

Frozen fixtures live at [`tests/protocol_vectors/payment_request.v1.json`](../tests/protocol_vectors/payment_request.v1.json). They pin the canonicalization rule + `content_hash` for three representative payloads:

| Fixture | Exercises |
|---|---|
| `fixture-01-minimal` | Only required fields. No `amount_usd`, no `description`, no `metadata`. |
| `fixture-02-full-metadata` | Optional `amount_usd` Decimal, free-form `description`, nested `metadata` dict with arrays. |
| `fixture-03-unicode-boundary` | Emoji, quote, backslash, newline in `description`; mainnet `chain_id=1`; `amount_wei` at 250 ETH magnitude. |

Each fixture records:

- `input` — the dict the implementation receives.
- `wire_canonical_bytes` — the exact byte sequence `to_json()` MUST produce.
- `hash_input_canonical_bytes` — the same canonicalization with `created_at` and `status` removed (the bytes that get fed to sha256).
- `content_hash_sha256` — the resulting `0x`-prefixed sha256 hex digest.

Other-language implementations of this protocol can pin against the same JSON file and assert the same hashes. The Python conformance test lives at [`tests/test_protocol_vectors.py`](../tests/test_protocol_vectors.py) and runs under `pytest`.

Any change that affects the canonicalization (e.g. adding a new optional field, changing how `Decimal` is stringified, switching JSON library to one with different sorting semantics) MUST update the fixtures **and** the spec's protocol version. A breaking-change to canonicalization without a version bump is a silent fork.

## 10. Future work


- **CAIP-2 chain identifiers** for non-EVM networks (issue: TBD).
- **MPP session adapter** for high-frequency micro-payments under a budget cap (tracked in [#17](https://github.com/kcolbchain/switchboard/issues/17)).
- **A2A discovery** — `.well-known/agent-payment.json` profile schema. Reference impl: `switchboard/discovery.py`. Spec: [agent-payment-discovery.md](agent-payment-discovery.md).
