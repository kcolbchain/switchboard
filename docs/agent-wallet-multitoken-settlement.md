# Agent Wallet + Multi-Token Settlement

**Status:** Design v0.1
**Authors:** Pattermesh (@Pattermesh), kcolbchain (@kcolbchain)
**Open decision for:** @abhicris (see §3.3)
**Extends:** [`agent-payment-protocol.md`](./agent-payment-protocol.md) (→ v1.2), [`eips/draft-native-eth-a2a-escrow.md`](../eips/draft-native-eth-a2a-escrow.md)
**Orthogonal to:** [`multi-chain-settlement.md`](./multi-chain-settlement.md) (#59)

---

## 1. Goal

Take switchboard from "agents pay each other in native ETH escrow" to:

1. **A multi-token settlement standard** — switchboard can settle an agent-to-agent payment in *any* token, chosen by what the payer and payee actually want, with an opt-in swap path when they want different tokens.
2. **A built-in agent wallet** — a wallet agents connect to and are delegated scoped authority over ("take"), that load-balances spend across tokens / rails / a wallet fleet / target holdings, and transacts through switchboard by honoring the settlement standard.

These are two layers of one system: the wallet is the client brain; the standard is the trustless settlement floor. They meet at a token-negotiation handshake.

### 1.1 Non-goals

- Cross-chain messaging — already designed in `multi-chain-settlement.md` (#59). Multi-token is the **orthogonal axis**: this design settles in any token *on a given chain*; the chain×token matrix composes the two designs, it does not re-solve cross-chain.
- Fiat on/off-ramps.
- A new signing scheme — we build on the existing `MPCWallet` (Shamir SSS threshold ECDSA) and `nonce_manager`.

### 1.2 Relationship to existing pieces

| Existing | Role here |
|---|---|
| `contracts/AgentEscrow.sol` | Native-ETH escrow; becomes the **ETH profile** of the new ERC (not replaced — see §3.3) |
| `agent-payment-protocol.md` (v1.1) | Already names a `currency` field but the contract can't settle it; §4 closes this gap → **v1.2** |
| `switchboard/mpc_wallet.py` | Signing substrate the `AgentWallet` wraps |
| `switchboard/gas_budget.py`, `gas_manager.py` | Reused as the per-tx / per-day caps inside `SpendPolicy` |
| `switchboard/nonce_manager.py` | Reused by `FleetBalancer` for cross-wallet nonce safety |
| `switchboard/adapters/lucidly.py` | The DEX/liquidity engine behind the swap adapter and `Rebalancer` |
| `contracts/IOracleAggregator.sol` | Price source for cross-token valuation and slippage bounds |

---

## 2. Architecture

```
PART 2 — Agent Wallet (Python, agent-facing)
  AgentWallet (wraps MPCWallet)
    ├─ Treasury          balances per (chain, token)
    ├─ Delegation        session keys + SpendPolicy (grant / revoke)
    └─ Router            load-balancer, 4 pluggable strategies:
         TokenSelector · RailSelector · FleetBalancer · Rebalancer
                    │ honors the settlement standard ↓
PART 1 — Settlement Standard (Solidity + ERC + protocol)
  Token Negotiation (off-chain, payment_protocol v1.2 / x402 accepts[])
                    │ picks one mutually-accepted token
  MultiTokenAgentEscrow.sol   settles in that ERC-20 (or ETH)
    + SwapSettlementAdapter (opt-in) via lucidly + IOracleAggregator
  New ERC: "Multi-Token A2A Escrow" (native-ETH EIP = a profile)
```

---

## 3. Part 1 — The Multi-Token Settlement Standard

### 3.1 The standard artifact

A new ERC — **Multi-Token Agent-to-Agent Escrow** — that generalizes the native-ETH EIP. The native-ETH escrow becomes a **profile** (the case where `token == address(0)`), so the already-drafted EIP is subsumed, not invalidated. Deliverables: `eips/draft-multitoken-a2a-escrow.md` + an ethereum-magicians post, mirroring the existing EIP workflow.

### 3.2 Contract — `MultiTokenAgentEscrow.sol`

Same lifecycle as today (create → confirm → release / refund / cancel, with challenge period + timeout), parameterized by `address token`:

- `token == address(0)` → native ETH via `msg.value` (unchanged semantics; the ETH profile).
- ERC-20 → `transferFrom(payer, escrow, amount)` on create (payer approves first); `transfer` on release/refund.
- **Non-standard tokens** (fee-on-transfer, rebasing): credited by measured **balance delta**, not the declared amount; a per-token `allowlist` flag gates whether such tokens are accepted, so the core stays safe by default.
- `Payment` struct gains a `token` field; all events carry `token`.

### 3.3 ⚖️ OPEN DECISION — for @abhicris

How to generalize the shipped, EIP-drafted `AgentEscrow.sol`. All three are viable; we want abhicris's input before committing (the native-ETH EIP is co-authored, so this is a shared call):

| Option | What | Trade-off |
|---|---|---|
| **A** — sibling contract | New `MultiTokenAgentEscrow.sol` beside the untouched ETH escrow, both implementing a shared `IAgentEscrow` interface | Lowest risk to the shipped/EIP'd contract; slight lifecycle duplication |
| **B** — generalize in place | Rewrite `AgentEscrow.sol` to handle ETH (`address(0)`) + ERC-20 in one contract | Less code; but changes the audit surface of an already-EIP'd contract |
| **C** — pluggable settlement modules | Abstract escrow core + `NativeModule` / `ERC20Module` / `SwapModule` | Most extensible; heaviest to audit and reason about |

The rest of this design is written to be **independent of this choice** — the interface (`IAgentEscrow`) and the protocol/wallet layers are identical regardless of A/B/C. Only the contract file layout and test harness differ. Implementation of §5 units ①–③ waits on this decision; everything else can proceed in parallel.

### 3.4 Settlement-token negotiation (protocol v1.2)

Extend `agent-payment-protocol.md` and the x402 `accepts[]` envelope so each party advertises **accepted tokens + a ranked preference**:

```
accepts_tokens: [ { chain_id, token, min_amount, rank } … ]   // payee side
offer_tokens:   [ { chain_id, token, balance_ok, rank } … ]   // payer side
```

Negotiation is deterministic: intersect accepted sets, pick the highest combined-rank common token. Outcome:

- **Common token exists** → settle same-token in `MultiTokenAgentEscrow` (core path, no swap).
- **No common token** → either fail cleanly (`NoCommonSettlementToken`) or, if the payer opts in, route through the swap adapter (§3.5).

`PaymentRequest` v1.2 adds `settlement_token` (the negotiated result) and keeps `currency` as a v1.1-compatible alias for the ETH profile.

### 3.5 Swap adapter (opt-in, layered — NOT in core)

`SwapSettlementAdapter` converts payer-token → payee-token **at release** using `lucidly` + `IOracleAggregator`, bounded by a payee-set `max_slippage_bps`. It sits *outside* the escrow core so the standard stays minimal and auditable: the escrow releases the held token to the adapter, the adapter swaps and forwards the payee's token, reverting the whole release if slippage exceeds the bound.

---

## 4. Part 2 — The Built-in Agent Wallet

`AgentWallet` wraps `MPCWallet` (keeps threshold signing / no single point of failure) and adds four concerns, each an independently testable unit:

### 4.1 Treasury
Tracks balances per `(chain_id, token)`; answers "what can I spend, in what, where." Read-through to chain state with a cache; the source of truth the Router queries.

### 4.2 Delegation — session keys + `SpendPolicy`
`grant(agent_id, policy) -> SessionKey` and `revoke(session_key)`. A `SpendPolicy` is:

- `token_allowlist` — which tokens the agent may spend
- `per_tx_cap`, `daily_cap` — enforced via `gas_budget` / `gas_manager`
- `expires_at` — time-boxed
- `allowed_counterparties` — optional payee allowlist

The agent signs *within* policy; `AgentWallet` validates every rule **before** co-signing. Revocable + time-boxed = safe for autonomous agents. This is what "an agent takes the wallet" means concretely: it receives a scoped, revocable session key — never the root key.

### 4.3 Router — the load-balancer
A strategy pipeline; each strategy is pluggable and independently tested:

| Strategy | Dimension | Decides |
|---|---|---|
| `TokenSelector` | across tokens | which held token to spend (balance / fee / expected slippage) |
| `RailSelector` | across rails | cheapest suitable rail: x402 (micro) / on-chain escrow (trustless/large) / MPP (multi-party) |
| `FleetBalancer` | across wallets | spread spend/nonce over N wallets (nonce contention, rate limits, single-key blast radius) — composes with `nonce_manager` |
| `Rebalancer` | target holdings | keep treasury near a target allocation (e.g. 60% USDC / 30% ETH / 10% native), executed via the swap adapter |

### 4.4 Transaction path
Agent requests a payment → Router: `TokenSelector` picks source token → `RailSelector` picks rail → `FleetBalancer` picks the signing wallet → drives §3.4 negotiation → executes via escrow / x402 / mpp → `gas_budget` + `nonce_manager` enforced throughout → `SpendPolicy` checked before every signature.

---

## 5. Decomposition for the contribution wave

Sliced for parallel work with minimal merge conflict. Dependencies noted.

| # | Unit | Depends on |
|---|---|---|
| ① | `MultiTokenAgentEscrow.sol` (per §3.3 decision) | §3.3 decision |
| ② | `SwapSettlementAdapter.sol` + lucidly/oracle wiring | ① |
| ③ | Foundry suite: ERC-20 / ETH / fee-on-transfer / swap-at-release | ① ② |
| ④ | ERC draft `eips/draft-multitoken-a2a-escrow.md` + magicians post | — |
| ⑤ | `IAgentEscrow` interface + `IOracleAggregator` wiring | — |
| ⑥ | Token negotiation in `payment_protocol.py` (v1.2) | — |
| ⑦ | x402 `accepts[]` multi-token envelope | ⑥ |
| ⑧ | `AgentWallet` + `Treasury` | — |
| ⑨ | Session keys + `SpendPolicy` (reuse gas_budget) | ⑧ |
| ⑩ | `TokenSelector` | ⑧ |
| ⑪ | `RailSelector` | ⑧ |
| ⑫ | `FleetBalancer` (reuse nonce_manager) | ⑧ |
| ⑬ | `Rebalancer` (uses swap adapter) | ⑧ ② |
| ⑭ | Multi-token 2-agent demo + `web/` explorer update | most of the above |

Units ④⑤⑥⑧ have no blockers and can start immediately in parallel.

## 6. Error handling

| Failure | Handling |
|---|---|
| No common settlement token | `NoCommonSettlementToken`; fall back to swap adapter only if payer opted in, else abort cleanly |
| Slippage exceeds bound | Adapter reverts the whole release; escrow stays funded; payee may renegotiate or refund |
| `SpendPolicy` violation | Wallet refuses to sign; typed `PolicyViolation` with the offending rule |
| Insufficient balance in chosen token | Router retries next candidate token/rail before failing |
| Swap execution failure | Fall back to same-token settlement if possible, else abort with `SwapFailed` |
| Nonce reorg / gap | Existing `nonce_manager` path; `FleetBalancer` reassigns to a healthy wallet |

## 7. Testing

- **Contract (Foundry):** ETH profile parity with current escrow; ERC-20 happy path; fee-on-transfer/rebasing via balance-delta; swap-at-release incl. slippage revert; challenge/timeout/refund unchanged.
- **Protocol (pytest):** negotiation determinism (same inputs → same token), no-common-token path, v1.1→v1.2 back-compat.
- **Wallet (pytest):** each Router strategy in isolation; `SpendPolicy` enforcement (cap/expiry/allowlist); session-key revocation; fleet nonce-safety under contention.
- **Integration:** extend the live 2-agent ETH demo into a **2-agent, 2-token** demo (payer holds USDC, payee wants DAI, settled via the adapter), surfaced in `web/`.

## 8. Security considerations

- Session keys are scoped + revocable + expiring; a compromised agent is bounded by its `SpendPolicy`, never holds the root key.
- Swap kept out of escrow core → the trustless primitive has no DEX/oracle attack surface; opt-in only.
- Non-standard tokens gated behind an allowlist + balance-delta accounting to prevent under/over-crediting.
- Oracle used only for slippage bounds, never as the settlement authority (funds move by real transfers, not oracle marks).
- `FleetBalancer` reduces single-key blast radius and nonce-griefing exposure.

## 9. Open decisions (summary)

1. **§3.3 contract-generalization strategy (A / B / C)** — routed to @abhicris. Blocks units ①–③ only.
2. Default `max_slippage_bps` for the swap adapter — propose 50 bps (0.5%), payee-overridable.
3. Whether `Rebalancer` ships in the first wave or a follow-up (it depends on ② and is the least agent-facing).
