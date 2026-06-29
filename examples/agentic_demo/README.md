# Agentic Payments Demo — A2A pay + escrow settle + SafeSwap route

A runnable scenario where **Agent A pays Agent B for work** via the Switchboard
x402 middleware + on-chain escrow, settles on delivery, then **Agent B routes the
received token through [SafeSwap](#safeswap)** to rebalance into a target asset.

Everything runs **offline** — no RPC node, no live SafeSwap — by driving the real
`switchboard` package surface against an in-memory chain and an in-process SafeSwap
mock. Swapping in a live RPC `PaymentClient` and `SafeSwapClient(base_url=...)`
needs no changes to the scenario code.

## Run it

```bash
PYTHONPATH=. python examples/agentic_demo/run.py
PYTHONPATH=. python examples/agentic_demo/run.py --swap-to LUX --price 8 --json
```

Exit code is `0` only when **both** `402 offer -> pay -> settle` and the
**agentic swap** succeed.

## The flow

```
Agent A                       Agent B (paid endpoint)            SafeSwap
   │  GET /inference  ───────────▶                                   │
   │  ◀───────────  402 + x402 PaymentOffer (escrow, 5 USDC)         │
   │  validate offer (cap / allowlist / gas budget)                  │
   │  lock 5 USDC in AgentEscrow  ──▶ [Locked]                       │
   │  ◀───────────  200 OK + deliverable                             │
   │  confirmPayment()  ──────────▶ escrow [Released] → B paid       │
   │                                  route 5 USDC ─────────────────▶│ quote
   │                                  ◀──────────────  best route + amountOut
   │                                  execute ─────────────────────▶ │ settle
   │                                  ◀──────────────  SwapReceipt    │
```

1. **402 offer** — `AgentBEndpoint.offer()` returns a real
   `switchboard.x402_middleware.PaymentOffer` with the **escrow** scheme.
2. **validate + pay** — Agent A's `X402Middleware._validate_offer()` enforces the
   payment cap, recipient allowlist, and gas budget, then `_pay_onchain()` locks
   funds via the escrow `create_payment()` path.
3. **deliver** — Agent B serves the work against the payment proof.
4. **settle** — Agent A `confirm_payment()` → escrow transitions
   `Locked → Released`, crediting Agent B.
5. **agentic swap** — Agent B routes the received USDC through `SafeSwapClient`
   (`quote` → `execute`) into ETH/LUX, getting a best-execution route + receipt.

## SafeSwap

`safeswap.py` is a tiny client against SafeSwap's orchestrator HTTP API
(`/v1/quote`, `/v1/execute`). It ships with `MockSafeSwapOrchestrator`, an
in-process transport with deterministic pricing so the demo and tests run with no
network. Point `SafeSwapClient(base_url=...)` at the live orchestrator for real
routing.

## Files

| File | Role |
|------|------|
| `run.py` | CLI entrypoint (`--swap-to`, `--price`, `--json`) |
| `scenario.py` | the orchestration + `BudgetGuard` + `AgentBEndpoint` |
| `onchain.py` | `MockChain` ledger + escrow + `MockPaymentClient` (PaymentClient surface) |
| `safeswap.py` | `SafeSwapClient` + `MockSafeSwapOrchestrator` |

## Test

```bash
PYTHONPATH=. python -m pytest tests/test_agentic_demo.py -q
```

Asserts: the 402 offer carries the escrow scheme + price, the escrow ends
`Released` (not just `Locked`), funds move payer → escrow → payee, the SafeSwap
orchestrator is genuinely called (`quote` then `execute`), and the swap routes
with a non-empty venue path and positive output.
