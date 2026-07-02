# Agent Wallet + Multi-Token Settlement — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:test-driven-development for every unit. Each unit is implemented test-first (write failing test → run → implement minimal → run → commit). Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build switchboard's multi-token settlement standard + a built-in agent wallet (session-key delegation, load-balancing router, fairness/access policy) + agent-facing surfaces (MCP, CLI, frontend, metrics), staged as tested PRs for @abhicris review.

**Architecture:** Two layers meeting at a token-negotiation handshake — a Solidity settlement floor (sibling `MultiTokenAgentEscrow`, approach A) and a Python agent wallet wrapping the existing `MPCWallet`, exposed to agents via MCP/CLI/web. See spec: `docs/agent-wallet-multitoken-settlement.md`.

**Tech Stack:** Solidity + Foundry; Python 3.11+ (pytest); existing `switchboard/` modules (`mpc_wallet`, `gas_budget`, `nonce_manager`, `adapters/lucidly`, `x402`, `mpp`); `web/` frontend conventions; MCP over stdio.

## Global Constraints

- Python **3.11+**; all new Python is typed and pytest-tested.
- Solidity contracts under `contracts/`, tested with **Foundry**; do **not** modify the shipped `AgentEscrow.sol` (approach A = new sibling file).
- **TDD mandatory** — no implementation code without a failing test first.
- Reuse existing modules; **DRY/YAGNI** — do not reinvent gas budgeting, nonce management, or DEX/liquidity (use `lucidly`).
- Commit identity: `Pattermesh <pattermesh@gmail.com>`. Frequent, small commits.
- Branch: `pattermesh/agent-wallet-multitoken-settlement`. Every unit → its own PR into `main`; **contract units (①②③) merge only after @abhicris signs off §3.3**.
- Featured partner tokens in allowlists/demos/dashboard: **LUX, ZOO** + other kcolbchain partners.
- Solana is **out of scope** (separate non-EVM track).

**Plan format note:** at 20 units across 4 languages this plan is specified at **unit granularity** — each unit lists exact files, interface signatures, the test list, and acceptance criteria. The implementing subagent performs the per-step TDD cycle (test→fail→impl→pass→commit) using the test-driven-development skill. This is the shared contract that keeps parallel work mergeable.

---

## Wave 1 — Settlement foundation (build first; everything depends on it)

### Unit ⑤ — `IAgentEscrow` interface + oracle wiring
- **Files:** Create `contracts/IAgentEscrow.sol`; Modify `contracts/IOracleAggregator.sol` (confirm price-quote signature).
- **Produces:** `interface IAgentEscrow { function createPayment(...) ; confirmPayment ; releaseByAttestation ; requestRefund ; cancelPayment ; getPayment ; }` with a `token` field in `Payment`. `IOracleAggregator.quote(tokenIn, tokenOut, amountIn) returns (amountOut, staleness)`.
- **Tests (Foundry):** interface compiles; a mock implementing it satisfies the ABI the Python client expects.
- **Acceptance:** both existing `AgentEscrow` (ETH) and the new multi-token contract can declare `is IAgentEscrow`.

### Unit ① — `MultiTokenAgentEscrow.sol` (approach A, sibling)
- **Files:** Create `contracts/MultiTokenAgentEscrow.sol`; Test `contracts/test/MultiTokenAgentEscrow.t.sol`.
- **Consumes:** `IAgentEscrow`.
- **Produces:** escrow lifecycle parameterized by `address token` (`address(0)` = ETH via `msg.value`; ERC-20 via `transferFrom`/`transfer`); balance-delta accounting for fee-on-transfer tokens; per-token `allowlist` flag; all events carry `token`.
- **Tests:** ETH-profile parity with `AgentEscrow`; ERC-20 happy path; fee-on-transfer via balance delta; timeout/refund/challenge/cancel unchanged; non-allowlisted token rejected.
- **Acceptance:** full lifecycle green for ETH + a standard ERC-20 + a fee-on-transfer mock.

### Unit ② — `SwapSettlementAdapter.sol` (opt-in)
- **Files:** Create `contracts/SwapSettlementAdapter.sol`; Test `contracts/test/SwapSettlementAdapter.t.sol`.
- **Consumes:** `IAgentEscrow`, `IOracleAggregator`, lucidly/DEX router interface.
- **Produces:** `settleWithSwap(requestId, tokenOut, maxSlippageBps)` — pulls the held token from escrow on release, swaps → `tokenOut`, forwards to payee; reverts the whole release if realized slippage > `maxSlippageBps`.
- **Tests:** successful X→Y swap-at-release; slippage-exceeded revert leaves escrow funded; oracle-stale rejection.
- **Acceptance:** payer-USDC → payee-DAI settles within bound; out-of-bound reverts atomically.

