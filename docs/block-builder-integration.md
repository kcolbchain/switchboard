# Block-Builder Integration: Per-Agent Epoch Gas Budgets

This document outlines how validator operators and block builders can integrate with the `AgentBudget` contract to enforce agent gas limits at the runtime level.

## Problem

Agent wallets are subject to hourly and daily gas budgets. If these budgets are only enforced client-side, they are vulnerable to out-of-band transactions (bypassing the client) or multi-process race conditions where two parallel client instances double-spend the cap.

## Proposed Integration

We expose a runtime hint via the `AgentBudget.sol` contract deployed at a well-known address. 

### Mechanism

1. The `AgentBudget` contract stores per-agent budget states, tracking `epoch`, `hourlyCap`, `dailyCap`, `hourlySpent`, `dailySpent`, and `lastResetBlock`.
2. When a transaction tagged with an `agent_id` is in the mempool, the block builder consults the `AgentBudget` contract state for that agent.
3. The builder simulates the transaction. If the gas cost would push the `hourlySpent` or `dailySpent` over their respective caps, the builder **defers** the transaction to the next epoch (or rejects it if the builder's local policy demands strict enforcement).
4. Because this is a **hint** rather than a consensus rule, an over-budget transaction is not inherently invalid at the network level, but compliant block builders will prioritize protecting agent wallets from draining their ETH over capturing the immediate base fee.

### Querying the Contract

To read the current budget state for an agent, call `budgets(address)` on the `AgentBudget` contract.

```solidity
struct Budget {
    uint256 epoch;
    uint256 hourlyCap;
    uint256 dailyCap;
    uint256 hourlySpent;
    uint256 dailySpent;
    uint256 lastResetBlock;
}

function budgets(address agent) external view returns (Budget memory);
```

Compare the `hourlySpent + estimatedGas` against `hourlyCap`, handling the implicit time-based resets (if the current block timestamp pushes into a new hour or day).

### Emergency Waiver
In the event of an emergency, a committee 2-of-N quorum can waive an agent's current epoch cap by calling `waiveEpoch(agentId, newHourlyCap, newDailyCap)`. The block builder will immediately respect the new cap.
