# GasManager Unification

## Goal
Unify the two legacy gas-budget implementations into a single core so we can support:

- rolling-window budgets for per-wallet agent spend
- calendar-reset budgets for the legacy global tracker
- backward-compatible imports for existing callers

## Proposed API

### Core
`GasManager(default_limits, clock, mode, scope)`

- `mode="rolling"` for sliding windows
- `mode="calendar"` for UTC hour/day resets
- `scope="per-wallet"` for isolated wallets
- `scope="global"` for a singleton/global budget

### Shared methods
- `set_limits(...)`
- `limits_for(...)`
- `can_spend(...)`
- `check(...)`
- `record(...)`
- `status(...)`
- `resume(...)`
- `reset(...)`
- `spent(...)`

## Compatibility plan

- `switchboard.gas_budget.GasBudgetTracker` becomes a thin rolling/per-wallet wrapper.
- `switchboard.gas_tracker.GasTracker` becomes a singleton calendar/global wrapper.
- Existing tests keep using the legacy names.

## Why this is better

- One source of truth for budget behavior.
- Old imports keep working.
- New code can choose the policy explicitly instead of inheriting hidden semantics from the module name.
