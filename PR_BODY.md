# fix(pkg): ship payment_protocol in wheel; expose at switchboard.payment_protocol

## Summary

- `pyproject.toml` configures the wheel with `packages = ["switchboard"]`, but `payment_protocol.py` (the `PaymentClient` / `PaymentRequest` / `parse_wei` / `format_wei` module) lived in `src/`, so it was **excluded from the wheel**. Anyone who `pip install`s the package gets `ModuleNotFoundError` for the headline feature.
- Moved `src/payment_protocol.py` to `switchboard/payment_protocol.py` (via `git mv`, history preserved), removed the now-empty `src/` directory and its `/src` sdist include, and re-exported `PaymentClient`, `AsyncPaymentClient`, `PaymentRequest`, `PaymentState`, `parse_wei`, `format_wei` from the package root so `from switchboard import PaymentClient` works.
- Updated tests, README quickstart, "What's in the box" table, and `docs/agent-payment-protocol.md` to the canonical `switchboard.payment_protocol` import path. README previously advertised a bare `from payment_protocol import PaymentClient` — that only ever worked from inside a repo clone.

## Reproduction (today, before this PR)

```bash
pip install switchboard-agents
python -c "from switchboard.payment_protocol import PaymentClient"
# ModuleNotFoundError: No module named 'switchboard.payment_protocol'

python -c "from payment_protocol import PaymentClient"   # as advertised in README
# ModuleNotFoundError: No module named 'payment_protocol'
```

The wheel `RECORD` only contains `switchboard/__init__.py` — `payment_protocol.py` is not shipped because it lives outside the `switchboard/` package directory that hatchling is configured to vendor.

## Test plan

- [x] `python -m pytest tests/test_payment_protocol.py -v` — **10 passed** (no `ModuleNotFoundError` after `from src` → `from switchboard` migration).
- [x] `python -c "from switchboard import PaymentClient, AsyncPaymentClient, PaymentRequest, PaymentState, parse_wei, format_wei"` succeeds with web3/eth-account **not** installed (the `try/except ImportError` guard in `payment_protocol.py` keeps top-level import safe).
- [x] `python -c "from switchboard.payment_protocol import PaymentClient"` succeeds — this is the canonical path now documented in README and `docs/agent-payment-protocol.md`.
- [ ] `python -m build && unzip -l dist/switchboard_agents-0.1.0-py3-none-any.whl | grep payment_protocol` — confirm `switchboard/payment_protocol.py` is present inside the wheel.
- [ ] `python -m build && tar -tzf dist/switchboard_agents-0.1.0.tar.gz | grep payment_protocol` — confirm the sdist still ships it (now under `switchboard/`, not `src/`).
- [ ] Spot-check `git log --follow switchboard/payment_protocol.py` — history is preserved across the move.

## Non-goals / out of scope

- No dependency changes. No behavior changes inside `payment_protocol.py` — pure packaging fix plus a re-export.
- The `src/` layout was the only file at that path, so removing the directory is safe.
