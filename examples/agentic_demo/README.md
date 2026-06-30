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

## Watch it live (browser)

The same orchestration, made **watchable** — two agents, the x402 `402` offer, and
the escrow `lock → release` animated step by step on the **mock chain**:

```bash
PYTHONPATH=. python examples/agentic_demo/server.py
# then open http://127.0.0.1:8402/  (or pass --open)
```

> **SIMULATED / MOCK CHAIN — not a live network.** No real ETH, no RPC, no funds;
> synthetic agents only. The page replays the *real* `run_scenario` output (real
> escrow state, real `X402Middleware` spend summary, real SafeSwap receipt) — it
> does not fake the numbers. `scenario.py` / `onchain.py` / `safeswap.py` are
> unchanged; `observable.py` records the timeline and `server.py` serves it over
> the JSON API in [`DEMO.md`](DEMO.md). _Live demo by Pattermesh (Patty /
> P. Sundaram)_ on **kcolbchain/switchboard** (Abhishek Krishna / @abhicris leads).

```
POST /api/demo/run    → run the scenario, return the ordered timeline + summary
GET  /api/demo/state  → replay the last run (or a fresh seeded one)
GET  /healthz         → {"ok": true}
```

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
| `observable.py` | wraps `run_scenario` into an ordered, render-ready **timeline** (deterministic) |
| `server.py` | stdlib HTTP server: the `/api/demo/*` JSON API + the live page |
| `web/` | canonical page sources (`index.html`, `demo.css`, `view.js`, `demo.js`); `build.mjs` inlines them to the self-contained `demo.html` the server serves |
| `DEMO.md` | the fixed design contract (event model, step ids, HTTP API) |

## Test

```bash
# scenario + the live (observable / server) layer
PYTHONPATH=. python -m pytest tests/test_agentic_demo.py examples/agentic_demo/ -q
# the page's pure view helpers + the demo.html build-sync guard
node --test examples/agentic_demo/web/demo.test.mjs
```

The scenario tests assert: the 402 offer carries the escrow scheme + price, the
escrow ends `Released` (not just `Locked`), funds move payer → escrow → payee, the
SafeSwap orchestrator is genuinely called (`quote` then `execute`), and the swap
routes with a non-empty venue path and positive output. The live-layer tests
(`examples/agentic_demo/test_live.py`) additionally pin the timeline order,
determinism (same seed ⇒ byte-identical), and the HTTP API envelope.
