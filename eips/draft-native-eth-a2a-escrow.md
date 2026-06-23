---
eip: <to be assigned by an EIP editor on PR>
title: Native-ETH Agent-to-Agent Escrow
description: A minimal payable escrow primitive for autonomous agent-to-agent payments using native ETH, with timeout-based refund and explicit challenge period.
author: Abhishek Krishna (@abhicris), Pattermesh (@Pattermesh), kcolbchain (@kcolbchain)
discussions-to: <ethereum-magicians.org thread URL — to be filled before opening the ethereum/EIPs PR>
status: Draft
type: Standards Track
category: ERC
created: 2026-05-24
requires: 165
---

<!--
  EDITOR NOTE (remove before submitting to ethereum/EIPs):
  Two frontmatter fields are intentionally placeholders because the EIP process
  forbids self-assigning them:
    - `eip:` is assigned by an EIP editor when the submission PR is opened
      (file is named `eip-XXXX.md`); ethereum/EIPs CI normally fills it.
    - `discussions-to:` MUST be a live ethereum-magicians.org thread. Open the
      thread first (see eips/magicians-post-draft.md), then paste the URL here.
  Everything else in this document is final and editor-ready.
-->


## Abstract

This standard defines a minimal escrow contract interface for autonomous agent-to-agent (A2A) payments using native ETH. A compliant contract accepts `msg.value` directly on `createPayment`, holds it under a string-keyed mapping, and resolves it on one of three terminal transitions: `confirmPayment` by the payer, `requestRefund` by the payer after a timeout plus challenge period, or `cancelPayment` by the payer while still locked.

The standard targets the case where two software agents settle a single off-chain deliverable on-chain without needing a token contract, an off-chain allowlist, or an arbitrator.

## Motivation

Existing on-chain agent-payment primitives in production at the time of writing (Coinbase's x402 SettlementContract, Google A2A's payment-claim, Circle Nanopayments, Tempo / MPP) share three properties that make them ill-suited for general A2A use:

1. **Token-bound.** Every primitive surveyed requires a specific ERC-20 token (typically USDC). The payer must (a) acquire the token on the target chain, (b) submit an `approve` transaction before payment, (c) trust that the token's bridge / issuance is live on that chain. Step (b) alone costs ~46k gas and an extra signature per payee.
2. **Coupled to a settlement counterparty.** SettlementContract-style designs assume an off-chain operator who can be sanctioned, off-boarded, or rate-limited. For autonomous agents that pick counterparties on the fly, this re-introduces the trusted third party the on-chain primitive was meant to remove.
3. **Non-portable.** Each primitive is deployed per-chain by its operator. There is no portable bytecode any party can deploy independently to a new chain and have other tooling interoperate with.

Native ETH is the only asset universally available on every EVM-compatible chain without a token contract. A `payable` escrow keyed by a free-form request id, with deterministic refund semantics, is the smallest primitive that resolves all three properties:

- Single transaction (no `approve` step), saving roughly 21k–46k gas per payment depending on chain.
- No off-chain dependency. The contract is self-contained; the payer alone authorizes confirm, refund, and cancel.
- Portable. The same Solidity source compiles and deploys to Ethereum mainnet, Lux, Optimism, Arbitrum, Base, Polygon, and every future EVM chain without modification.

## Specification

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT", "SHOULD", "SHOULD NOT", "RECOMMENDED", "MAY", and "OPTIONAL" in this document are to be interpreted as described in RFC 2119 and RFC 8174.

### State machine

A compliant payment moves through exactly one of these paths:

```
            createPayment{value: amount}()
                       │
                       ▼
                  ┌─────────┐
                  │ Locked  │
                  └────┬────┘
                       │
       ┌───────────────┼───────────────┐
       │               │               │
       │               │     timeoutBlocks + challengePeriod
       │               │     elapsed since createdAt
       │               │               │
       │               │               ▼
       ▼               ▼          requestRefund()
 confirmPayment()  cancelPayment()      │
       │               │               ▼
       ▼               ▼           ┌──────────┐
  ┌─────────┐    ┌──────────┐      │ Refunded │
  │Released │    │Cancelled │      └──────────┘
  └─────────┘    └──────────┘
```

All three terminal states are absorbing. No transition out.

