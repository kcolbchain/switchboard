---
eip: <TBD — request from EIP editors>
title: Native-ETH Agent-to-Agent Escrow
description: A minimal, payable, timeout-bounded escrow primitive for autonomous agent-to-agent payments using native ETH, without ERC-20 dependencies.
author: kcolbchain (@kcolbchain), Abhishek Krishna (@abhicris), Pattermesh (@Pattermesh)
discussions-to: https://ethereum-magicians.org/<TBD>
status: Draft
type: Standards Track
category: ERC
created: 2026-05-24
requires: 165
---

> **Status note (kcolbchain internal):** This file is a working draft intended to be expanded into a formal EIP submission. The reference implementation lives in [`contracts/AgentEscrow.sol`](../contracts/AgentEscrow.sol). The job of this document is to translate the working contract into the EIP-1 template so it can be submitted to [`ethereum/EIPs`](https://github.com/ethereum/EIPs).
>
> Owner: **@abhicris**. Open questions in §10.
> See [switchboard #49](https://github.com/kcolbchain/switchboard/issues/49) (competitive survey) for the positioning rationale: as of 2026-05-24 we have not identified another deployed primitive that combines (a) `payable` create, (b) request-id keyed mapping, (c) timeout + challenge-period refund, all in one minimal contract. This EIP makes that primitive portable.

## Abstract

This standard defines a minimal escrow contract interface for autonomous agent-to-agent (A2A) payments using native ETH. The contract MUST accept `msg.value` directly on `createPayment(requestId, payee, timeoutBlocks, challengePeriod)`, hold it under a string-keyed mapping, and release on payer confirmation, refund after `timeoutBlocks + challengePeriod`, or cancel while still `Locked`.

The standard intentionally avoids ERC-20 approval flows, wrapped-ETH detours, and oracle-mediated release in v1; those are layered as separate EIPs (see §11).

## Motivation

The agent-payment ecosystem today fragments along three axes:

1. **Token dependency.** Every existing on-chain agent-payment primitive we've surveyed (x402's SettlementContract, AP2's payment-claim, Circle's Nanopayments, MPP/Tempo) settles in a specific token — usually USDC. This requires the payer to hold the token, approve a spender, and accept that the token is bridge-dependent across chains.
2. **Escrow shape.** Where escrow does exist, it's either application-specific (Reality.eth's bond layer, Kleros's arbitration layer) or assumes a human in the loop (mutual-cancel, dispute initiation). Agent-to-agent flows need a primitive that releases on a programmable rule without dragging in arbitrators.
3. **Portability.** No primitive composes cleanly across L1 + L2s. A bytecode-portable EIP — anyone can deploy and integrate against the same interface — would.

This EIP fills the gap with the smallest possible primitive that still supports the three real lifecycles: happy path (release), timeout (refund), mutual cancel.

## Specification

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT", "SHOULD", "SHOULD NOT", "RECOMMENDED", "MAY", and "OPTIONAL" in this document are to be interpreted as described in RFC 2119.

### State machine

```
                       createPayment{value: amount}()
                  ┌─────────────────────────────┐
                  ▼                             │
         ┌────────────────┐                     │
   ┌────▶│     LOCKED     │                     │
   │     └───────┬────────┘                     │
   │             │                              │
   │             │ confirmPayment() ── (only payer) ──▶ ┌───────────┐
   │             │                                       │  RELEASED  │
   │             │                                       └───────────┘
   │             │
   │             │ cancelPayment() ── (only payer) ──▶  ┌────────────┐
   │             │                                       │  CANCELLED │
   │             │                                       └────────────┘
   │             │
   │             │ block.number ≥ createdAt + timeoutBlocks + challengePeriod
   │             │
   │             ▼
   │     ┌────────────────┐  requestRefund() ── (only payer) ──▶ ┌───────────┐
   │     │  REFUNDABLE     │                                       │  REFUNDED  │
   │     └────────────────┘                                       └───────────┘
   │
   │  RELEASED, CANCELLED, REFUNDED are terminal
```

### 3.1 Required interface

A compliant contract MUST implement:

```solidity
interface IAgentEscrow {
    enum State { None, Locked, Released, Refunded, Cancelled }

    /// @notice Create an escrow with `msg.value` ETH, keyed by `requestId`.
    /// @dev MUST revert if `requestId` already used, `payee == address(0)`,
    ///      `timeoutBlocks == 0`, or `msg.value == 0`.
    function createPayment(
        string calldata requestId,
        address payable payee,
        uint256 timeoutBlocks,
        uint256 challengePeriod
    ) external payable;

    /// @notice Release the escrow to the payee. Only the original payer.
    ///         MUST revert if state != Locked or if block.number is past
    ///         createdAt + timeoutBlocks.
    function confirmPayment(string calldata requestId) external;

    /// @notice Refund the escrow to the payer after the challenge period
    ///         has fully elapsed. Only the original payer. MUST revert if
    ///         block.number < createdAt + timeoutBlocks + challengePeriod.
    function requestRefund(string calldata requestId) external;

    /// @notice Cancel a payment while still Locked. Only the payer.
    ///         MUST return the funds to the payer.
    function cancelPayment(string calldata requestId) external;

    /// @notice Read the payment state.
    function getPayment(string calldata requestId) external view returns (
        address payer,
        address payee,
        uint256 amount,
        uint256 timeoutBlocks,
        uint256 challengePeriod,
        State   state,
        uint256 createdAt
    );

    /// @notice Convenience getter — returns true iff state == Locked AND
    ///         block.number ≥ createdAt + timeoutBlocks.
    function isExpired(string calldata requestId) external view returns (bool);
}
```

### 3.2 Required events

```solidity
event PaymentCreated(string indexed requestId, address indexed payer, address indexed payee, uint256 amount);
event PaymentReleased(string indexed requestId, address indexed payee, uint256 amount);
event PaymentRefunded(string indexed requestId, address indexed payer, uint256 amount);
event PaymentCancelled(string indexed requestId, address indexed payer, uint256 amount);
```

A compliant contract MUST emit exactly one terminal event per request id.

### 3.3 ERC-165 support

`supportsInterface(0xTBD)` MUST return `true` where `0xTBD` is the XOR of every selector in `IAgentEscrow`. The interface id will be computed and pinned in the final draft.

### 3.4 Reentrancy

`confirmPayment`, `requestRefund`, and `cancelPayment` MUST follow checks-effects-interactions: state transition before any value transfer. The reference implementation uses `payable.call{value:}` for forward-compatibility with smart-contract payees.

### 3.5 Request ID

`requestId` is a `string` to allow off-chain protocols (HTTP-402, ZAP wire) to embed UUIDs / nonces directly without conversion. Implementations MAY restrict to ≤ 64 ASCII bytes for gas efficiency.

## Rationale

### Why native ETH and not an ERC-20?

The premise of this EIP is that the *minimal* primitive needed for autonomous A2A payments is `payable` ETH transfer with a timeout. Every ERC-20 layer adds: (a) an `approve` transaction the agent has to author + sign, (b) a token-specific bridge / liquidity assumption, (c) chain-specific deployments. ETH is the only asset that is universally available with no extra steps on every EVM chain. Higher-value flows that require stablecoins can wrap this contract in a `payable` adapter that swaps ETH ⇄ token at the boundary — but the base contract MUST NOT depend on a token.

### Why string `requestId` and not `bytes32`?

UUIDv4s + base64 nonces from real off-chain protocols (HTTP `X-Request-Id`, JWT-style ids, agent-readable strings) are not naturally `bytes32`. Hashing them to `bytes32` makes the on-chain trace less debuggable. The cost of `string` is paid only on creation, indexed by hash by event indexers.

### Why a challenge period after the timeout?

A pure timeout without a challenge window forces the payee to compete with the payer's `requestRefund` call in the same block — racing conditions. The challenge period is a small grace window during which the payee can still call `confirmPayment` (which they can't, only payer can — see §3.1 — *correction needed in final draft, see §10 OQ-2*). In v1 it primarily exists so that disputes / arbitrator integrations (out of scope for this EIP) have a deterministic window to act.

