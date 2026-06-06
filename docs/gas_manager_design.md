# GasManager Unified Design Document

## Problem

Two overlapping gas-management modules exist:
- `gas_budget.py` — rolling-window, per-wallet, multi-wallet
- `gas_tracker.py` — calendar-reset, global singleton

They have different semantics and APIs, causing confusion and code duplication.

## Solution: Unified GasManager

A single `GasManager` class that supports both use cases through configuration.

### Key Design Decisions

1. **Window Mode**: `rolling` (deque-based, from gas_budget) or `calendar` (hour-aligned, from gas_tracker)
2. **Scope**: Per-wallet tracking (default) or global singleton mode
3. **Backward Compatibility**: Wrappers that reimplement old APIs on top of GasManager

### API Design

```python
class GasManager:
    def __init__(
        self,
        hourly_limit: int = 0,      # 0 = no limit
        daily_limit: int = 0,       # 0 = no limit
        window_mode: str = "rolling",  # "rolling" or "calendar"
        singleton: bool = False,     # If True, acts as global singleton
        clock: Callable = time.time,
    )

    # Core operations
    def can_spend(self, wallet: str, estimated_gas: int) -> bool
    def record(self, wallet: str, gas_used: int) -> GasStatus
    def status(self, wallet: str) -> GasStatus

    # Control
    def pause(self, wallet: str) -> None
    def resume(self, wallet: str) -> None
    def reset(self, wallet: str) -> None
    def set_limits(self, wallet: str, hourly: int, daily: int) -> None

    # Global mode convenience (uses "global" wallet)
    def can_send_transaction(self, estimated_gas: int) -> bool
    def record_gas_usage(self, gas_used: int) -> None
    def is_paused(self) -> bool
    def get_current_spent(self) -> tuple[int, int]
    def reset_all(self) -> None
```

### Backward Compatibility

- `GasBudgetTracker` → thin wrapper around `GasManager(window_mode="rolling")`
- `GasTracker` → thin wrapper around `GasManager(window_mode="calendar", singleton=True)`

All existing imports continue to work unchanged.
