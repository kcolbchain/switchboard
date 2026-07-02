# Thinking Chains & Intelligent Financial Systems on switchboard

**Status:** Context / positioning
**Companion code:** [`switchboard/thinking_chain.py`](../switchboard/thinking_chain.py), [`switchboard/adapters/hanzo.py`](../switchboard/adapters/hanzo.py)

---

## Thesis

Autonomous AI increasingly *reasons its way to financial decisions*. A model deciding to hire another agent, buy an API call, post a bond, or rebalance a treasury is running a **thinking chain** — a multi-step reasoning trace where some steps are not thoughts but **money movements**. The moment a thinking chain touches money, it needs a settlement substrate built for machine reasoning, not for humans clicking "confirm."

That substrate is switchboard.

## What a thinking chain is

A thinking chain is an ordered, inspectable sequence of typed steps an agent walks to reach and execute a decision:

```
assess-task → negotiate-settlement-token → policy/fairness-check
   → create-escrow → verify-work → release-or-refund
```

Some steps are pure reasoning; others are **financial actions**. Each step records its input, its reasoning, and its outcome, so the whole chain is auditable and replayable. In `switchboard/thinking_chain.py` this is the `ThinkingChain` runner: financial steps call the real primitives (`negotiate_settlement_token`, `AgentWallet.pay` via the `Router`, `access_policy.check`) and emit `metrics.WalletOpEvent`s, so a reasoning trace and a settlement trace are the *same* trace.

## Why generic payment rails fail thinking chains

An LLM thinking chain that pays with a raw private key + an RPC endpoint is one runaway loop away from ruin, and one ambiguous counterparty away from theft. Generic rails miss five things a reasoning agent needs:

| Thinking-chain need | Generic rail | switchboard |
|---|---|---|
| **Bounded risk** — a wrong thought can't drain the wallet | none | session keys + `SpendPolicy` (per-tx / daily caps, allowlists, expiry) |
| **Fairness** — one agent can't starve a fleet | none | per-agent token-bucket in `access_policy` |
| **Trustless settlement** — pay only if work is accepted | manual escrow | `MultiTokenAgentEscrow` (timeout, challenge, refund) |
| **Token choice** — pay in what each side actually holds/wants | single asset | settlement-token negotiation + opt-in swap adapter |
| **Observability** — every financial step is measurable | logs, maybe | `metrics` (fill rate, time-to-release, denials, fleet health) |

A thinking chain without these is a demo. A thinking chain *with* them is a system you can let run unattended.

## The escrow thinking-chain pattern

The canonical financial thinking chain is escrow-mediated hiring:

1. **assess** — is this task worth paying for, and how much?
2. **negotiate** — pick a settlement token both sides accept (`negotiate_settlement_token`); if none, opt into the swap adapter.
3. **check** — `access_policy.check` gates the action against tier, fairness, `SpendPolicy`, and contract compliance *before* any signature.
4. **escrow** — lock funds in `MultiTokenAgentEscrow` (ETH profile or any allowlisted ERC-20).
5. **verify** — inspect the delivered work; this is a *reasoning* step feeding a financial one.
6. **release or refund** — settle on the provenance the chain recorded.

Every step is a record. The chain is the receipt.

## Intelligent financial systems need this

Scale the single chain to a population and you get **intelligent financial systems**: agent-to-agent markets, autonomous treasuries, multi-hop service economies where agents subcontract agents. Those systems live or die on properties switchboard provides natively:

- **Auditability** — reasoning + settlement in one trace; disputes are replayable.
- **Bounded, revocable authority** — an intelligent system delegates spend to sub-agents via session keys it can revoke the instant a chain misbehaves.
- **Fairness under contention** — shared rails that no single agent can monopolize.
- **Composability of rails** — a chain routes each payment over the cheapest suitable rail (x402 for micro, escrow for trustless, MPP for multi-party) without re-plumbing.

## Hanzo agents on switchboard

Hanzo AI agents (via the Hanzo MCP `fetch` tool and the `switchboard/adapters/hanzo.py` binding) can run their thinking chains directly on switchboard: a Hanzo agent identity maps to a scoped switchboard `AgentWallet` + `SessionKey`, its paid `fetch` calls settle through the x402 middleware, and its higher-order decisions run as escrow thinking chains. `HanzoEscrowThinkingChain` is the worked example.

## Acknowledgments

Cross-org access (pattermesh / lux / hanzo / zoo) that made this integration possible was provided by **@zeekay**.