### Unit ③ — Foundry test suite hardening + CI
- **Files:** `contracts/test/*`; Modify `.github/workflows/*` (Foundry job matrix incl. new contracts); mocks under `contracts/mocks/`.
- **Acceptance:** `forge test` green; CI runs the multi-token + swap suites.

### Unit ⑥ — Token negotiation in `payment_protocol.py` (v1.2)
- **Files:** Modify `src/payment_protocol.py`; Test `tests/test_payment_protocol_negotiation.py`; update `docs/agent-payment-protocol.md` → v1.2.
- **Produces:** `negotiate_settlement_token(payer_offer, payee_accepts) -> SettlementToken | None`; `PaymentRequest.settlement_token`; `currency` retained as ETH-profile alias.
- **Tests:** deterministic pick (same inputs→same token); no-common-token → `None`; v1.1 payloads still parse.
- **Acceptance:** negotiation is pure/deterministic and back-compatible.

### Unit ⑦ — x402 `accepts[]` multi-token envelope
- **Files:** Modify `switchboard/x402/server.py`, `switchboard/x402_middleware.py`; Test `tests/test_x402_multitoken.py`.
- **Consumes:** ⑥.
- **Produces:** `accepts[]` entries gain `{chain_id, token, min_amount, rank}`; middleware advertises accepted tokens and validates the negotiated `settlement_token`.
- **Acceptance:** a 402 response lists multiple accepted tokens; a payment in a non-accepted token is rejected.

### Unit ④ — ERC draft + magicians post
- **Files:** Create `eips/draft-multitoken-a2a-escrow.md`, `eips/magicians-post-multitoken.md`.
- **Acceptance:** draft passes EIP frontmatter lint; cites native-ETH EIP as a profile; no code dependency (can start immediately).

---

## Wave 2 — Agent wallet core (depends on Wave 1 interfaces ⑤⑥)

### Unit ⑧ — `AgentWallet` + `Treasury`
- **Files:** Create `switchboard/agent_wallet.py`, `switchboard/treasury.py`; Tests `tests/test_agent_wallet.py`, `tests/test_treasury.py`.
- **Consumes:** `MPCWallet`.
- **Produces:** `AgentWallet(mpc: MPCWallet)`; `Treasury.balance(chain_id, token)`, `.spendable(...)`, `.credit/debit`; `AgentWallet.pay(request) -> receipt` entrypoint (router-driven).
- **Tests:** balance tracking per (chain,token); spendable respects reserves; `pay` routes through the Router.
- **Acceptance:** wallet reports multi-token balances and executes a mocked same-token payment end to end.

### Unit ⑨ — Session keys + `SpendPolicy`
- **Files:** Create `switchboard/delegation.py`; Test `tests/test_delegation.py`.
- **Consumes:** ⑧, `gas_budget`/`gas_manager`.
- **Produces:** `grant(agent_id, SpendPolicy) -> SessionKey`; `revoke(session_key)`; `SpendPolicy(token_allowlist, per_tx_cap, daily_cap, expires_at, allowed_counterparties)`; wallet enforces policy before co-signing.
- **Tests:** cap/expiry/allowlist/counterparty enforcement; revocation blocks further signing; daily cap via gas_budget.
- **Acceptance:** an over-cap or expired session key cannot spend.

