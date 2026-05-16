# fix(async): forward kwargs in AsyncPaymentClient.create_payment_async

## Bug

`AsyncPaymentClient.create_payment_async` passes its `**kwargs` dict to
`loop.run_in_executor` as a single positional argument instead of
unpacking it. The offending code in `src/payment_protocol.py`:

```python
async def create_payment_async(self, payee: str, amount_wei: int, **kwargs) -> PaymentRequest:
    """Async version of create_payment"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, self.create_payment, payee, amount_wei, kwargs)
```

`loop.run_in_executor(executor, func, *args)` only forwards positional
arguments — there is no kwargs unpacking. The executor therefore calls:

```python
self.create_payment(payee, amount_wei, {"timeout_blocks": 100, "description": "x"})
```

Since `create_payment`'s third positional parameter is
`timeout_blocks: int = 100`, the call raises `TypeError` (or silently
binds a dict where an int is expected on unstrict runtimes).

Minimal repro:

```python
await client.create_payment_async(payee, amt, timeout_blocks=100, description="x")
# TypeError: unsupported type for timeout_blocks (got dict)
```

## Fix

Wrap the call in `functools.partial` so kwargs are bound before the
executor invokes the callable:

```diff
 import asyncio
+import functools
 import hashlib
 ...
     async def create_payment_async(self, payee: str, amount_wei: int, **kwargs) -> PaymentRequest:
         """Async version of create_payment"""
         loop = asyncio.get_event_loop()
-        return await loop.run_in_executor(None, self.create_payment, payee, amount_wei, kwargs)
+        return await loop.run_in_executor(
+            None,
+            functools.partial(self.create_payment, payee, amount_wei, **kwargs),
+        )
```

`functools` is stdlib — no new runtime dependencies.

## Test

Added `test_async_create_payment_forwards_kwargs` to
`tests/test_payment_protocol.py`. It patches `HAS_WEB3`, `Web3`, and
`Account` at the module level (matching the existing
`unittest.mock.patch` style used elsewhere in this file), instantiates
`AsyncPaymentClient`, replaces `client.create_payment` with a
`MagicMock`, and calls `create_payment_async` with two kwargs
(`timeout_blocks=42`, `description="forwarded"`). It then asserts that
the underlying `create_payment` was invoked with those kwargs as real
keyword arguments — not as a dict in the third positional slot.

Run:

```
$ python3 -m pytest tests/test_payment_protocol.py -v
...
tests/test_payment_protocol.py::test_async_create_payment_forwards_kwargs PASSED
============================== 11 passed in 0.06s ==============================
```

## Audit of sibling async methods

I checked the other async methods on `AsyncPaymentClient` for the same
class of bug:

- `confirm_payment_async(self, request_id)` — no `**kwargs` parameter,
  only forwards a single positional `request_id`. **Safe.**
- `wait_for_confirmations_async(self, tx_hash)` — already uses a
  `lambda: self.wait_for_confirmations(tx_hash)` closure, which
  captures arguments correctly. **Safe.**

Only `create_payment_async` is affected.
