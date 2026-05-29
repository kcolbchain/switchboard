# switchboard

**Programmable payments for AI agents.** One Python library, one contract suite, and one binary wire format that lets agents pay each other for work — over HTTP/402, on-chain escrow, multi-party micropayments, or stablecoin rails — without a human in the loop.

[![tests](https://img.shields.io/badge/tests-passing-brightgreen)](#test-results) [![python](https://img.shields.io/badge/python-3.11+-blue)](https://www.python.org/) [![status](https://img.shields.io/badge/status-active-success)](https://github.com/kcolbchain/switchboard) [![org](https://img.shields.io/badge/org-kcolbchain-7c3aed)](https://kcolbchain.com)

---

## Why this exists

Every AI agent that wants to call a paid API or hire another agent today has to roll its own payment plumbing — JSON over HTTP, Stripe meters, custom RPC paywalls, ad-hoc ETH transfers. That's fine for one app. It collapses the moment two agents from different teams need to settle on the fly.

`switchboard` is the **shared payment substrate**. Agents speak whichever rail fits the job, the protocols are auditable, and the implementations are open source.

```
┌──────────────┐    402 + accepts: x402            ┌──────────────┐
│   Agent A    │ ───────────────────────────────▶ │   Agent B    │
│  (caller)    │ ◀─────────────────────────────── │  (provider)  │
└──────┬───────┘    X-PAYMENT (signed, on-chain)  └──────┬───────┘
       │                                                  │
       │   ZAP binary wire (zero-alloc, ~10× smaller)    │
       └─────────── for high-volume A2A ──────────────────┘
                                │
                                ▼
                  ┌──────────────────────────┐
                  │  AgentEscrow (Solidity)  │  trustless settlement,
                  │  + gas budget + nonce    │  timeout, refund,
                  │    manager + x402 mw     │  challenge period
                  └──────────────────────────┘
```

Built and maintained by [kcolbchain](https://kcolbchain.com). Aligned with [Lux ZAP](https://github.com/luxfi/zap), [Coinbase x402](https://www.x402.org), and the [Hanzo MCP](https://github.com/hanzoai/mcp) `fetch` tool.

---

## What's in the box

| Module | Status | What it does |
|---|---|---|
| `switchboard/x402_middleware.py` | ✅ shipped (PR [#19](https://github.com/kcolbchain/switchboard/pull/19)) | Server-side **HTTP 402** middleware. Drop into FastAPI/Flask; gates routes behind on-chain payment. Verifies `X-PAYMENT` signatures and emits the standard `accepts[]` envelope. |
| `switchboard/zap_transport.py` | 🔄 in PR [#21](https://github.com/kcolbchain/switchboard/pull/21) | **Binary wire** for `PaymentOffer`/`PaymentProof` over [luxfi/zap](https://github.com/luxfi/zap). Zero-allocation, ~10× smaller than JSON. First production consumer of `zap_py`. |
| `switchboard/gas_tracker.py` + `gas_budget.py` | ✅ shipped (PR [#14](https://github.com/kcolbchain/switchboard/pull/14)) | **Hard gas budgets** (per-hour, per-day) so an agent can't get rugged by its own runaway loop. Closes the #1 footgun of autonomous on-chain agents. |
| `switchboard/nonce_manager.py` | ✅ shipped (PR [#11](https://github.com/kcolbchain/switchboard/pull/11)) | Client-side **nonce manager** with reorg protection. The thing every shipping agent eventually has to write. |
| `contracts/AgentEscrow.sol` + `src/payment_protocol.py` + [`docs/agent-payment-protocol.md`](docs/agent-payment-protocol.md) | ✅ shipped (PR [#8](https://github.com/kcolbchain/switchboard/pull/8); PQ spec update in progress) | **Trustless escrow** with timeout, challenge period, and mutual cancel. Solidity contract + Python client + CLI, plus protocol spec v1.1 with PQ signature envelope section. |
| `web/` | ✅ shipped (PR [#15](https://github.com/kcolbchain/switchboard/pull/15)) | Side-by-side **explorer** for x402 / MPP / AP2 / Circle / on-chain escrow. The clearest public comparison of agent-payment rails today. |
| `web/agents-demo.html` + [`SCENES.md`](web/SCENES.md) | ✅ shipped (PR [#39](https://github.com/kcolbchain/switchboard/pull/39), polish [#40](https://github.com/kcolbchain/switchboard/pull/40)) | Interactive **agent-payments lab** — 16 animated scenes covering x402 paywalls, escrow + refund, streaming MPP, AI compute auctions, HITL, treasury rebalance, signed oracle pulls, the PQ envelope proposal, taxi handover, café walk-by, food delivery, split bill, native ETH escrow, subscription, multi-city trip. Single static HTML, no build. |

---

## Try the lab

**Live:** [https://kcolbchain.github.io/switchboard/agents-demo.html](https://kcolbchain.github.io/switchboard/agents-demo.html)

Single-file canvas demo (no build, no deps). Hover any agent for an informatics-rich tooltip; toggle light/dark; step through events with the transport bar (`space` play/pause, `n` step, `r` restart); scrub simulation sliders. Renames map to real places: **Work-In-Progress** is Abhi's coffee shop in Siolim, **Eat Pray Love** is Tridib's restaurant.

The lab is intentionally fork-and-extend friendly: every scene is ~150 lines and follows the contract in [`web/SCENES.md`](web/SCENES.md). The pill bar already has a `+ Add your scene` slot that links to it. PRs welcome.

---

## 30-second quickstart

### 1. Make any HTTP route paid

```python
from fastapi import FastAPI
from switchboard.x402_middleware import X402Middleware

app = FastAPI()
app.add_middleware(
    X402Middleware,
    pay_to="0xYourTreasury…",
    asset="0x036CbD53842c5426634e7929541eC2318f3dCF7e",  # USDC on Base Sepolia
    network="base-sepolia",
    price="1000",  # 0.001 USDC per call
    paths=["/agent-only"],
)

@app.get("/agent-only")
def agent_only():
    return {"ok": True, "payload": "this cost the caller 0.001 USDC"}
```

Now any AI agent that hits `/agent-only` gets a standards-compliant 402 with an `accepts[]` envelope, signs an `X-PAYMENT` header, and is served. The same agent flow works through the [Hanzo MCP `fetch` tool](https://github.com/hanzoai/mcp/pull/5).

### 2. Cap an agent's burn rate

```python
from switchboard.gas_budget import GasBudget

budget = GasBudget(per_hour_eth="0.05", per_day_eth="0.50")

if not budget.can_spend(estimated_cost_wei):
    raise RuntimeError("agent over budget — pause loop")

# … submit tx …
budget.record(actual_cost_wei)
```

### 3. Settle work over an escrow

```python
from payment_protocol import PaymentClient

client = PaymentClient(private_key, escrow_address, rpc_url)

req = client.create_payment(
    payee="0xCounterparty…",
    amount_wei=10**16,            # 0.01 ETH
    timeout_blocks=100,
    challenge_period_blocks=10,
)
# … off-chain work happens …
client.confirm_payment(req.request_id)   # release funds
```

### 4. Native ETH escrow — no token, no approve, every EVM chain

The cheapest, most universal A2A primitive. No ERC-20 dance, no wrapped assets, no per-chain stablecoin contracts. Just `msg.value` directly into the escrow.

```python
from payment_protocol import PaymentClient

# Patty commissions Abhi for a one-shot inference job priced in ETH.
client = PaymentClient(patty_priv_key, escrow_address, rpc_url)

# Lock funds — createPayment{value: 0.05 ether}
req = client.create_payment(
    payee="0xAbhi…",
    amount_wei=5 * 10**16,        # 0.05 ETH
    timeout_blocks=100,
    challenge_period_blocks=10,
)
# Abhi delivers off-chain (URL / hash / event — verifiable by Patty)
client.confirm_payment(req.request_id)   # → escrow .call{value:}(payee)
```

Or from Solidity directly:

```solidity
// Direct call from any agent contract — no approve(), no transferFrom()
IAgentEscrow(escrow).createPayment{value: 0.05 ether}(
    "req-7f3e", abhi, /*timeout*/ 100, /*challenge*/ 10
);
```

Same code works on Lux, Base, Optimism, Arbitrum, Polygon, Ethereum mainnet — and any future EVM chain — without changing a line. ETH is the only asset universally available on every EVM chain without a token contract.

See scene 14 (`native-ETH escrow`) in [the lab](https://kcolbchain.github.io/switchboard/agents-demo.html) for an animated walkthrough. The primitive is on the EIP track — see [issue #50](https://github.com/kcolbchain/switchboard/issues/50) and the [draft EIP](https://github.com/kcolbchain/switchboard/blob/eip/native-eth-a2a-escrow/eips/draft-native-eth-a2a-escrow.md) on branch `eip/native-eth-a2a-escrow`.

### 5. Speak ZAP binary on a hot path

```python
from switchboard.zap_transport import encode_offer, decode_offer, PaymentOffer, PaymentScheme

offer = PaymentOffer(
    scheme=PaymentScheme.STREAMING,
    chain_id=84532,
    amount=10**6,                        # 1 USDC, uint256-be
    currency="USDC",
    recipient="0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    expires_at=None,                     # null sentinel
    description="model inference, 1k tokens",
    endpoint="https://provider.example/v1/infer",
    nonce="agt-7f3e",
)

wire = encode_offer(offer)               # zero-alloc bytes
roundtrip = decode_offer(wire)           # exact equality
```

---

## Comparison

|  | switchboard | raw x402 server | Stripe meter | Custom RPC paywall |
|---|---|---|---|---|
| HTTP/402 native | ✅ | ✅ | ❌ | ❌ |
| On-chain settlement | ✅ | depends | ❌ | ✅ |
| Trustless escrow | ✅ | ❌ | ❌ | ❌ |
| Multi-party (MPP) | 🛣️ tracked in [#17](https://github.com/kcolbchain/switchboard/issues/17) | ❌ | ❌ | ❌ |
| Binary A2A wire | ✅ ([#21](https://github.com/kcolbchain/switchboard/pull/21)) | ❌ | n/a | ❌ |
| Built-in gas budget | ✅ | ❌ | n/a | ❌ |
| Nonce + reorg safe | ✅ | n/a | n/a | maybe |
| Open source, MIT | ✅ | ✅ | ❌ | varies |

---

## Architecture

```
                                        ┌──────────────────────────┐
                                        │  AgentEscrow.sol         │
                                        │  (trustless settlement)  │
                                        └────────▲─────────────────┘
                                                 │
                                                 │ payment_protocol.py
                                                 │ (PaymentClient + CLI)
                                                 │
┌────────────────────────────────────────────────┴─────────────────────┐
│                                                                       │
│                   switchboard core (Python, MIT)                      │
│                                                                       │
│  ┌────────────────┐   ┌────────────────┐   ┌────────────────────┐    │
│  │ x402 middleware │   │ zap_transport  │   │ gas budget +       │    │
│  │ (HTTP 402)      │   │ (binary wire)  │   │ nonce manager      │    │
│  └────────────────┘   └────────────────┘   └────────────────────┘    │
│                                                                       │
└───────────┬─────────────────────────┬─────────────────────────┬───────┘
            │                         │                         │
            ▼                         ▼                         ▼
    ┌──────────────┐         ┌──────────────┐         ┌──────────────┐
    │ Coinbase     │         │ Lux ZAP      │         │ EVM L1/L2    │
    │ x402 spec    │         │ (Go + Py)    │         │ (Base, Lux,  │
    │              │         │              │         │  OP, Eth)    │
    └──────────────┘         └──────────────┘         └──────────────┘
```

---

## Use cases

- **Pay-per-call APIs.** Wrap any FastAPI/Flask endpoint behind `X402Middleware`, charge $0.0001/call in USDC, no Stripe account, no API keys, no humans.
- **Agent marketplaces.** Two MCP servers settle on `AgentEscrow` with timeout protection. Provider only paid when work confirms; payer can claim back if provider goes silent.
- **High-volume A2A.** Agents on the same Lux/Base subnet exchange `PaymentOffer`/`PaymentProof` over ZAP wire — zero parse-time allocation, ~10× smaller than JSON, schema-locked across Python ↔ Go.
- **Autonomous burn caps.** Long-running agents enforce per-hour / per-day spend limits before the on-chain submit, killing the runaway-loop class of bugs.

---

## Status & roadmap

- ✅ **Shipped:** AgentEscrow contract, payment client + CLI, nonce manager, gas budget, x402 middleware, web explorer.
- 🔄 **In flight:** [#34](https://github.com/kcolbchain/switchboard/issues/34) — protocol spec v1.1 / PQ signature envelope (§11 in `docs/agent-payment-protocol.md`).
- 🔄 **In flight:** [PR #21](https://github.com/kcolbchain/switchboard/pull/21) — ZAP binary wire encoding for `PaymentOffer` / `PaymentProof`. Validates `zap_py` end-to-end.
- 🛣️ **Next:** [#17](https://github.com/kcolbchain/switchboard/issues/17) MPP (multi-party micropayments) sessions, settlement-receipt format, Go interop fixtures with `luxfi/zap`.

Open issues with [`good first issue`](https://github.com/kcolbchain/switchboard/labels/good%20first%20issue) are real and welcome.

---

## Install

```bash
pip install switchboard-agents
# optional, for ZAP binary wire:
pip install 'switchboard-agents[zap]'
# or, pinning to the upstream luxfi/zap python bindings directly:
pip install 'luxfi-zap @ git+https://github.com/luxfi/zap@main#subdirectory=python'
```

Or, for local development:

```bash
git clone https://github.com/kcolbchain/switchboard && cd switchboard
pip install -e '.[dev]'
```

Python 3.11+. Tests: `pytest tests/`.

> The PyPI distribution name is `switchboard-agents` (bare `switchboard` on PyPI is an unrelated WSGI library). The Python import name remains `import switchboard`.

---

## Test results

```
$ pytest tests/ -v
tests/test_x402_middleware.py        ✅ 22 passed
tests/test_gas_budget.py             ✅  9 passed
tests/test_gas_tracker.py            ✅  9 passed
tests/test_payment_protocol.py       ✅ 10 passed
tests/test_nonce_manager.py          ✅ 12 passed
tests/test_zap_transport.py          ✅ 11 passed   (with luxfi-zap installed)
```

---

## License & contact

MIT. Built by [kcolbchain](https://kcolbchain.com) — [@abhicris](https://github.com/abhicris).

If you're building agent-payment infrastructure and want to compare notes — open an issue, or [research@kcolbchain.com](mailto:research@kcolbchain.com).

> **kcolb** = "block" reversed. We've been at this since 2015. The agent-payment rails are the part of crypto that finally has a real customer: AI.
