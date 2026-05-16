# refactor(gas): unify on GasBudgetTracker; deprecate GasTracker

## Motivation

The package shipped two implementations of the same idea:

- `switchboard/gas_budget.py` — `GasBudgetTracker` (per-wallet, rolling-window,
  thread-safe, pluggable clock, no global state). Newer, clearly the better
  design.
- `switchboard/gas_tracker.py` — `GasTracker` (singleton via `__new__`,
  calendar-day UTC reset — a burst at 23:59 UTC could reset 1 minute later,
  process-global state). Older.

`X402Middleware._validate_offer` only called the legacy
`can_send_transaction` / `record_gas_usage` API, silently ignoring whatever
budget configuration users built with the newer tracker. Two "blessed" gas
APIs in one library is technical debt. This PR picks one and provides a
back-compat path for the other.

## What changed

- **Added `WalletBoundBudget` adapter** in `switchboard/gas_budget.py`. Wraps a
  `GasBudgetTracker` together with a fixed wallet address and exposes the
  legacy `can_send_transaction(int) -> bool` / `record_gas_usage(int) -> None`
  / `is_paused() -> bool` surface. Obtain via
  `GasBudgetTracker(...).bind_wallet(addr)`.
- **Wired `X402Middleware`** docstring and `__init__` signature to recommend
  `GasBudgetTracker` + `bind_wallet(...)`. The middleware was already
  duck-typed — no runtime change to its behavior; this is documentation +
  typing tightening that makes the new tracker the path users will reach for.
- **Deprecated `GasTracker`.** Emits `DeprecationWarning` on instantiation
  (with `stacklevel=2`, so the warning is attributed to user code). The
  warning advertises the v0.3 removal target and points at
  `GasBudgetTracker.bind_wallet`.
- **Pytest config:** added a single `filterwarnings` entry so the legacy
  `test_gas_tracker.py` suite stays clean without each test needing
  `pytest.warns(...)`.
- **README:** "What's in the box" row updated to mark `gas_tracker.py` as
  deprecated and describe the migration.
- **Integration test:** new `tests/test_x402_with_gas_budget.py` that
  constructs a `GasBudgetTracker`, binds it to a payer wallet, passes it to
  `X402Middleware`, and verifies a small offer is accepted while a too-large
  offer is rejected through `can_send_transaction`.

## Migration

Before:

```python
from switchboard.gas_tracker import GasTracker

tracker = GasTracker(hourly_limit=2_000_000, daily_limit=20_000_000)
middleware = X402Middleware(payment_client=client, gas_tracker=tracker)
```

After:

```python
from switchboard.gas_budget import GasBudgetTracker, GasLimits

budget = GasBudgetTracker(
    default_limits=GasLimits(per_hour=2_000_000, per_day=20_000_000),
)
middleware = X402Middleware(
    payment_client=client,
    gas_tracker=budget.bind_wallet(client.wallet_address),
    max_payment_wei=10**16,
)
```

You get:

- per-wallet accounting (the same `budget` can govern many signers),
- rolling-window enforcement (no 23:59-UTC reset cliff),
- no global singleton state,
- a deterministic clock hook for tests.

## Backward compatibility

`GasTracker` still works. Instantiating it emits a `DeprecationWarning` and
will continue to satisfy the middleware's duck-typed `gas_tracker` parameter.
Removal is scheduled for v0.3.

## Test plan

- [x] `tests/test_gas_budget.py` — 12 passed (existing).
- [x] `tests/test_gas_tracker.py` — 9 passed (existing, warnings filtered).
- [x] `tests/test_x402_with_gas_budget.py` — 5 new tests, all passing.
- [x] `tests/test_x402_middleware.py` — 19 passed (existing).
- [x] Deprecation warning fires in user code (`python -W default`) and
      stays silent under the project pytest config.