### Why not include oracle-mediated release in v1?

Oracle-mediated release shifts settlement authority from the payer to an external attester. That is genuinely different security model and deserves its own EIP. See `kcolbchain/escrow-oracles` for the work-in-progress design + the planned `releaseByAttestation()` extension.

## Backwards Compatibility

No backwards compatibility concerns. This EIP defines a new contract; deployments are independent. The reference implementation (`contracts/AgentEscrow.sol` in `kcolbchain/switchboard`) has been running on testnet since 2026-04 without standardization; integrators already against it will see no breaking change once the EIP number is assigned.

## Reference Implementation

[`contracts/AgentEscrow.sol`](../contracts/AgentEscrow.sol) in the `kcolbchain/switchboard` repository. ~180 lines of Solidity 0.8.20, MIT-licensed. Foundry tests in `tests/` cover all four lifecycles + the four revert cases.

## Security Considerations

### Reentrancy

External calls to `payee` and `payer` SHOULD use `.call{value:}` with proper checks-effects-interactions ordering. The reference implementation does this.

### Front-running

`requestId` uniqueness is per-contract-instance; a frontrunner cannot grief a victim by submitting their `requestId` first because the *payer* (signer of the `createPayment` call) is the one whose funds get locked.

### Griefing via dust

A payer could lock 1 wei in escrow with a huge `timeoutBlocks`, hoarding a request-id forever. Implementations MAY enforce a minimum `msg.value` and a maximum `timeoutBlocks`.

### Smart-contract payees

A payee that is itself a contract MUST be prepared to receive ETH (i.e., expose a payable fallback). The reference implementation uses `.call{value:}` which lets payees execute receive logic; if the payee's receive function reverts, the release will revert and the funds remain locked (the payer can `requestRefund` after the challenge period).

### Wallet attribution

A `requestId` collision across deployments is impossible since each contract is its own mapping. Indexers MUST scope queries by `(chainId, contractAddress, requestId)`.

## Open Questions (TODOs for @abhicris)

- **OQ-1: Interface ID.** Compute and pin the final ERC-165 selector once the interface is frozen.
- **OQ-2: Confirm during challenge period.** Should `confirmPayment` be callable *during* the challenge period? Current ref impl says no (must be before timeout). Worth revisiting — argument for "yes": gives the payee a defensive window if the payer is offline and the timeout is about to expire.
- **OQ-3: Cancel grace.** Should `cancelPayment` require a minimum age (e.g., 1 block) to prevent same-block cancel after a payee acts? Probably out of scope but worth noting.
- **OQ-4: Receipt event.** Should we add a `PaymentReceipt(requestId, txHash)` event for off-chain receipt indexers, or rely on the existing `PaymentReleased` event with extended off-chain metadata?
- **OQ-5: Discussion thread.** Open an `ethereum-magicians` thread first to gauge editor reception. Link in the `discussions-to` field above.
- **OQ-6: Composition with escrow-oracles.** This EIP MUST NOT reference `kcolbchain/escrow-oracles` directly — they're independently composable. But the rationale section's "v1 does not include oracle-mediated release" wording should be diplomatic: this EIP defines the *base*; extension EIPs may add oracle hooks.

## Copyright

Copyright and related rights waived via [CC0](https://creativecommons.org/publicdomain/zero/1.0/).