### Required interface

A compliant contract MUST implement `IAgentEscrow`:

```solidity
interface IAgentEscrow {
    enum State { None, Locked, Released, Refunded, Cancelled }

    /// @notice Create an escrow with `msg.value` ETH, keyed by `requestId`.
    /// @dev MUST revert if any of: `requestId` is already used in this
    ///      contract instance, `payee == address(0)`, `timeoutBlocks == 0`,
    ///      `msg.value == 0`, or `bytes(requestId).length == 0`.
    ///      The payer is `msg.sender`.
    function createPayment(
        string calldata requestId,
        address payable payee,
        uint256 timeoutBlocks,
        uint256 challengePeriod
    ) external payable;

    /// @notice Release the escrow to the payee. MUST be callable only by
    ///         the original payer, only while state == Locked, and only
    ///         before block.number >= createdAt + timeoutBlocks.
    function confirmPayment(string calldata requestId) external;

    /// @notice Refund the escrow to the payer. MUST be callable only by
    ///         the original payer, only while state == Locked, and only
    ///         once block.number >= createdAt + timeoutBlocks + challengePeriod.
    function requestRefund(string calldata requestId) external;

    /// @notice Cancel a payment while still Locked. MUST be callable only
    ///         by the original payer. Returns the funds to the payer.
    function cancelPayment(string calldata requestId) external;

    /// @notice Read the payment record. The return shape is illustrative;
    ///         the ERC-165 selector depends only on the argument types
    ///         (`string`), so a `returns (Payment)` struct form is
    ///         equally conformant.
    function getPayment(string calldata requestId) external view returns (
        address payer,
        address payee,
        uint256 amount,
        uint256 timeoutBlocks,
        uint256 challengePeriod,
        State   state,
        uint256 createdAt
    );

    /// @notice True iff state == Locked AND block.number ≥ createdAt + timeoutBlocks.
    function isExpired(string calldata requestId) external view returns (bool);
}
```

### ERC-165 interface identifier

The interface identifier of `IAgentEscrow` is **`0x5c3738e9`**.

