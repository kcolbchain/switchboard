# Gas-budget waive/cap escalation threat model

This note audits the current switchboard gas-budget surface and the planned
chain-side waiver/reset surface described in issue #84. The current repository
ships client-side budget tracking plus escrow release paths; the explicit
`waiveEpoch(agentId, newCap)`, `resetEpoch()`, and on-chain `AgentBudget` state
are not present yet. Those future paths are therefore specified here as launch
gates instead of being marked complete.

## Security objective

An agent's gas budget must fail closed. No caller, committee, middleware, escrow
recipient, or clock/chain reorg condition should be able to raise, reset, or
bypass a configured cap without an auditable authorization path.

The invariant is:

```text
spent(agent, epoch) + requested_spend <= active_cap(agent, epoch)
```

Any exception must be explicit, bounded to one `(agentId, epoch, nonce)` tuple,
and visible in logs or events.

## Current implementation map

| Path | Current code | Budget mutation | Current guard |
| --- | --- | --- | --- |
| Client-side rolling budget | `switchboard/gas_budget.py` | `record(wallet, gas_used)` appends spend and may pause the wallet | `threading.Lock`, rolling hour/day windows, negative value rejection |
| Client-side hard reset | `switchboard/gas_budget.py` | `reset(wallet)` clears all spend for a wallet | local caller authority only; no signature or role model yet |
| Client-side resume | `switchboard/gas_budget.py` | `resume(wallet)` clears pause without clearing counters | local caller authority only; counters remain intact |
| Legacy singleton budget | `switchboard/gas_tracker.py` | `record_gas_usage`, `set_limits`, `reset_all` mutate global totals | singleton lock; reset and limit changes are local process authority |
| x402 auto-payment | `switchboard/x402_middleware.py` | validates against `gas_tracker`, pays, then records amount | cap check before pay; recipient allowlist; expiration check; local tracker only |
| MPP session cap | `switchboard/mpp/session.py` | checks tracker on open/charge and records settled amount on close | session `limit_usd`; local tracker; status may pause after budget exhaustion |
| Escrow payer release | `contracts/AgentEscrow.sol` | releases ETH to payee | `nonReentrant`, Locked-state check, payer-only authorization, timeout check |
| Escrow oracle release | `contracts/AgentEscrow.sol` | releases ETH to payee after oracle attestation | `nonReentrant`, Locked-state check, policy hash, aggregator verification |
| Escrow refund/cancel | `contracts/AgentEscrow.sol` | returns ETH to payer | `nonReentrant`, state checks, payer-only authorization |

## Trust boundaries

- Local process boundary: `GasBudgetTracker` and `GasTracker` trust the process
  that instantiates them. They do not authenticate `set_limits`, `reset`,
  `reset_all`, or `resume` calls.
- Middleware boundary: `X402Middleware` trusts the provided `payment_client` and
  the provided tracker object. It checks policy before payment, but the tracker
  interface is duck-typed and local.
- Session boundary: `MPPSession` enforces a per-session USD cap locally. It
  reconciles to `GasBudgetTracker` on close, so unclosed or failed sessions must
  be handled by the caller.
- Contract boundary: `AgentEscrow` owns the ETH state. Release/refund/cancel
  entry points use checks-effects-interactions and `nonReentrant`.
- Future committee boundary: any waiver committee must be treated as partially
  Byzantine. A single signer must not be able to raise caps, reset epochs, or
  replay old waivers.

## Threat scenarios

### Silent cap escalation by operator or committee

Attack: a compromised operator raises a cap via `setCap` or submits an emergency
waiver during a spend window, allowing an agent to drain more than its published
budget.

Mitigations required:

- Emit a cap-change event with `agentId`, `oldHourly`, `oldDaily`, `newHourly`,
  `newDaily`, `epoch`, caller, and reason hash.
- Require a role or committee quorum for every cap increase. Cap decreases may
  be less privileged but should still emit the same event.
- Add a maximum cap delta or timelock for non-emergency increases.
- Treat `setCap` as initial provisioning only after an agent exists; subsequent
  increases should use the same waiver path as emergency changes.

Current status:

- The Python trackers expose local `set_limits` APIs without authentication.
  That is acceptable for client-side libraries, but not sufficient for an
  on-chain `AgentBudget`.

### Reentrancy during release or waiver

Attack: `release -> external call -> waiveEpoch -> release` attempts to use a
payee callback to raise or reset budget state before a second release.

Mitigations required:

- Every waiver-relevant entry point that can affect spend or cap state must use
  a reentrancy guard or an equivalent state-machine lock.