### Unit ⑲ — Fairness + agent access policy engine
- **Files:** Create `switchboard/access_policy.py`; Test `tests/test_access_policy.py`.
- **Consumes:** ⑨.
- **Produces:** per-agent access **tiers**, **rate-fairness** (token-bucket so one agent can't starve others), and **contract-compliance** checks (refuse actions that would violate escrow terms). `check(agent_id, action) -> Decision`.
- **Tests:** fairness under contention (N agents, bounded shares); tier limits; compliance refusal on an invalid escrow action.
- **Acceptance:** concurrent agents get fair, bounded access; non-compliant actions are refused with a typed reason.

### Unit ⑩ — `TokenSelector` · Unit ⑪ — `RailSelector` · Unit ⑫ — `FleetBalancer` · Unit ⑬ — `Rebalancer`
- **Files:** Create `switchboard/router/` (`__init__.py`, `token_selector.py`, `rail_selector.py`, `fleet_balancer.py`, `rebalancer.py`); Tests `tests/router/test_*.py`.
- **Consumes:** ⑧ (all); `nonce_manager` (⑫); swap adapter ② (⑬).
- **Produces:** `Router.route(request) -> Plan(token, rail, wallet)` composing four pluggable strategies with the interfaces `select_token`, `select_rail`, `select_wallet`, `rebalance_targets`.
- **Tests:** each strategy in isolation — token by balance/fee/slippage; rail by amount (x402/escrow/mpp); fleet spreads nonces without collision; rebalancer moves toward target allocation.
- **Acceptance:** given a treasury + request, Router returns a valid Plan; strategies independently unit-tested.

---

## Wave 3 — Agent surfaces & product (depends on Waves 1–2)

### Unit ⑮ — MCP server (connect-your-agent surface)
- **Files:** Create `switchboard/mcp_server.py`; Test `tests/test_mcp_server.py`.
- **Consumes:** ⑧⑨⑲.
- **Produces:** MCP tools over stdio: `wallet_balance`, `create_escrow`, `confirm_payment`, `request_refund`, `pay`, `policy_status`, `escrow_metrics`. Each maps to `AgentWallet`/escrow, gated by session key + access policy.
- **Tests:** each tool round-trips against a mocked wallet; policy-denied calls return structured errors.
- **Acceptance:** an MCP client can list tools and execute a full escrow payment within policy.

### Unit ⑯ — CLI
- **Files:** Create `switchboard/cli.py`; Test `tests/test_cli.py`; register console-script in `pyproject.toml`.
- **Produces:** `switchboard wallet balance|grant|revoke`, `switchboard escrow create|confirm|refund|status`, `switchboard metrics`.
- **Acceptance:** CLI drives the same core as MCP; `--help` documents every command; smoke tests green.

### Unit ⑰ — Tool registry + wiring
- **Files:** Modify `switchboard/registry.json`; Create `switchboard/tools.py`; Test `tests/test_tools_registry.py`.
- **Produces:** a registry agents query to discover callable tools + their policies; single source of truth shared by MCP and CLI.
- **Acceptance:** registry lists tools with schemas; MCP/CLI both read from it (DRY).

### Unit ⑱ — Frontend onboarding
- **Files:** `web/` per existing conventions (login view, connect-API-key view, connect-agent view, 3-step "operate the wallet" walkthrough). Build with the `frontend-design` skill.
- **Consumes:** ⑮⑯⑳.
- **Produces:** login/session; paste+store API key (scoped, never logged); connect-agent flow showing the MCP endpoint + minimal instructions; links into the dashboard.
- **Acceptance:** a user can sign in, connect a key, connect an agent, and reach the metrics dashboard; responsive; matches `web/` style.

### Unit ⑳ — Escrow-fulfilment metrics + polling dashboard
- **Files:** Create `switchboard/metrics.py`; `web/` dashboard panel; Tests `tests/test_metrics.py`.
- **Consumes:** ⑧ + contract events.
- **Produces:** polling of escrow fulfilment (fill rate, time-to-release, timeout rate, refund rate, challenge rate) + wallet ops (spend by token/rail, policy denials, fleet health); a live dashboard panel in `web/`.
- **Acceptance:** metrics computed from event/state fixtures; dashboard renders the panels and refreshes.

### Unit ⑭ — Multi-token 2-agent demo + explorer update
- **Files:** `examples/` (extend the live 2-agent ETH demo → 2-token), `web/` explorer.
- **Acceptance:** watchable demo: payer holds USDC, payee wants DAI (or LUX/ZOO), settled via the adapter; explorer shows the multi-token flow.

---

## Self-Review

- **Spec coverage:** every spec §3–§4 + §10 unit maps to a plan unit (①–⑳). ✓
- **Placeholders:** none — each unit has files, interface, tests, acceptance.
- **Type consistency:** `IAgentEscrow`, `SettlementToken`, `SpendPolicy`, `SessionKey`, `Router.route→Plan(token, rail, wallet)` used consistently across units.
- **Open dependency:** contract units ①②③ carry the §3.3 approach-A provisional and merge only after abhicris signs off; all other units are independent of that decision.
