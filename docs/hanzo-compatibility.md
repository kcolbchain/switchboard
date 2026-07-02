# Hanzo.ai Compatibility

How a hanzo.ai agent connects to, pays through, and escrows via Switchboard.

---

## What compatibility means concretely

Switchboard speaks [x402](https://x402.org) — HTTP 402 Payment Required with a
`PaymentRequirements` envelope advertised in `X-Payment-Required` + `WWW-Authenticate: x402`.

Hanzo MCP agents use the `fetch` tool (HIP-0300 unified surface, defined in
`hanzoai/mcp:src/tools/unified/fetch.ts`) to make HTTP calls.  When that tool
sees a 402 it calls `parsePaymentRequired()`, which reads:

1. `body.accepts` — a top-level JSON array per the x402.org v2 spec.
2. `headers['www-authenticate']` — the `x402` challenge string.
3. Sends payment retries with an `X-PAYMENT` header carrying base64-encoded JSON.

**Switchboard's mismatch (fixed):** `X402Server.build_402_response()` puts
payment details under `body.payment_requirements`, not `body.accepts`.  The
Hanzo `fetch` tool therefore finds no top-level `accepts` and falls back to
`raw_body` (unhelpful).

**Fix delivered in `switchboard/adapters/hanzo.py`:**

- `normalize_402_body(body)` promotes `payment_requirements.accepts` to the
  top level, synthesising a single-entry list when the server hasn't configured
  multi-token accepts.
- `build_hanzo_402_body(requirements)` builds a response body that is
  simultaneously Hanzo-native (top-level `accepts`) and switchboard back-compat
  (`payment_requirements` preserved).

---

## How a Hanzo agent connects

### Step 1 — Create a wallet binding

```python
from switchboard.adapters.hanzo import HanzoAgentWallet
from switchboard.delegation import SpendPolicy
from datetime import datetime, timezone, timedelta

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bda02913"

hab = HanzoAgentWallet(
    hanzo_agent_id="admin/my-bot",        # Hanzo IAM identity (owner/name)
    policy=SpendPolicy(
        expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
        token_allowlist=[USDC_BASE],       # only USDC on Base
        per_tx_cap=50_000_000,             # 50 USDC / tx
        daily_cap=500_000_000,             # 500 USDC / day
    ),
)
```

`HanzoAgentWallet.__post_init__` does three things:

1. Creates an `AgentWallet` (wraps `MPCWallet` + `Treasury` + `EscrowClient`).
2. Wraps it in a `Delegation` layer.
3. Issues a scoped, revocable `SessionKey` via `Delegation.grant()`.

The `hanzo_agent_id` string (`"admin/my-bot"`) flows through as `agent_id` in
every `WalletOpEvent` emitted to the metrics / fairness engine.

### Step 2 — Fund the treasury

```python
hab.credit(chain_id=8453, token=USDC_BASE, amount=1_000_000_000)  # 1000 USDC
```

In production, treasury credits come from the on-chain deposit flow.  The
`credit()` helper is a test / top-up convenience.

### Step 3 — Pay

```python
receipt = hab.pay(
    chain_id=8453,
    token=USDC_BASE,
    amount=10_000_000,        # 10 USDC
    payee="0xServiceProvider",
)
print(receipt.tx_id, receipt.escrow_id)
```

The call path:

```
HanzoAgentWallet.pay()
  └─ Delegation.pay_with_key(session_key, request)
       ├─ SpendPolicy checks (revoked? expired? token? per_tx_cap? daily_cap?)
       └─ AgentWallet.pay(request, agent_id=hanzo_agent_id)
            ├─ AccessPolicy gate (if wired)
            ├─ Router (if wired → token / rail / wallet selection)
            ├─ Treasury debit
            ├─ MPCWallet.sign_and_send()
            └─ EscrowClient.create_payment() + release_payment()
```

### Step 4 — Escrow

```python
receipt = hab.escrow(
    chain_id=8453,
    token=USDC_BASE,
    amount=20_000_000,        # 20 USDC locked
    payee="0xTaskRunner",
    metadata={"task_id": "t-xyz"},
)
```

`escrow()` is a thin wrapper over `pay()` that adds `{"action": "escrow"}` to
the metadata, allowing Router and access-policy engines to distinguish escrow
flows from direct transfers.

---

## How the Hanzo fetch tool pays through Switchboard

When a Hanzo agent's `fetch` tool hits a Switchboard-protected endpoint:

```
Agent                Hanzo fetch tool            Switchboard server
  |                       |                             |
  |─── fetch(action="request", url=...) ──────────────>|
  |                       |                             |
  |                       |<── 402 + X-Payment-Required |
  |                       |    + WWW-Authenticate: x402  |
  |                       |    body: {payment_requirements:{...}} |
  |                       |                             |
  |   parsePaymentRequired()                            |
  |   → normalize_402_body() ← adapter fixes body.accepts
  |   → body.accepts found, payment_required surfaced   |
  |                       |                             |
  |   Agent inspects payment_required, decides to pay   |
  |   hab.pay(chain_id, token, amount, payee)           |
  |   receipt = {tx_id, escrow_id, ...}                 |
  |                       |                             |
  |   encode_hanzo_payment_header({txHash, chainId, payer, amount, nonce})
  |                       |                             |
  |─── fetch(action="request", payment=<b64-json>) ───>|
  |                       | X-PAYMENT: <base64>         |
  |                       |                             |
  |                       |    PaymentVerifier.verify() |
  |                       |<── 200 OK                   |
```

### Headers at each hop

| Direction | Header | Value |
|-----------|--------|-------|
| Server → Client (402) | `X-Payment-Required` | JSON `PaymentRequirements` |
| Server → Client (402) | `WWW-Authenticate` | `x402` |
| Client → Server (retry) | `X-PAYMENT` | base64(JSON payment payload) |
| Client → Server (retry) | `X-Payment-Proof` | JSON (switchboard legacy, also accepted) |

Switchboard's `X402Server.read_payment_header()` accepts both `X-PAYMENT` and
`X-Payment-Proof`, so legacy switchboard clients continue to work alongside
Hanzo agents.

---

## Caveats

### EscrowClient is a stub in this worktree

`AgentWallet` uses `_NoOpEscrow` by default — `create_payment()` returns
`"0xnoop"` and `release_payment()` always returns `True`.  Wire the real
`MultiTokenAgentEscrow` client (Unit ① / ③) when it lands:

```python
from switchboard.agent_wallet import AgentWallet
wallet = AgentWallet(mpc=mpc, treasury=treasury, escrow=real_escrow_client)
hab = HanzoAgentWallet(hanzo_agent_id="admin/my-bot", wallet=wallet)
```

### Router not wired by default

`HanzoAgentWallet` does not wire a `Router` by default.  To enable
multi-rail routing and `WalletOpEvent` emission from the Router layer, pass a
pre-configured `AgentWallet(router=...)`:

```python
from switchboard.router.router import Router
router = Router(...)
wallet = AgentWallet(mpc=mpc, treasury=treasury, router=router)
hab = HanzoAgentWallet(hanzo_agent_id="admin/my-bot", wallet=wallet)
```

### SessionKey expiry

A `SessionKey` is scoped to the `SpendPolicy.expires_at` datetime.  Create a
new `HanzoAgentWallet` to refresh a key (grants a new `SessionKey`):

```python
hab = HanzoAgentWallet(
    hanzo_agent_id="admin/my-bot",
    policy=SpendPolicy(expires_at=datetime.now(timezone.utc) + timedelta(hours=8)),
)
```

### Body normalization is server-side

`normalize_402_body()` is middleware for servers that know their callers are
Hanzo agents.  If you control the server, prefer `build_hanzo_402_body()` to
emit a Hanzo-native response directly.  If you don't control the server, call
`normalize_402_body()` on the client side after receiving the 402 body.

### x402 payment header encoding

Hanzo's `fetch` tool encodes the payment payload as
`base64(JSON.stringify(payload))` — the same encoding as the x402.org v2
spec's `X-PAYMENT` header.  Use `encode_hanzo_payment_header()` /
`decode_hanzo_payment_header()` for interop with Hanzo agents.

---

## Module reference

`switchboard/adapters/hanzo.py`:

| Symbol | Purpose |
|--------|---------|
| `normalize_402_body(body)` | Promote `payment_requirements.accepts` to top level for Hanzo fetch tool compatibility |
| `build_hanzo_402_body(requirements)` | Build a 402 body that is simultaneously Hanzo-native and switchboard back-compat |
| `encode_hanzo_payment_header(payload)` | Encode a dict as base64 JSON for the `X-PAYMENT` header |
| `decode_hanzo_payment_header(value)` | Decode an `X-PAYMENT` header into a dict |
| `read_payment_header(headers)` | Find the best payment header (`X-PAYMENT` > `X-Payment` > `X-Payment-Proof`) |
| `payment_requirements_from_hanzo_accepts(accepts)` | Convert Hanzo `accepts[]` to switchboard `PaymentRequirements` |
| `HanzoAgentWallet` | Bind a Hanzo IAM identity to a switchboard wallet + session key |

---

## Acknowledgments

Access to the `pattermesh`, `lux`, `hanzo`, and `zoo` org repositories that
made this integration possible was provided by **@zeekay**.