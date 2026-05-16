# feat(payment): wire NonceManager into PaymentClient for reorg safety

## Motivation

`switchboard/nonce_manager.py` ships a fully-featured, thread-safe,
reorg-aware `NonceManager` — but `PaymentClient` in
`src/payment_protocol.py` never used it. Instead it maintained its own
ad-hoc cache:

```python
def get_nonce(self, force_refresh: bool = False) -> int:
    if force_refresh or self.wallet_address not in self._nonce_cache:
        self._nonce_cache[self.wallet_address] = self.w3.eth.get_transaction_count(self.wallet_address)
    else:
        self._nonce_cache[self.wallet_address] += 1
    return self._nonce_cache[self.wallet_address]
```

That cache:

- Does not distinguish pending from confirmed nonces.
- Cannot react to chain reorgs (nonces silently advance off the head).
- Cannot release a nonce if a transaction reverts; subsequent sends
  collide forever.
- Is not safe under concurrent senders even within a single process.

So the safer manager that already ships in the same package was simply
not on the actual payment path.

## Design

- **`_Web3ChainClient` adapter** (module-private). Implements the single
  method `get_current_onchain_nonce(address)` required by
  `NonceManager.ChainClient`. This keeps the `nonce_manager` module
  free of any `web3` dependency.
- **`PaymentClient.__init__`** now accepts an optional `nonce_manager`.
  - If callers do not pass one (the entire existing surface), we
    default-construct `NonceManager(_Web3ChainClient(self.w3))` so all
    existing users transparently get the reorg-safe path.
  - Callers that want to share a manager across multiple
    `PaymentClient` instances, or wire one into a separate reorg
    detector, can inject their own.
  - The `NonceManager` import is **lazy** (inside `__init__`) so users
    who installed `switchboard` without `web3` still get a working
    `nonce_manager` import; conversely, removing `web3` does not break
    importing `switchboard.nonce_manager`.
- **`get_nonce`** now delegates to `self.nonce_manager.acquire_nonce`.
  `force_refresh` is kept as a no-op for API compatibility — the
  manager always syncs with the chain on each acquire.
- **`sign_and_send`** stashes the acquired nonce keyed by the returned
  `tx_hash` in `self._tx_nonces`.
- **`wait_for_confirmations`** looks up that nonce and calls
  `confirm_nonce` on success (status == 1) or `release_nonce` on
  failure (status == 0), then clears the entry.
- `self._nonce_cache` is retained as an empty dict to avoid breaking
  any third-party code that pokes at it; it is marked deprecated in
  the docstring and is no longer read or written by this class.

## Behavior change

- **Happy path:** identical — same nonces, same broadcast order.
- **Reorgs:** when an external reorg detector calls
  `nonce_manager.on_reorg(...)`, pending transactions tied to
  reverted nonces are invalidated locally and (if a re-queue callback
  was set) re-emitted. Wiring a reorg detector is out of scope here;
  this PR only exposes the API.
- **Failed receipts:** the consumed nonce is now released back to the
  manager rather than silently leaving the cache poisoned.

## Files changed

- `src/payment_protocol.py` — adapter class, `__init__` signature,
  `get_nonce`, `sign_and_send`, `wait_for_confirmations`.
- `tests/test_payment_client_nonce.py` — 8 new tests (added).

## Test plan

- [x] `tests/test_payment_client_nonce.py` — 8 passing tests:
  - default `NonceManager` is constructed when none is supplied
  - an injected `NonceManager` is used as-is
  - `get_nonce` delegates to `acquire_nonce`
  - `sign_and_send` acquires a nonce and stashes it under `tx_hash`
  - user-supplied `tx['nonce']` wins
  - successful receipt calls `confirm_nonce`
  - failed receipt calls `release_nonce` and raises
  - `create_payment` end-to-end acquires-then-confirms
- [x] `tests/test_payment_protocol.py` — 10 pre-existing tests still
      pass (no regressions).
- [ ] Integration tests against a real Anvil node — out of scope for
      this PR.

## Not in this PR

- Hooking `on_reorg` into a chain monitor / block subscription.
- Removing `self._nonce_cache`; left in place for backward compat.
- Async path (`AsyncPaymentClient`) inherits the new behavior via
  `PaymentClient.__init__`; no additional async wiring required.

---

Signed-off-by: pattermesh <pattermesh@gmail.com>