- Budget state must update before any external value transfer or callback.
- Release paths must be one-way state transitions. Re-entered calls should fail
  on state, even before the guard is considered.

Current status:

- `AgentEscrow.confirmPayment`, `releaseByAttestation`, `requestRefund`, and
  `cancelPayment` are marked `nonReentrant`.
- Both release paths set `p.state = State.Released` and `p.amount = 0` before
  the external ETH transfer.
- Future `waiveEpoch`, `resetEpoch`, and `setCap` entry points should follow the
  same pattern and should not call untrusted contracts.

### Replay of an old waiver signature

Attack: a valid waiver from an older epoch is replayed after reset, silently
raising a new epoch's budget.

Mitigations required:

- Waiver signatures must bind to:

```text
domainSeparator,
chainId,
budgetContract,
agentId,
epoch,
nonce,
newCap,
expiresAt,
reasonHash
```

- Store consumed nonces per `(agentId, epoch)`.
- Reject stale epochs and expired signatures.
- Include the current cap in the signed payload when practical, so a waiver
  cannot be applied to a materially different baseline.

Current status:

- `x402_middleware.PaymentOffer` and `PaymentProof` carry nonces, but those
  nonces are payment-message nonces, not committee waiver nonces.
- The future waiver design must add its own nonce ledger.

### Premature epoch reset by time or reorg manipulation

Attack: a boundary condition or reorg causes `resetEpoch()` to run early,
clearing `*_spent` before the real epoch is complete.

Mitigations required:

- Use block-number epochs or finalized timestamps rather than raw wall-clock
  time for chain-side resets.
- Allow reset only when `currentEpoch > storedEpoch`.
- Store `lastResetBlock` and reject resets inside the finality window.
- Emit reset events with old/new epoch, previous spend, and caller.
- If off-chain services mirror the state, reconcile only after sufficient
  confirmations.

Current status:

- `GasBudgetTracker` uses rolling windows from an injected clock. That is good
  for deterministic client-side tests, but a chain-side reset must not inherit
  local wall-clock assumptions.
- `GasTracker` resets on UTC hour/day boundaries. It should remain a local
  helper, not the source of truth for an on-chain invariant.

### Middleware under-accounting after failed settlement

Attack: middleware validates against a budget, sends a payment, but crashes
before recording spend; later payments see stale spend.

Mitigations required:

- Prefer durable budget ledgers for production agents.
- Record pending spend before broadcast and reconcile to actual spend after
  confirmation.
- Make payment history durable or derive it from receipts.

Current status:

- `X402Middleware` records after `_pay_onchain` returns.
- `MPPSession` records on `close`, not on every charge. This is reasonable for a
  prototype, but production callers need failure recovery for unclosed sessions.

## Required waiver state machine

Future chain-side implementation should model waiver state as:

```text
PendingSpend -> SettledSpend
           \-> RevertedSpend

CapActive -> WaiverProposed -> WaiverApplied
                         \-> WaiverExpired

EpochOpen -> EpochFinalizing -> EpochClosed
```

Rules:

- Spend increments must be monotonic inside an epoch.
- Cap increases must be explicit waiver transitions.
- Epoch reset may open a new epoch but must not mutate historical spend.
- A waiver can apply only once.
- A waiver cannot both raise a cap and reset spend unless the signed payload
  explicitly authorizes both operations.

## Audit checklist

- [x] Current escrow value-transfer entry points use `nonReentrant`.
- [x] Current escrow release paths update state before external calls.
- [x] Current rolling budget tracker rejects negative spend.
- [x] Current rolling budget tracker serializes mutations with a lock.
- [ ] Chain-side `AgentBudget` state exists.
- [ ] `waiveEpoch(agentId, newCap)` exists and is guarded by quorum.
- [ ] Waiver signatures are bound to `(agentId, epoch, nonce)`.
- [ ] Consumed waiver nonces are stored per `(agentId, epoch)`.
- [ ] `resetEpoch()` is tied to finalized chain epoch boundaries.
- [ ] Cap increases emit auditable events with old and new values.
- [ ] A third-party auditor signs off before mainnet.

## No-silent-escalation requirement

Before mainnet, the published model should be considered satisfied only if every
cap increase, spend reset, or budget bypass is observable from one of:

- an on-chain event,
- a durable off-chain ledger entry,
- a signed waiver payload,
- or a test vector checked into this repository.

Any path that can change effective spend capacity without one of those records
is a silent-escalation path and should block release.
