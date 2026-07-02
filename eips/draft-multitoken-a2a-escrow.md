---
eip: <to be assigned by an EIP editor on PR>
title: Multi-Token Agent-to-Agent Escrow
description: A token-agnostic escrow primitive for autonomous agent-to-agent payments, settling in native ETH or any ERC-20, with timeout, challenge period, and optional oracle release.
author: Abhishek Krishna (@abhicris), Pattermesh (@Pattermesh), kcolbchain (@kcolbchain)
discussions-to: <ethereum-magicians.org thread URL — to be filled before opening the ethereum/EIPs PR>
status: Draft
type: Standards Track
category: ERC
created: 2026-07-02
requires: 20, 165
---

<!--
  EDITOR NOTE (remove before submitting to ethereum/EIPs):
  Two frontmatter fields are intentionally placeholders because the EIP process
  forbids self-assigning them:
    - `eip:` is assigned by an EIP editor when the submission PR is opened
      (file is named `eip-XXXX.md`); ethereum/EIPs CI normally fills it.
    - `discussions-to:` MUST be a live ethereum-magicians.org thread. Open the
      thread first (see eips/magicians-post-multitoken.md), then paste the URL here.
  Everything else in this document is final and editor-ready.
-->


## Abstract

This standard defines a token-agnostic escrow contract interface for autonomous agent-to-agent (A2A) payments. A compliant contract accepts either native ETH (via `msg.value`) or any owner-allowlisted ERC-20 token on `createPayment`, holds the funds under a string-keyed mapping, and resolves on one of four terminal transitions: `confirmPayment` by the payer, `requestRefund` by the payer after a timeout plus challenge period, `cancelPayment` by the payer while still locked, or `releaseByAttestation` by any party holding a valid oracle attestation keyed by an attached policy hash.

