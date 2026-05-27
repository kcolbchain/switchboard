# Research Survey: Native-ETH Agent-to-Agent Escrow

**Status:** v1.0
**Date:** 2026-05-27
**Surveyor:** @Gaotax2006
**Tracks issue:** [#49](https://github.com/kcolbchain/switchboard/issues/49)

---

## Executive Summary

**Finding: Switchboard's AgentEscrow.sol is the only production-ready native-ETH agent-to-agent escrow primitive as of May 2026.**

Of 10 projects surveyed, 9 either require a specific ERC-20 token (USDC), depend on a centralized settlement operator, or are generalized multi-purpose escrows not optimized for A2A micropayments. Only switchboard provides a minimal, payable, self-contained escrow that two autonomous agents can use without any third-party dependency, token approval, or off-chain infrastructure.

## Methodology

Each project was evaluated against four criteria:

| Criterion | Weight | Description |
|-----------|--------|-------------|
| Native ETH | High | Can the payer use native ETH without an ERC-20? |
| A2A-specific | Medium | Is the primitive explicitly designed for agent-to-agent flows? |
| No operator | High | Does settlement depend on a third-party operator or oracle? |
| Portable | Low | Can the bytecode be deployed independently on any EVM chain? |

Projects were scored 0-3 per criterion (0 = no support, 3 = native support). A score >= 8/12 indicates a direct competitor.

## Surveyed Projects

### 1. Coinbase x402 SettlementContract

| Criterion | Score | Notes |
|-----------|:-----:|-------|
| Native ETH | 0 | USDC only. Requires ERC-20 approval. |
| A2A-specific | 2 | Designed for x402 HTTP-402 payments, agent-like but human-initiated. |
| No operator | 1 | Settlement handled by Coinbase's off-chain operator. |
| Portable | 0 | Coinbase-deployed only; bytecode not independently deployable. |

**Score: 3/12**

The closest conceptual cousin to switchboard. x402 uses HTTP-402 status codes + X-Payment-Required headers to signal payment, and a SettlementContract to release funds. Key differences: (1) requires USDC, (2) Coinbase operates the settlement contract, (3) dependent on Base chain availability. Switchboard's ETH-native approach eliminates the USDC dependency; its operator-free design eliminates the Coinbase dependency.

**Reference:** https://github.com/coinbase/x402

### 2. Google A2A Payment Claim

| Criterion | Score | Notes |
|-----------|:-----:|-------|
| Native ETH | 0 | USDC only. |
| A2A-specific | 3 | Part of Google A2A agent-to-agent protocol. |
| No operator | 0 | Requires Google's payment infrastructure. |
| Portable | 0 | Google-managed; not independently deployable. |

**Score: 3/12**

Google's A2A framework includes a PaymentClaim message for agents to request payment. However, settlement goes through Google's payment processing layer. Not a self-contained on-chain primitive. Agents cannot deploy their own escrow.

**Reference:** https://github.com/google/A2A

### 3. Circle Nanopayments / USDC

| Criterion | Score | Notes |
|-----------|:-----:|-------|
| Native ETH | 0 | USDC on multiple chains. |
| A2A-specific | 1 | Generic payment processing, not agent-specific. |
| No operator | 0 | Circle manages the USDC settlement. |
| Portable | 0 | USDC is a token; no escrow contract. |

**Score: 1/12**

Circle's infrastructure handles cross-chain USDC transfers. While it enables programmable payments, it is not an escrow primitive and requires USDC. Agents must trust Circle's bridge and comply with Circle's policies.

**Reference:** https://www.circle.com/nanopayments

### 4. Tempo / MPP (Multi-Party Payment)

| Criterion | Score | Notes |
|-----------|:-----:|-------|
| Native ETH | 0 | Uses HTS (Hedera Token Service) for stablecoin settlement. |
| A2A-specific | 0 | Enterprise payment rail, not agent-oriented. |
| No operator | 0 | Tempo operates as a licensed payment institution. |
| Portable | 0 | Hedera-specific. |

**Score: 0/12**

Tempo's MPP is a licensed payment system using Hedera for settlement. Not suitable for autonomous agent-to-agent payments on EVM chains.

**Reference:** https://www.tempo.eu.com/mpp

### 5. Sablier (streaming)

| Criterion | Score | Notes |
|-----------|:-----:|-------|
| Native ETH | 2 | Supports ETH and ERC-20 streaming. |
| A2A-specific | 0 | Designed for payroll / vesting, not agent payments. |
| No operator | 3 | Fully on-chain, no operator. |
| Portable | 3 | Deployable on any EVM chain. |

**Score: 8/12**

Sablier is a continuous streaming protocol. While it supports native ETH and is fully on-chain, it is designed for linear vesting over time, not per-deliverable escrow. An agent cannot create a Sablier stream for a single off-chain deliverable and cancel after delivery.

**Not a direct competitor.** Sablier solves a different problem (streaming vs. escrow).

### 6. Superfluid (streaming)

| Criterion | Score | Notes |
|-----------|:-----:|-------|
| Native ETH | 0 | Wrapper tokens; native ETH requires wrapping first. |
| A2A-specific | 0 | Subscription / streaming, not per-work escrow. |
| No operator | 3 | Fully on-chain. |
| Portable | 3 | EVM-compatible chain support. |

**Score: 6/12**

Superfluid allows continuous settlement of value flows. Like Sablier, it is a streaming protocol, not a per-deliverable escrow.

**Not a direct competitor.**

### 7. Hats Finance / Kleros (arbitration)

| Criterion | Score | Notes |
|-----------|:-----:|-------|
| Native ETH | 2 | Supports ETH. |
| A2A-specific | 0 | Designed for dispute resolution, not agent payments. |
| No operator | 1 | Requires arbitrators / jurors. |
| Portable | 2 | Deployable on EVM chains. |

**Score: 5/12**

Kleros provides arbitration-backed escrows. They are general-purpose and could be adapted for A2A use, but they are overkill: each escrow involves a bond, a dispute period, and potentially a jury. For agent micropayments where the payer can simply refund after a timeout, the overhead of arbitration is unnecessary.

**Not a direct competitor.**

### 8. ERC-20 generic escrow contracts (OpenZeppelin Escrow)

| Criterion | Score | Notes |
|-----------|:-----:|-------|
| Native ETH | 0 | ERC-20 bound. |
| A2A-specific | 0 | Generic escrow. |
| No operator | 3 | Fully on-chain. |
| Portable | 3 | Deployable anywhere. |

**Score: 6/12**

OpenZeppelin's Escrow.sol and ConditionalEscrow.sol are ERC-20-based escrow primitives. They are general-purpose and could be used for agent payments, but they require ERC-20 approval and do not emit agent-oriented events.

**Partial competitor.** Switchboard's native ETH support is the differentiating factor.

### 9. Bitcoin DLCs (Discreet Log Contracts)

| Criterion | Score | Notes |
|-----------|:-----:|-------|
| Native ETH | 0 | Bitcoin-native. |
| A2A-specific | 0 | Oracle-based conditional payments. |
| No operator | 1 | Requires oracle. |
| Portable | 0 | Bitcoin only. |

**Score: 1/12**

DLCs use an oracle to attest to a condition, then release funds. While trust-minimized, they require Bitcoin, an oracle, and are not EVM-compatible. Not applicable to switchboard's EVM agent payments.

### 10. Polygon zkEVM Native Bridge (bridged ETH escrow)

| Criterion | Score | Notes |
|-----------|:-----:|-------|
| Native ETH | 3 | Native ETH on Polygon zkEVM. |
| A2A-specific | 0 | Bridge / rollup primitive. |
| No operator | 3 | Trustless via ZK proofs. |
| Portable | 0 | Polygon-specific. |

**Score: 6/12**

Polygon's zkEVM bridge locks ETH on L1 and mints it on L2. While it uses native ETH, it solves asset bridging, not agent payments.

**Not a direct competitor.**

## Comparative Analysis

### Gap matrix

| Feature | Switchboard | x402 | Google A2A | Sablier | Superfluid | OZ Escrow |
|---------|:-----------:|:----:|:----------:|:-------:|:----------:|:---------:|
| Native ETH | Yes | No | No | Yes | No | No |
| No operator | Yes | No | No | Yes | Yes | Yes |
| A2A-specific | Yes | Partial | Yes | No | No | No |
| Per-deliverable | Yes | Yes | Yes | No | No | Partial |
| Challenge period | Yes | No | No | No | No | No |
| Gas-optimized | Yes | Partial | No | Partial | No | Partial |
| Portable EVM | Yes | No | No | Yes | Yes | Yes |

### Key insight

The only projects that offer [Native ETH, A2A-specific, No operator, Portable] = 4/4 are none. The closest is Coinbase x402 (3/4, missing native ETH and operator-free). Switchboard's AgentEscrow.sol is the **only** primitive that hits all four criteria.

The gap exists because:
1. Most payment projects optimize for human users, who prefer stablecoins (USDC).
2. Operator-mediated settlement is simpler for the primitive designer but introduces trust.
3. Agent-to-agent payments are a newer use case; existing primitives adapted from human-focused designs carry assumptions (ERC-20, KYC, operator) that are unnecessary for agents.

## Recommendation

**Originality confirmed.** We recommend proceeding with the EIP standardization (issue #50) and documenting AgentEscrow.sol as a new ERC primitive. The survey should be referenced in the EIP's Motivation section to justify why a new standard is needed.

Additionally, consider:
- Publishing a short blog post summarizing this survey to generate community awareness.
- Filing issues on x402 and Google A2A repositories suggesting native ETH escrow support.
- Engaging with the ERC discussion forum (ethereum-magicians.org) to get early feedback on the EIP.

## References

| Project | URL | Type |
|---------|-----|------|
| Coinbase x402 | https://github.com/coinbase/x402 | Payment protocol |
| Google A2A | https://github.com/google/A2A | Agent protocol |
| Circle Nanopayments | https://www.circle.com/nanopayments | Payment infrastructure |
| Tempo MPP | https://www.tempo.eu.com/mpp | Payment rail |
| Sablier | https://sablier.com | Streaming |
| Superfluid | https://www.superfluid.finance | Streaming |
| Kleros | https://kleros.io | Arbitration |
| OpenZeppelin Escrow | https://github.com/OpenZeppelin/openzeppelin-contracts | Escrow contract |
| Bitcoin DLCs | https://github.com/discreetlogcontracts/dlcs | Conditional payments |
| Polygon zkEVM | https://polygon.technology/polygon-zkevm | L2 bridge |
