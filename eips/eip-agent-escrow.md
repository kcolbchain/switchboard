---
eip: <TBD>
title: Native-ETH Agent-to-Agent Escrow
description: A minimal payable escrow primitive for autonomous agent-to-agent payments using native ETH, with timeout-based refund and explicit challenge period.
author: Abhishek Krishna (@abhicris), Pattermesh (@Pattermesh), kcolbchain (@kcolbchain)
discussions-to: https://ethereum-magicians.org/<TBD>
status: Draft
type: Standards Track
category: ERC
created: 2026-05-24
requires: 165
---

## Abstract

This standard defines a minimal escrow contract interface for autonomous agent-to-agent (A2A) payments using native ETH. A compliant contract accepts msg.value directly on createPayment, holds it under a string-keyed mapping, and resolves it on one of three terminal transitions: confirmPayment by the payer, requestRefund by the payer after a timeout plus challenge period, or cancelPayment by the payer while still locked.

The standard targets the case where two software agents settle a single off-chain deliverable on-chain without needing a token contract, an off-chain allowlist, or an arbitrator.

## Motivation

Existing on-chain agent-payment primitives in production at the time of writing (Coinbase's x402 SettlementContract, Google A2A's payment-claim, Circle Nanopayments, Tempo / MPP) share three properties that make them ill-suited for general A2A use:

1. Token-bound. Every primitive surveyed requires a specific ERC-20 token (typically USDC). The payer must (a) acquire the token on the target chain, (b) submit an approve transaction before payment, (c) trust that the token's bridge / issuance is live on that chain. Step (b) alone costs ~46k gas and an extra signature per payee.
2. Coupled to a settlement counterparty. SettlementContract-style designs assume an off-chain operator who can be sanctioned, off-boarded, or rate-limited. For autonomous agents that pick counterparties on the fly, this re-introduces the trusted third party the on-chain primitive was meant to remove.
3. Non-portable. Each primitive is deployed per-chain by its operator. There is no portable bytecode any party can deploy independently to a new chain and have other tooling interoperate with.

Native ETH is the only asset universally available on every EVM-compatible chain without a token contract. A payable escrow keyed by a free-form request id, with deterministic refund semantics, is the smallest primitive that resolves all three properties:

- Single transaction (no approve step), saving roughly 21k-46k gas per payment depending on chain.
- No off-chain dependency. The contract is self-contained; the payer alone authorizes confirm, refund, and cancel.
- Portable. The same Solidity source compiles and deploys to Ethereum mainnet, Lux, Optimism, Arbitrum, Base, Polygon, and every future EVM chain without modification.

## Specification

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT", "SHOULD", "SHOULD NOT", "RECOMMENDED", "MAY", and "OPTIONAL" in this document are to be interpreted as described in RFC 2119 and RFC 8174.

### State machine

A compliant payment moves through exactly one of these paths:

```
            createPayment{value: amount}()
                       |
                       |
                  +-----------+
                  |  Locked   |
                  +-----+-----+
                        |
        +---------------+---------------+
        |               |               |
        |     timeoutBlocks + challengePeriod
        |     elapsed since createdAt
        |               |               |
        |               |               |
        |          requestRefund()
 confirmPayment()      |               cancelPayment()
        |               |               |
        |          +--------+           |
   +----------+    | Refunded|     +-----------+
   | Released |    +--------+     | Cancelled |
   +----------+                  +-----------+
```

All three terminal states are absorbing. No transition out.

### Required interface

A compliant contract MUST implement IAgentEscrow:

```solidity
interface IAgentEscrow {
    enum State { None, Locked, Released, Refunded, Cancelled }

    function createPayment(
        string calldata requestId,
        address payable payee,
        uint256 timeoutBlocks,
        uint256 challengePeriod
    ) external payable;

    function confirmPayment(string calldata requestId) external;
    function requestRefund(string calldata requestId) external;
    function cancelPayment(string calldata requestId) external;

    function getPayment(string calldata requestId) external view returns (
        address payer,
        address payee,
        uint256 amount,
        uint256 timeoutBlocks,
        uint256 challengePeriod,
        State   state,
        uint256 createdAt
    );

    function isExpired(string calldata requestId) external view returns (bool);
}
```

The ERC-165 interface identifier is 0x5c3738e9.

### Required events

```solidity
event PaymentCreated(string indexed requestId, address indexed payer, address indexed payee, uint256 amount);
event PaymentReleased(string indexed requestId, address indexed payee, uint256 amount);
event PaymentRefunded(string indexed requestId, address indexed payer, uint256 amount);
event PaymentCancelled(string indexed requestId, address indexed payer, uint256 amount);
```

### Checks, effects, interactions

All three terminal transitions MUST update the on-chain state field before transferring value. The reference implementation uses .call{value:} so that smart-contract payees can receive ETH via their receive or fallback. If the external call fails, the transition MUST revert and the funds remain locked.

### Request ID encoding

The requestId is a Solidity string. Implementations MAY restrict its length to a sane maximum (the reference implementation accepts up to 256 bytes). The string is opaque to the contract; off-chain protocols embed their own identifiers directly without conversion.

## Rationale

### Native ETH instead of an ERC-20

A token-mediated escrow requires the payer to first approve the escrow contract to spend a specific token. This is one extra transaction (~46k gas), one extra signature, and one extra trust assumption.

### String requestId rather than bytes32

UUIDv4 strings, base64-encoded nonces, and human-readable request ids from real off-chain protocols are not naturally bytes32. Hashing them makes the on-chain trace impossible to correlate with off-chain logs without a separate mapping.

### Explicit challenge period after timeout

A pure timeout without a challenge window forces the payee and the payer to race in the same block. The challenge period is an explicit grace window during which neither party can act unilaterally.

## Backwards Compatibility

This EIP defines a new contract interface with no existing deployments. There is no backwards compatibility concern.

## Reference Implementation

The reference implementation is contracts/AgentEscrow.sol in the kcolbchain/switchboard repository. The contract is ~180 lines of Solidity ^0.8.20, MIT-licensed, dependency-free. It has been running on Base Sepolia and Lux testnet since April 2026.

Foundry test coverage covers createPayment success path, value lock, event emission, all revert cases, confirmPayment happy path, only-payer enforcement, expired-window revert, requestRefund happy path, pre-challenge-period revert, cancelPayment happy path, only-locked revert, smart-contract payee receiving, and reentrancy via checks-effects-interactions ordering.

## Security Considerations

### Reentrancy

confirmPayment, requestRefund, and cancelPayment all perform an external call. Each MUST follow checks-effects-interactions: state transition before the external call.

### Griefing via dust

A payer may lock 1 wei in escrow with a very large timeoutBlocks, occupying a requestId indefinitely without economic stake. Implementations MAY enforce a minimum msg.value.

### Front-running and request id squatting

A front-runner cannot grief a payer by submitting their requestId first: the payer (signer of createPayment) is the one whose funds get locked.

### Time-based attacks

block.number is used for the timeout and challenge period. On chains with adjustable block times or potential reorgs, timeoutBlocks and challengePeriod MUST be chosen to comfortably exceed the chain's reorg depth.

## Copyright

Copyright and related rights waived via CC0.