The settlement asset is selected at payment creation via an `address token` parameter: `address(0)` denotes the **native-ETH profile**, in which case the contract behaves identically to the primitive specified in the companion native-ETH ERC draft (see [Relationship to the Native-ETH ERC](#relationship-to-the-native-eth-erc)). Any other address selects an ERC-20 settlement asset, pulled via `transferFrom` at creation and disbursed via `transfer` at release, with fee-on-transfer safety guaranteed by balance-delta accounting.

## Motivation

The native-ETH A2A escrow standard resolves three defects common to existing on-chain agent-payment primitives (token-binding, off-chain operator coupling, and per-chain non-portability). It does so by restricting settlement to native ETH: the only asset universally available on every EVM-compatible chain without a token contract.

Production agent deployments, however, routinely hold and transact in ERC-20 stablecoins (USDC, DAI, USDT) or chain-specific tokens and prefer to settle in those assets. Forcing settlement into ETH imposes conversion cost, slippage, and cross-chain bridge risk. Two concrete failure modes motivate this generalization:

1. **Stablecoin-denominated payees.** A payee quoting in USDC terms cannot accept ETH settlement without real-time price discovery and slippage risk. Wrapping the ETH escrow in a DEX call moves that risk inside the settlement path rather than outside it.
2. **Chain-specific token ecosystems.** On chains with their own native tokens as the primary liquidity asset (e.g. LUX on the Lux network), an ETH-only escrow requires an additional bridge step before the payment reaches the chain, eliminating the portability advantage.

This standard generalizes the native-ETH primitive to any settlement asset while keeping every design invariant of the base standard: single-transaction funding (one `approve` for ERC-20, no second round-trip beyond the payer's existing approval), no off-chain operator, portable bytecode, and a deterministic payer-first refund policy.

The native-ETH escrow is not superseded. It becomes the **ETH profile** of this standard — the case `token == address(0)` — so implementations of the native-ETH draft that add a `token` field to their `Payment` struct and require `token == address(0)` satisfy both standards simultaneously.

## Specification

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT", "SHOULD", "SHOULD NOT", "RECOMMENDED", "MAY", and "OPTIONAL" in this document are to be interpreted as described in RFC 2119 and RFC 8174.

### Profiles

This standard defines two profiles of the same interface:

| Profile | `token` value | Funding mechanism | Release mechanism |
|---|---|---|---|
| **ETH profile** | `address(0)` | `msg.value` on `createPayment` | Low-level `.call{value:}` |
| **ERC-20 profile** | ERC-20 contract address | `IERC20.transferFrom(payer, escrow, amount)` on `createPayment` | `IERC20.transfer(recipient, amount)` |

All lifecycle semantics, state machine transitions, timeout/challenge-period logic, event requirements, and error conditions are identical across both profiles. A compliant implementation MUST support the ETH profile. It MAY restrict the ERC-20 profile to an owner-managed allowlist.

### State machine

A compliant payment moves through exactly one of these paths:

```
            createPayment(token, amount, …)
              [ETH profile: also msg.value == amount]
                           │
                           ▼
                      ┌─────────┐
                      │ Locked  │
                      └────┬────┘
                           │
         ┌─────────────────┼──────────────────────┐
         │                 │                      │
         │     block.number < createdAt            │
         │     + timeoutBlocks                     │
         │                 │      block.number ≥ createdAt
         │    policyHash   │      + timeoutBlocks
         │    != 0x00      │      + challengePeriod │
         │                 │                       │
         ▼                 │                       ▼
 releaseByAttestation()    │                 requestRefund()
         │                 │                       │
         ▼         ┌───────┴────────┐              ▼
   ┌──────────┐    │  confirmPayment │        ┌──────────┐
   │ Released │◄───│  cancelPayment  │        │ Refunded │
   └──────────┘    └───────┬────────┘        └──────────┘
                           │
                     cancelPayment()
                           │
                           ▼
                     ┌──────────┐
                     │Cancelled │
                     └──────────┘
```

All terminal states (`Released`, `Refunded`, `Cancelled`) are absorbing. No transition out.

### Required interface

A compliant contract MUST implement `IAgentEscrow`:

```solidity
interface IAgentEscrow {
    enum State {
        Created,
        Locked,
        Confirmed,
        Released,
        Refunded,
        Cancelled
    }

    struct Payment {
        address payer;
        address payee;
        address token;          // address(0) = ETH profile; else the ERC-20 escrowed
        uint256 amount;         // credited amount held in escrow
        uint256 timeoutBlocks;  // blocks until auto-expire
        uint256 challengePeriod;// blocks payer must additionally wait after timeout to reclaim
        State state;
        string requestId;       // off-chain payment request ID
        uint256 createdAt;      // block number at creation
        bytes32 policyHash;     // 0x00 = payer-only release; non-zero enables oracle release
    }

    // ─── Events ─────────────────────────────────────────────────────────────

    event PaymentCreated(
        string indexed requestId,
        address indexed payer,
        address indexed payee,
        address token,
        uint256 amount
    );
    event PaymentLocked(string indexed requestId, address token);
    event PaymentConfirmed(string indexed requestId, address indexed payer, address token);
    event PaymentReleased(
        string indexed requestId,
        address indexed payee,
        address token,
        uint256 amount
    );
    event PaymentReleasedByOracle(
        string indexed requestId,
        bytes32 policyHash,
        bytes32 attestationHash
    );
    event PaymentRefunded(
        string indexed requestId,
        address indexed payer,
        address token,
        uint256 amount
    );
    event PaymentCancelled(
        string indexed requestId,
        address indexed payer,
        address token,
        uint256 amount
    );

    // ─── Lifecycle ───────────────────────────────────────────────────────────

    /// @notice Create a payment and lock funds.
    /// @param requestId       Off-chain payment request ID. MUST be unique per contract instance.
    /// @param payee           Recipient on release. MUST NOT be address(0).
    /// @param token           Settlement asset. address(0) = ETH profile (send via msg.value);
    ///                        otherwise an ERC-20 the payer has approved to this contract.
    /// @param amount          Declared amount. ETH profile requires amount == msg.value.
    ///                        ERC-20 profile pulls up to amount via transferFrom; the
    ///                        credited amount is the measured balance delta.
    /// @param timeoutBlocks   Blocks until the payment auto-expires. MUST be > 0.
    /// @param challengePeriod Additional blocks the payer must wait after timeout to reclaim.
    function createPayment(
        string calldata requestId,
        address payee,
        address token,
        uint256 amount,
        uint256 timeoutBlocks,
        uint256 challengePeriod
    ) external payable returns (bool);

    /// @notice Release the escrow to the payee.
    ///         MUST be callable only by the original payer, only while state == Locked,
    ///         and only while block.number < createdAt + timeoutBlocks.
    function confirmPayment(string calldata requestId) external returns (bool);

    /// @notice Oracle-mediated release, gated by the payment's policyHash.
    ///         MUST be callable by any address while state == Locked and
    ///         block.number < createdAt + timeoutBlocks.
    ///         MUST revert if policyHash == 0x00 on the payment.
    function releaseByAttestation(
        string calldata requestId,
        bytes32 attestationHash,
        bytes[] calldata signatures
    ) external returns (bool);

    /// @notice Refund the escrow to the payer after timeout + challenge period.
    ///         MUST be callable only by the original payer, only while state == Locked,
    ///         and only once block.number >= createdAt + timeoutBlocks + challengePeriod.
    function requestRefund(string calldata requestId) external returns (bool);

    /// @notice Cancel a still-locked payment.
    ///         MUST be callable only by the original payer while state == Locked.
    function cancelPayment(string calldata requestId) external returns (bool);

    /// @notice Read the payment record.
    function getPayment(string calldata requestId) external view returns (Payment memory);
}
```

### ERC-165 interface identifier

The interface identifier of `IAgentEscrow` is **`0x01dc5a49`**.

It is the XOR of the [Solidity ABI](https://docs.soliditylang.org/en/latest/abi-spec.html#function-selector) function selectors of the six member functions, per [ERC-165](./eip-165.md). The six canonical signatures and selectors are:

| Function (canonical signature) | Selector |
|---|---|
| `createPayment(string,address,address,uint256,uint256,uint256)` | `0x75fd60ae` |
| `confirmPayment(string)` | `0x912db0fb` |
| `releaseByAttestation(string,bytes32,bytes[])` | `0x6404c242` |
| `requestRefund(string)` | `0xc38821fc` |
| `cancelPayment(string)` | `0x84126e01` |
| `getPayment(string)` | `0xc69207a3` |

Running XOR (left to right):

```
  0x75fd60ae
^ 0x912db0fb  = 0xe4d0d055
^ 0x6404c242  = 0x80d41217
^ 0xc38821fc  = 0x435c33eb
^ 0x84126e01  = 0xc74e5dea
^ 0xc69207a3  = 0x01dc5a49   ← interface id
```

The computation is reproducible with Foundry:

```bash
cast sig "createPayment(string,address,address,uint256,uint256,uint256)"  # 0x75fd60ae
cast sig "confirmPayment(string)"                                          # 0x912db0fb
cast sig "releaseByAttestation(string,bytes32,bytes[])"                   # 0x6404c242
cast sig "requestRefund(string)"                                           # 0xc38821fc
cast sig "cancelPayment(string)"                                           # 0x84126e01
cast sig "getPayment(string)"                                              # 0xc69207a3
# XOR of all six = 0x01dc5a49
```

A compliant contract MUST implement [ERC-165](./eip-165.md) and MUST return `true` from `supportsInterface(0x01dc5a49)` and from `supportsInterface(0x01ffc9a7)` (the ERC-165 identifier itself).

### ETH profile preconditions

When `token == address(0)`:

- `msg.value` MUST equal the declared `amount`. A compliant contract MUST revert if `msg.value != amount`.
- A compliant contract MUST NOT require or read any ERC-20 approval.
- The `credited` amount stored in the `Payment` record is `msg.value`.

### ERC-20 profile preconditions

When `token != address(0)`:

- `msg.value` MUST be `0`. A compliant contract MUST revert if `msg.value > 0`.
- The payer MUST have approved this contract to spend at least `amount` of `token` before calling `createPayment`.
- A compliant contract MUST measure the balance delta to determine the `credited` amount:

  ```
  credited = balanceOf(address(this), token) after transferFrom
           - balanceOf(address(this), token) before transferFrom
  ```

  The `amount` field of the stored `Payment` MUST be `credited`, not the declared `amount`. This makes the implementation safe for fee-on-transfer tokens: the contract never promises to release more than it actually holds.

- A compliant contract SHOULD maintain a per-token allowlist and MUST revert if the token is not on the allowlist. Native ETH (`address(0)`) is implicitly always allowed.
- The `credited` amount MUST be `> 0`. A compliant contract MUST revert if no tokens arrived (e.g., `transferFrom` succeeded but the delta is zero — possible with some rebasing tokens).

### Required events

```solidity
event PaymentCreated(
    string indexed requestId,
    address indexed payer,
    address indexed payee,
    address token,
    uint256 amount       // credited amount
);
event PaymentLocked(string indexed requestId, address token);
event PaymentConfirmed(string indexed requestId, address indexed payer, address token);
event PaymentReleased(string indexed requestId, address indexed payee, address token, uint256 amount);
event PaymentReleasedByOracle(string indexed requestId, bytes32 policyHash, bytes32 attestationHash);
event PaymentRefunded(string indexed requestId, address indexed payer, address token, uint256 amount);
event PaymentCancelled(string indexed requestId, address indexed payer, address token, uint256 amount);
```

A compliant contract MUST emit `PaymentCreated` and `PaymentLocked` from `createPayment`. It MUST emit exactly one of `PaymentReleased`, `PaymentRefunded`, or `PaymentCancelled` when reaching a terminal state. `PaymentConfirmed` MUST be emitted alongside `PaymentReleased` when the payer triggers release via `confirmPayment`. `PaymentReleasedByOracle` MUST be emitted alongside `PaymentReleased` when release is triggered by oracle attestation.

All terminal events carry `token` and `amount` (the credited amount disbursed) to allow indexers to attribute flows per asset.

The `requestId` field is `indexed` even though it is a `string`; per the ABI specification, the topic is `keccak256(requestId)`. Off-chain indexers SHOULD hash off-chain request ids to query logs.

### Errors

A compliant contract MUST revert (it MUST NOT silently no-op or return `false`) when a precondition is violated. The normative revert conditions are:

| Function | MUST revert when |
|---|---|
| `createPayment` | `bytes(requestId).length == 0`; `payee == address(0)`; `timeoutBlocks == 0`; `amount == 0`; `requestId` is already in use in this contract instance; ETH profile and `msg.value != amount`; ERC-20 profile and `msg.value > 0`; ERC-20 profile and token is not on the allowlist; ERC-20 profile and `credited == 0` |
| `confirmPayment` | caller is not the payer; `state != Locked`; or `block.number >= createdAt + timeoutBlocks` |
| `releaseByAttestation` | `state != Locked`; `policyHash == bytes32(0)`; `block.number >= createdAt + timeoutBlocks`; or the oracle aggregator rejects the attestation |
| `requestRefund` | caller is not the payer; `state != Locked`; or `block.number < createdAt + timeoutBlocks + challengePeriod` |
| `cancelPayment` | caller is not the payer; or `state != Locked` |
| any terminal transition | the asset transfer to the recipient fails (the state change MUST be rolled back with the revert) |

The reason strings or [custom errors](https://docs.soliditylang.org/en/latest/contracts.html#errors-and-the-revert-statement) used are NOT normative. Implementations are RECOMMENDED to use named custom errors for cheaper reverts and machine-readable cause codes.

### Checks, effects, interactions

All three payer-driven terminal transitions and `releaseByAttestation` MUST update the on-chain `state` field and zero the `amount` field before transferring value. For the ETH profile, the disbursement MUST use `recipient.call{value: amount}("")` to forward all gas (see Security Considerations — Smart-contract payees). For the ERC-20 profile, the disbursement MUST use a `transfer`-compatible call; implementations are RECOMMENDED to use a SafeERC20 wrapper to tolerate non-boolean-returning tokens (e.g. USDT). If the transfer fails, the transition MUST revert and the funds remain locked.

### Oracle release policy

`releaseByAttestation` is an OPTIONAL lifecycle path. Its availability is per-payment, not per-contract: it is enabled only when the `policyHash` field of a `Payment` is non-zero. A payer that does not set a policy hash on `createPayment` receives payer-only release semantics. A separate `createPaymentWithPolicy` function that accepts a `policyHash` argument MAY be provided by compliant implementations; its inclusion does not change the interface identifier.

The oracle verification logic (signature schemes, quorum rules, attestation formats) is intentionally left to the `IOracleAggregator` implementation and is NOT normative in this standard. The only normative requirement is that the aggregator returns a boolean and that a `false` return causes `releaseByAttestation` to revert.

### Request ID encoding

The `requestId` is a Solidity `string`, opaque to the contract. Implementations MAY restrict its length to a reasonable maximum. The string is compatible with HTTP-402 `X-Request-Id`, ZAP wire nonce, and JWT claim identifiers without conversion. Indexers MUST scope queries by `(chainId, contractAddress, requestId)`.

## Rationale

### Generalization over a new primitive

This standard could have been written independently, with the native-ETH escrow as a separate, narrower standard. Instead it is designed as a strict superset: every implementation of the native-ETH ERC is a valid implementation of this standard's ETH profile, with only the addition of the `token` parameter (fixed to `address(0)`) and the `policyHash` field (which may be zero). This preserves every existing conformant deployment and avoids splitting the A2A escrow surface into two incompatible interface families.

### Balance-delta accounting for ERC-20 tokens

The credited amount stored in the `Payment` is the measured balance increase, not the declared `amount`. This design choice has three consequences:

1. **Fee-on-transfer tokens are safe.** A token that deducts a fee on transfer credits only what arrived; the escrow cannot be made to promise more than it holds.
2. **Rebasing tokens are bounded.** The escrow credits the balance at create-time. Rebasing up or down between create and release does not trigger accounting errors; the amount released equals what was recorded, not what is currently held. (Implementations that wish to track the live rebase delta MAY do so, but this is not required and changes the interface.)
3. **The `amount` event field is the credited amount.** Downstream indexers see the real economic value locked, not a declared amount that may differ.

The cost is one extra `balanceOf` read on create (approximately 700 gas on typical ERC-20s), which is negligible relative to the `transferFrom` itself.

### Token allowlist

The per-token allowlist allows a deployer to gate which ERC-20s are accepted before the contract has been audited against a given token's edge cases. Native ETH is exempt: the ETH profile has no token contract edge cases. The allowlist is an operational guard, not a trust assumption — a permissionless version that skips the allowlist check is conformant, but SHOULD document the associated risks (see Security Considerations — Unsupported token types).

### Oracle release as an opt-in, per-payment path

Embedding oracle-mediated release directly into the base interface (rather than as an extension) means that all compliant implementations must handle the oracle path, but a payment with `policyHash == 0x00` is indistinguishable from a payer-only release payment at the protocol level. The cost is one additional function in the interface, paid once; the benefit is that every compliant escrow deployment is capable of hosting oracle-policy payments without an upgrade. Keeping `releaseByAttestation` in `IAgentEscrow` also allows a single ERC-165 check to confirm the full capability set.

### Why `confirmPayment` returns `bool`

The base native-ETH ERC specifies `confirmPayment` returning nothing (void). This standard specifies `returns (bool)` to align with common ERC-20 `transfer` convention and to give callers a machine-readable success signal without relying on the absence of a revert. The interface selector `0x912db0fb` is the same in both; the return type does not affect the ABI selector.

### Relationship to the Native-ETH ERC

The companion native-ETH A2A escrow draft specifies a narrower interface (`IAgentEscrow` without a `token` parameter, `policyHash`, or `releaseByAttestation`). That draft's interface identifier is `0x5c3738e9`. This standard's identifier is `0x01dc5a49`. The two are NOT interchangeable: a contract conforming to this standard does not automatically report `supportsInterface(0x5c3738e9) == true` unless it also implements the native-ETH interface function signatures verbatim (which it cannot, because the `createPayment` signatures differ in parameter count). Implementations that wish to signal compatibility with both SHOULD maintain a separate `AgentEscrow`-compatible entry point or use an adapter.

### `block.number` for timeouts

As in the native-ETH ERC, this standard uses block height rather than timestamp for the timeout and challenge period windows. The reasoning is unchanged: block height is reorg-stable, monotonically increasing, and not manipulable within the miner's discretion window the way `block.timestamp` is on some chains. The deployer is responsible for choosing `timeoutBlocks` and `challengePeriod` values appropriate for the target chain's block time and expected reorg depth.

## Backwards Compatibility

This EIP defines a new contract interface. It does not modify or deprecate any existing standard.

A contract conforming to the companion native-ETH A2A escrow ERC can be made to conform to this standard's ETH profile by adding the `token` parameter (fixed to `address(0)`) to `createPayment` and adding `releaseByAttestation`, `policyHash`, and the `token` field to `Payment`. These are additive changes; they do not affect the native-ETH contract's existing selectors.

Implementations MUST NOT silently accept plain ERC-20 `transfer` calls into the contract address as escrow creations. Only the `createPayment` + `transferFrom` path is normative.

## Reference Implementation

The reference implementation is [`contracts/MultiTokenAgentEscrow.sol`](../contracts/MultiTokenAgentEscrow.sol) in the `kcolbchain/switchboard` repository. It is Solidity ^0.8.20, MIT-licensed, deployed on Base Sepolia and Lux testnet. The shared interface is [`contracts/IAgentEscrow.sol`](../contracts/IAgentEscrow.sol).

The reference contract is a superset of `IAgentEscrow`:

- It implements the full required lifecycle interface.
- It adds `createPaymentWithPolicy(…, bytes32 policyHash)` for oracle-enabled payments; this function is a convenience wrapper and does not change the interface identifier.
- It maintains an owner-curated per-token allowlist (`setTokenAllowed`) and an agent allowlist (`registerAgent` / `deregisterAgent`).
- It exposes `isExpired(string requestId) external view returns (bool)` and `isState(string, State)` as non-normative read helpers.

Known gaps to close before the contract is declared fully conformant:

1. The reference contract does not yet inherit ERC-165 / expose `supportsInterface`. A conformant deployment MUST add it and return `true` for `0x01dc5a49` and `0x01ffc9a7`.
2. A `supportsInterface(0x01dc5a49) == true` Foundry test does not yet exist and MUST be added.

The oracle aggregator interface is [`contracts/IOracleAggregator.sol`](../contracts/IOracleAggregator.sol); a mock is at [`contracts/mocks/MockOracleAggregator.sol`](../contracts/mocks/MockOracleAggregator.sol).

## Test Cases

The reference Foundry suite covers:

| Test | What it asserts |
|---|---|
| `test_happyPath_ETH_createConfirmReleased` | ETH profile: `createPayment{value}` sets `Locked`; `confirmPayment` sets `Released` and transfers ETH to payee |
| `test_happyPath_ERC20_createConfirmReleased` | ERC-20 profile: `transferFrom` on create; `transfer` on confirm; balances correct |
| `test_feeOnTransfer_creditsDelta` | Fee-on-transfer token: `amount` stored is balance delta, not declared; release is exactly what was credited |
| `test_timeoutRefund_path` | `requestRefund` reverts before challenge period ends; succeeds after; state is `Refunded` |
| `test_doubleConfirm_reverts` | Second `confirmPayment` on a `Released` payment reverts |
| `test_cancel_returnsFunds_ETH` | ETH `cancelPayment` while `Locked` returns ETH to payer; state is `Cancelled` |
| `test_cancel_returnsFunds_ERC20` | ERC-20 `cancelPayment` returns tokens to payer |
| `test_onlyPayerCanConfirm` | `confirmPayment` from non-payer reverts |
| `test_reentrancy_confirmPayment_reverts` | Malicious payee re-entering `confirmPayment` cannot trigger a second release |
| `test_releaseByAttestation_success` | Oracle path succeeds; permissionless submitter |
| `test_releaseByAttestation_revertsNoPolicyHash` | Oracle path reverts when `policyHash == 0x00` |
| `test_releaseByAttestation_revertsAfterTimeout` | Oracle path reverts once timeout window has closed |
| `test_ERC20_notAllowlisted_reverts` | `createPayment` with a non-allowlisted token reverts |
| `test_ERC20_msgValue_reverts` | `createPayment` with ERC-20 token and non-zero `msg.value` reverts |

A `supportsInterface(0x01dc5a49) == true` test MUST be added alongside the ERC-165 implementation.

## Security Considerations

### Reentrancy

`confirmPayment`, `requestRefund`, `cancelPayment`, and `releaseByAttestation` all perform external asset transfers. Each MUST follow checks-effects-interactions: update `state` and zero `amount` before the transfer. A reentrant caller cannot trigger a second transition on the same `requestId` because the state field has already reached a terminal value. Implementations are RECOMMENDED to use a reentrancy guard (e.g., OpenZeppelin `ReentrancyGuard`) as defense in depth, since `releaseByAttestation` involves a call to an external oracle aggregator before the transfer, creating a reentrant surface if the state is not updated first.

### ERC-20 transfer failures and stuck funds

For ERC-20 tokens, `transfer` on a terminal transition MUST succeed or the transition MUST revert. A token whose `transfer` is paused, blacklisted, or otherwise broken can cause funds to become temporarily unreachable. Implementations SHOULD use SafeERC20 to handle non-boolean-returning tokens and SHOULD revert cleanly on failed transfers. The challenge-period / refund path gives the payer a recovery route if the payee-bound transfer is broken: after the challenge period, the payer can call `requestRefund` which routes the transfer back to themselves.

### Fee-on-transfer tokens and the allowlist

The balance-delta accounting design is correct for fee-on-transfer tokens: the escrow credits what it received and releases exactly that amount. However, a release of a fee-on-transfer token will again incur a fee, so the payee receives less than the credited amount. This is expected and is the token's behavior; the escrow does not attempt to compensate. Deployers SHOULD document per-token behavior in their allowlist policy so payers are aware. Tokens that charge fees differently on different call paths (e.g. tax only on buy) SHOULD be individually reviewed before allowlisting.

### Rebasing tokens

Rebasing tokens that increase supply (positive rebase) will leave a surplus in the contract after release; that surplus is unattributed and cannot be withdrawn unless the implementation adds a sweep function. Rebasing tokens that decrease supply (negative rebase) may make the credited amount undeliverable (the contract holds less than it promised to release). Implementations that accept rebasing tokens SHOULD either track the live balance (which changes the accounting model) or restrict them to the allowlist with appropriate user-facing warnings. The reference implementation accepts any allowlisted token and relies on the deployer's allowlist curation.

### Unsupported token types

Token contracts that implement behaviors incompatible with this interface (e.g., tokens that revert on `transfer` to contract addresses, tokens with infinite-loop `transferFrom`, tokens with transfer hooks that re-enter this contract) can cause denial-of-service or reentrancy. The allowlist is the primary mitigation: a compliant implementation that exposes an allowlist gate SHOULD review each token against these behaviors before allowlisting. A permissionless implementation that skips the allowlist gate MUST prominently document the increased risk.

### Front-running and request ID squatting

As in the native-ETH profile: a front-runner cannot grief a payer by reserving a `requestId` in their name, because the payer is the one whose funds are locked. A third party squatting a `requestId` locks their own funds. Cross-instance collisions are impossible; each contract has its own mapping. Indexers MUST scope by `(chainId, contractAddress, requestId)`.

### Oracle aggregator trust surface

`releaseByAttestation` delegates trust to the `IOracleAggregator` set at construction. A compromised or malicious aggregator can release funds to the payee before the payer intended. Payers that do not want oracle-mediated release MUST use `policyHash == bytes32(0)` (the default `createPayment` path); they cannot be subject to `releaseByAttestation` regardless of the aggregator state. The aggregator address is immutable in the reference implementation; upgrades require a new deployment.

### Smart-contract payees

A payee that is itself a contract must accept the settlement asset. For the ETH profile: the payee must expose a `receive()` or `payable fallback()` that does not revert, and that does not consume more than the gas forwarded. The standard mandates `.call{value:}` (not `.transfer` or `.send`) to forward all gas, supporting contract payees that update storage on receipt. For the ERC-20 profile: the payee must not have a blocked `transfer` target. If a transfer fails, the terminal transition reverts and the payer can retry or, after the challenge period, call `requestRefund`.

### Smart-contract payers

`msg.sender` is the payer at create time. An upgradeable payer contract that loses the ability to call `confirmPayment` after an upgrade cannot retroactively move the on-chain `payer` field; funds remain locked until the refund path opens.

### Time-based attacks

`block.number` is used for the timeout and challenge period. On chains with variable block times, reorg risk, or sequencer control (L2s), `timeoutBlocks` and `challengePeriod` MUST be sized to comfortably exceed the chain's expected reorg depth and block-time variance. Very short `timeoutBlocks` on high-reorg chains may allow a payer to request a refund before the payee's delivery confirmation has finalized.

### Griefing via dust

A payer may lock a minimal amount with a large `timeoutBlocks`, occupying a `requestId` indefinitely. Implementations MAY enforce a minimum `amount`. The reference implementation requires `amount > 0` only.

## Copyright

Copyright and related rights waived via [CC0](https://creativecommons.org/publicdomain/zero/1.0/).
