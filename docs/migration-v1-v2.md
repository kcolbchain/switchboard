# Migration Guide: AgentEscrow v1 → v2

This guide outlines the transition from `AgentEscrow` (v1) to `AgentEscrowV2`, which introduces **Composable Refund Policies as On-Chain Plugins**.

## What Changed?
In v1, `AgentEscrow.sol` only supported a single timeout-based refund policy: if the payer requested a refund after `timeoutBlocks + challengePeriod`, they received the full amount back.

In `AgentEscrowV2`, the refund policy logic has been extracted into pluggable contracts implementing `IRefundPolicy`. This allows infinite refund semantics without needing to upgrade the core escrow contract, such as milestone payments, pro-rata subscriptions, and multi-night reservations.

## For Smart Contract Integrators

### 1. Default Behavior (Backward Compatibility)
If you continue to use the v1 function signature:
```solidity
function createPayment(
    string calldata requestId,
    address payee,
    uint256 timeoutBlocks,
    uint256 challengePeriod
) external payable returns (bool);
```
Under the hood, `AgentEscrowV2` routes this to the standard `TimeoutPolicy` functionality. The caller doesn't need to change their existing integration; it is 100% backward compatible.

### 2. Using Custom Policies
To use a custom policy, call the new `createPayment` signature:
```solidity
function createPayment(
    string calldata requestId,
    address payee,
    IRefundPolicy policy,                 // ← new: pluggable policy
    bytes calldata policyData,
    uint256 timeoutBlocks
) external payable returns (bool);
```

#### Existing Policies
- **TimeoutPolicy:** Parity with v1. `policyData` = `abi.encode(amount, timeoutBlocks, challengePeriod)`.
- **ProRataPolicy:** For subscriptions. Refunds proportional to time elapsed. `policyData` = `abi.encode(totalAmount, totalBlocks)`.
- **MultiNightPolicy:** Per-night release schedule. `policyData` = `abi.encode(totalAmount, blocksPerNight, totalNights)`.

### 3. Refunds and Partial Releases
Instead of `requestRefund(requestId)`, when using a custom policy, you must provide the same `policyData` you used during creation:
```solidity
// Payer requests a refund (unearned portion is returned)
function requestRefund(string calldata requestId, bytes calldata policyData) external returns (bool);

// Payee requests a partial release (earned portion is distributed)
function releasePartial(string calldata requestId, bytes calldata policyData) external returns (bool);
```

## For Python SDK Users

`switchboard.payment_protocol.PaymentClient` has been updated. The `create_payment` method now accepts `policy` and `policy_data` arguments.

### Example: Using a Custom Policy in Python
```python
client = PaymentClient(private_key, escrow_address, rpc_url)

# E.g., for ProRataPolicy, encode totalAmount and totalBlocks using eth_abi or similar
policy_address = "0x..."
policy_data = b"..." 

request = client.create_payment(
    payee="0xPayee...",
    amount_wei=10**18,
    timeout_blocks=100,
    policy=policy_address,
    policy_data=policy_data
)
```

If you do not pass a `policy`, the SDK falls back to the v1 timeout/challenge period logic automatically.
