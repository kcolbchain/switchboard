# Agent-to-Agent Payment Discovery — switchboard

**Status:** Draft v1
**Reference impl:** [`switchboard/discovery.py`](../switchboard/discovery.py)
**Tracks issue:** [#23 — A2A discovery profile schema](https://github.com/kcolbchain/switchboard/issues/23)

---

## 1. Goal

Before any payment, a paying agent needs to know three things about the
service agent: **who they are**, **where they accept funds**, and **what
schemes / rails they speak**. This spec defines a single JSON document
served at a well-known URL — `https://<agent>/.well-known/agent-payment.json` —
that answers those questions in one round-trip. The profile is advisory:
the authoritative price for any given call still comes from the 402 /
`x402.payment.required` response at request time.

## 2. Document location

```
GET https://<agent-host>/.well-known/agent-payment.json
```

The path constant is exported as `switchboard.discovery.DISCOVERY_PATH`.
Servers SHOULD respond with `Content-Type: application/json`. Clients MUST
accept `application/json` and any `application/*+json` variant.

## 3. Schema

Canonical structure (all fields lowercase snake_case, matching
`PaymentRequest` in [`agent-payment-protocol.md`](agent-payment-protocol.md)
§2):

| field                  | type           | required | notes                                                                  |
| ---------------------- | -------------- | -------- | ---------------------------------------------------------------------- |
| `version`              | string         | yes      | Profile schema version. Current = `"1"`. Mismatched versions MUST be rejected. |
| `agent`                | object         | yes      | Stable identifier object. See §3.1.                                    |
| `rails`                | array<string>  | yes      | Supported transports. Known values: `"http-402"`, `"a2a-x402"`, `"zap-binary"`. |
| `accepts`              | array<object>  | yes      | Non-empty list of payment endpoints. See §3.2.                         |
| `updated_at`           | string         | yes      | ISO 8601 timestamp of last profile change.                             |
| `prices`               | object         | no       | Free-form rate hints. Authoritative price still comes from the 402 response. |

### 3.1 `agent`

| field   | type   | required | notes                                          |
| ------- | ------ | -------- | ---------------------------------------------- |
| `name`  | string | yes      | Human-readable identifier (e.g. `"image-gen-bot"`). |
| `did`   | string | no       | Optional W3C DID for the agent.                |

### 3.2 `accepts[]`

| field             | type          | required | notes                                                                              |
| ----------------- | ------------- | -------- | ---------------------------------------------------------------------------------- |
| `network`         | string        | yes      | Network identifier. Same vocabulary as the A2A x402 adapter (`"base"`, `"ethereum"`, `"eip155:<id>"`, …). |
| `asset`           | string        | yes      | Asset identifier. EVM token contract address, or a symbolic name (e.g. `"ETH"`).   |
| `pay_to`          | string        | yes      | Recipient address on `network`.                                                    |
| `schemes`         | array<string> | yes      | Non-empty subset of `{"exact", "escrow", "streaming"}` (see `PaymentScheme`).      |
| `escrow_contract` | string        | no       | Escrow contract address on `network`. Only meaningful if `"escrow"` is in `schemes`. |

## 4. Wire encoding

```python
json.dumps(d, sort_keys=True, separators=(',', ':'))
```

`sort_keys=True` and the tight separators give a byte-for-byte canonical
encoding. Any payload that round-trips through `from_dict()` → `to_json()`
produces identical bytes — same canonicalization rule as `PaymentRequest`.

## 5. Example

A minimal profile for an image-generation agent accepting USDC on Base, both
direct (`exact`) and escrowed:

```json
{
  "version": "1",
  "agent": {"name": "image-gen-bot"},
  "rails": ["http-402", "a2a-x402"],
  "accepts": [
    {
      "network": "base",
      "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bda02913",
      "pay_to": "0xServerWalletAddressHere",
      "schemes": ["exact", "escrow"],
      "escrow_contract": "0xEscrowContractAddressHere"
    }
  ],
  "updated_at": "2026-05-16T12:00:00Z"
}
```

A paying agent fetches it like this:

```python
from switchboard.discovery import fetch_profile

profile = await fetch_profile("https://image-gen-bot.example.com")
for endpoint in profile.accepts:
    print(endpoint.network, endpoint.asset, endpoint.pay_to, endpoint.schemes)
```

## 6. A2A x402 mapping

Switchboard's discovery schema is snake_case for internal consistency. The
A2A x402 v0.1 `accepts[]` requirement shape is camelCase (`payTo`,
`maxAmountRequired`, …). `switchboard.discovery.to_a2a_accepts(profile)`
projects a profile into one A2A x402 requirement per `(endpoint, scheme)`
pair. The `maxAmountRequired` field is intentionally **not** filled in —
discovery is advisory, and per-call pricing belongs in the 402 response.

## 7. Security considerations

- **No signing in v1.** The profile is fetched over TLS and trusted at the
  HTTP/DNS level only. There is no embedded signature, no
  `agent.did`-derived proof, and no on-chain registry lookup.
- **Verify on-chain after first use.** Treat the `pay_to` and
  `escrow_contract` addresses as hints. A payer SHOULD confirm — out of
  band, or from the first 402 response — that those addresses match what
  the service actually reports at payment time.
- **The profile is advisory.** Pricing in `prices` is a hint for budgeting.
  The authoritative `amount_wei` always comes from the 402 /
  `x402.payment.required` response at request time. A profile that lies
  about a price does not bind the merchant.
- **Cache carefully.** Servers SHOULD set sensible `Cache-Control` headers.
  Clients SHOULD respect `updated_at` and refetch when their cached profile
  is older than the merchant's stated freshness window.

## 8. Backwards compat

Any change that affects canonicalization (new optional field, type
narrowing, etc.) MUST bump `version`. Unknown `version` values MUST cause
the client to reject the profile rather than silently parse it. Clients
SHOULD NOT attempt forward-compat parsing — a stale client talking to a
new-schema server is exactly the case where misreading `pay_to` would lose
funds.

## 9. Future work

- **Signed profiles** — embed a JWS or detached signature so the profile
  can be cached / mirrored without losing authenticity. Open question:
  whose key signs (agent DID, on-chain key, operator key)?
- **CAIP-2 addressing** — replace the freeform `network` field with
  CAIP-2 (`eip155:8453`, `solana:mainnet-beta`, …). Tracks the same
  migration noted in [`agent-payment-protocol.md`](agent-payment-protocol.md)
  §7.
- **Rate negotiation handshake** — let payer and payee agree a price band
  ahead of time (e.g. for streaming MPP sessions). Currently the merchant
  unilaterally sets price in the 402 response.