It is the XOR of the [Solidity ABI](https://docs.soliditylang.org/en/latest/abi-spec.html#function-selector) function selectors of the six member functions, per [ERC-165](./eip-165.md). A function selector is the first four bytes of `keccak256` of the canonical signature — the function name and the parenthesized argument types only. Return types, the `payable`/`view`/`external` modifiers, and `address` vs `address payable` do **not** affect the selector. The six signatures and selectors are:

| Function (canonical signature) | Selector |
|---|---|
| `createPayment(string,address,uint256,uint256)` | `0x8a5d6ff0` |
| `confirmPayment(string)` | `0x912db0fb` |
| `requestRefund(string)` | `0xc38821fc` |
| `cancelPayment(string)` | `0x84126e01` |
| `getPayment(string)` | `0xc69207a3` |
| `isExpired(string)` | `0xc64fafbc` |

Running XOR (left to right):

```
  0x8a5d6ff0
^ 0x912db0fb  = 0x1b70df0b
^ 0xc38821fc  = 0xd8f8fef7
^ 0x84126e01  = 0x5cea90f6
^ 0xc69207a3  = 0x9a789755
^ 0xc64fafbc  = 0x5c3738e9   ← interface id
```

The computation is reproducible with Foundry:

```bash
cast sig "createPayment(string,address,uint256,uint256)"  # 0x8a5d6ff0
cast sig "confirmPayment(string)"                         # 0x912db0fb
cast sig "requestRefund(string)"                          # 0xc38821fc
cast sig "cancelPayment(string)"                          # 0x84126e01
cast sig "getPayment(string)"                             # 0xc69207a3
cast sig "isExpired(string)"                              # 0xc64fafbc
# XOR of all six = 0x5c3738e9
```

A compliant contract MUST implement [ERC-165](./eip-165.md) and MUST return `true` from `supportsInterface(0x5c3738e9)` and from `supportsInterface(0x01ffc9a7)` (the ERC-165 identifier itself).

### Required events

```solidity
event PaymentCreated(
    string indexed requestId,
    address indexed payer,
    address indexed payee,
    uint256 amount
);
event PaymentReleased(string indexed requestId, address indexed payee, uint256 amount);
event PaymentRefunded(string indexed requestId, address indexed payer, uint256 amount);
event PaymentCancelled(string indexed requestId, address indexed payer, uint256 amount);
```

A compliant contract MUST emit `PaymentCreated` from `createPayment`, and exactly one of `PaymentReleased`, `PaymentRefunded`, or `PaymentCancelled` per request id when reaching the terminal state.

The `requestId` field is `indexed` even though it is a `string`; per the ABI specification, the topic is `keccak256(requestId)`. Off-chain indexers SHOULD hash off-chain request ids to query the log.

### Errors

A compliant contract MUST revert (it MUST NOT silently no-op or return `false`) when a precondition is violated. The normative revert conditions are:

| Function | MUST revert when |
|---|---|
| `createPayment` | `bytes(requestId).length == 0`; `payee == address(0)`; `timeoutBlocks == 0`; `msg.value == 0`; or `requestId` is already in use in this contract instance |
| `confirmPayment` | caller is not the payer; `state != Locked`; or `block.number >= createdAt + timeoutBlocks` (window closed) |
| `requestRefund` | caller is not the payer; `state != Locked`; or `block.number < createdAt + timeoutBlocks + challengePeriod` (challenge window not elapsed) |
| `cancelPayment` | caller is not the payer; or `state != Locked` |
| any terminal transition | the ETH transfer to the recipient fails (the state change MUST be rolled back with the revert) |

The reason strings or [custom errors](https://docs.soliditylang.org/en/latest/contracts.html#errors-and-the-revert-statement) used are NOT normative — only the *fact* of reverting is. Implementations are RECOMMENDED to use named custom errors for cheaper reverts and machine-readable cause codes, for example:

```solidity
error RequestIdInUse(string requestId);
error EmptyRequestId();
error ZeroPayee();
error ZeroTimeout();
error ZeroValue();
error NotPayer(address caller);
error NotLocked(string requestId);
error WindowClosed(string requestId);      // confirm after timeout
error ChallengeNotElapsed(string requestId); // refund before challenge end
error TransferFailed(address recipient, uint256 amount);
```

The reference implementation currently uses `require` reason strings; the migration to custom errors is editorial and does not change the interface id.

### Checks, effects, interactions

All three terminal transitions MUST update the on-chain `state` field before transferring value. The reference implementation uses `(bool ok, ) = payee.call{value: amount}("")` so that smart-contract payees can receive ETH via their `receive` or `fallback`. If the external call fails, the transition MUST revert and the funds remain locked; the payer can retry, or after the challenge period, call `requestRefund`.

### Request ID encoding

The `requestId` is a Solidity `string`. Implementations MAY restrict its length to a sane maximum (the reference implementation accepts up to 256 bytes). The string is opaque to the contract; off-chain protocols (HTTP-402 `X-Request-Id`, ZAP wire `nonce`, etc.) embed their own identifiers directly without conversion.

A `bytes32` variant is intentionally not part of this standard. A future EIP MAY add `IAgentEscrowFixed` with `bytes32` keys for gas-sensitive deployments; the two variants are not interchangeable.

## Rationale

### Native ETH instead of an ERC-20

A token-mediated escrow requires the payer to first `approve` the escrow contract to spend a specific token. This is one extra transaction (~46k gas), one extra signature, and one extra trust assumption (the token contract behaves correctly on this chain). For micropayments, the dominant agent-payment use case, these overheads are larger than the payment itself.

Native ETH avoids all three. The payer authorizes both the value transfer and the escrow creation in a single transaction. The same bytecode works on any EVM chain.

Higher-value flows that genuinely require stablecoin denomination can wrap this contract in a `payable` adapter that swaps ETH for the target token at the contract boundary. The base standard does not depend on a token.

### String `requestId` rather than `bytes32`

UUIDv4 strings, base64-encoded nonces, and human-readable request ids from real off-chain protocols (HTTP headers, JWT claims, agent-emitted ids) are not naturally `bytes32`. Hashing them to `bytes32` makes the on-chain trace impossible to correlate with off-chain logs without a separate mapping.

The cost is paid only once per payment (on `createPayment`); subsequent transitions look up the payment by string key. Event topics are hashed to `bytes32` automatically by the ABI, so indexers see the same overhead they would with a hashed `bytes32` field.

### Explicit challenge period after timeout

A pure timeout without a challenge window forces the payee and the payer to race in the same block: the payee may submit a delivery proof to a separate channel that the payer hasn't yet observed, while the payer's `requestRefund` is pending in the mempool.

The challenge period is the explicit grace window during which neither party can act unilaterally: `confirmPayment` reverts once `block.number >= createdAt + timeoutBlocks`, and `requestRefund` reverts while `block.number < createdAt + timeoutBlocks + challengePeriod`. After the period, only the payer's `requestRefund` is callable.

This intentionally favors the payer in the unhappy path. The rationale: the payer locked the funds; without the payee actively confirming delivery in time, returning the funds is the safer default.

### No on-chain release condition beyond payer confirmation

Some escrow designs (Kleros, UMA Optimistic Oracle) allow a non-payer party to trigger release via arbitration or oracle attestation. This standard does not, by design. The minimum primitive is payer-only release; oracle-mediated release is a strict extension layered on top, not folded in.

A separate standard for attestation-triggered release (see `kcolbchain/escrow-oracles`) is in design. It composes by adding a `releaseByAttestation(string,bytes32,bytes[])` external function callable by a registered oracle aggregator, gated by the same `state == Locked` and `block.number < createdAt + timeoutBlocks` checks. Layering rather than baking-in keeps the base standard auditable.

### Indexed `string` events

Per ABI v0.4+, `indexed` parameters of dynamic types are stored as `keccak256(value)`. The human-readable `requestId` is not directly recoverable from the topic; an off-chain indexer must either maintain a hash-to-string lookup from the `PaymentCreated` event's non-indexed data, or accept that downstream queries are by hash.

The standard indexes the field anyway because the most common indexer query is "all events for `requestId` X" — having topic 0 narrow the result set to a single payment is worth the off-chain bookkeeping.

## Backwards Compatibility

This EIP defines a new contract interface with no existing deployments. There is no backwards compatibility concern.

Implementations MUST NOT silently accept ERC-20 token transfers (`ERC-20::transfer` calls into the escrow address) as escrow creations. A token-aware extension EIP MAY add an `IAgentEscrowToken` interface; that work is out of scope here.

## Reference Implementation

The reference implementation is [`contracts/AgentEscrow.sol`](../contracts/AgentEscrow.sol) in the `kcolbchain/switchboard` repository. It is Solidity ^0.8.20, MIT-licensed, and has been running on Base Sepolia and Lux testnet since April 2026.

The reference contract is a *superset* of `IAgentEscrow`: it implements the full required interface (with `createPayment`/`confirmPayment`/`requestRefund`/`cancelPayment` returning `bool` and `getPayment` returning a `Payment` struct — neither return-shape difference changes the selectors, hence the interface id is unchanged), plus three non-normative extensions that are out of scope for this base standard:

- An owner-curated agent allowlist (`registerAgent` / `deregisterAgent`), and
- An optional oracle-release path (`createPaymentWithPolicy`, `releaseByAttestation`) that consults an external [`IOracleAggregator`](../contracts/IOracleAggregator.sol). This is the concrete shape of the layered extension described in the Rationale; it composes onto the base state machine without altering it.

Two known gaps between the reference contract and this specification, to be closed before the contract is declared conformant (they do not affect the interface id):

1. The reference contract does not yet inherit ERC-165 / expose `supportsInterface`. A conformant deployment MUST add it and return `true` for `0x5c3738e9` and `0x01ffc9a7`.
2. The reference enum is `{Created, Locked, Confirmed, Released, Refunded, Cancelled}`; the canonical `IAgentEscrow` enum is `{None, Locked, Released, Refunded, Cancelled}`. The on-chain observable state machine is identical (only `Locked` and the three terminals are reachable post-`createPayment`); the extra enum members are implementation detail.

Foundry tests live in [`test/AgentEscrow.t.sol`](../test/AgentEscrow.t.sol) (base primitive) and [`contracts/test/AgentEscrowOracle.t.sol`](../contracts/test/AgentEscrowOracle.t.sol) (allowlist + oracle extension), with a mock aggregator at [`contracts/mocks/MockOracleAggregator.sol`](../contracts/mocks/MockOracleAggregator.sol).

## Test Cases

The reference implementation's Foundry suite is the canonical test set. Selected cases (names as they appear in the suite):

| Test | What it asserts |
|---|---|
| `test_happyPath_createConfirmReleased` | After `createPayment`, state is `Locked`; after payer `confirmPayment`, state is `Released` and the payee balance increases by `amount` |
| `test_timeoutRefund_path` | `requestRefund` reverts before `createdAt + timeoutBlocks + challengePeriod`; `isExpired` is true once `block.number >= createdAt + timeoutBlocks`; refund succeeds after the challenge window and state becomes `Refunded` |
| `test_doubleConfirm_reverts` | A second `confirmPayment` on an already-`Released` request reverts |
| `test_cancel_returnsFunds` | `cancelPayment` while `Locked` returns funds to the payer and sets state `Cancelled` |
| `test_onlyPayerCanConfirm` | `confirmPayment` from a non-payer reverts |
| `test_reentrancy_confirmPayment_reverts` | A malicious payee re-entering `confirmPayment` cannot trigger a second release (state already terminal + guard) |
| `test_registerAgent_onlyOwner_strangerReverts` | The allowlist extension's `registerAgent` is owner-gated |
| `test_releaseByAttestation_success` | Oracle-extension release succeeds when the aggregator verifies the attestation; permissionless submitter |
| `test_releaseByAttestation_revertsAfterTimeout` | Oracle release reverts once the timeout window has closed (use refund instead) |

A `supportsInterface(0x5c3738e9) == true` test MUST be added alongside the ERC-165 implementation noted above; it does not yet exist in the reference suite.

## Security Considerations

### Reentrancy

`confirmPayment`, `requestRefund`, and `cancelPayment` all perform an external call (`payee.call{value:}` or `payer.call{value:}`). Each MUST follow checks-effects-interactions: state transition before the external call. A reentrant caller cannot trigger a second transition on the same `requestId` because the state field has already been updated to a terminal value.

### Griefing via dust

A payer may lock 1 wei in escrow with a very large `timeoutBlocks`, occupying a `requestId` indefinitely without economic stake. Implementations MAY enforce a minimum `msg.value`. The reference implementation requires `msg.value > 0` only; tighter policies are a per-deployment choice.

### Smart-contract payees

A payee that is itself a contract must accept ETH via a `receive()` or `payable fallback()`. If the payee's receive function reverts, `confirmPayment` will revert and the funds remain locked. The payer can then `requestRefund` after the challenge period.

A payee whose `receive` consumes more than 2300 gas (e.g., updates storage on receipt) will not work with a `.transfer` or `.send` based release. The standard mandates `.call{value:}` precisely to forward all gas, so this case is supported.

### Front-running and request id squatting

A front-runner cannot grief a payer by submitting their `requestId` first: the payer (signer of `createPayment`) is the one whose funds get locked. A third party reserving a `requestId` would lock their own funds.

Cross-instance `requestId` collisions are impossible; each contract has its own mapping. Indexers MUST scope queries by `(chainId, contractAddress, requestId)`.

### Time-based attacks

`block.number` is used for the timeout and challenge period. On chains with adjustable block times or potential reorgs, `timeoutBlocks` and `challengePeriod` MUST be chosen to comfortably exceed the chain's reorg depth. The reference implementation's default of `timeoutBlocks = 100` is appropriate for chains with ~12s blocks and ≤6-block reorgs; chains with longer reorgs should configure proportionally larger values.

### Event ordering

A consumer relying on event order MUST handle the case where `PaymentCreated` and a terminal event appear in the same block. The contract enforces that the terminal event cannot precede `PaymentCreated` (the `createPayment` transaction must mine first), but a `cancelPayment` in a subsequent transaction in the same block is possible.

### Smart-contract payers

`msg.sender` is the payer. If the payer is an upgradeable contract, an upgrade that changes the contract's view of "who can call `confirmPayment` on behalf of the payer" cannot retroactively affect already-locked escrows: the on-chain `payer` field is immutable and tied to the address. An upgraded payer contract that revokes the ability to call `confirmPayment` will leave funds locked until refund.

## Copyright

Copyright and related rights waived via [CC0](https://creativecommons.org/publicdomain/zero/1.0/).
